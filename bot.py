import asyncio
import hashlib
import html
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "1321815165"))
PORT = int(os.getenv("PORT", "10000"))
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
KYIV = ZoneInfo("Europe/Kyiv")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db = None
cfg = {}
pending = {}  # (chat_id, user_id) -> dict
awaiting = {"welcome": False}
chat_titles = {}
background = []

DEFAULTS = {
    "enabled": "1",
    "mode": "captcha",
    "types": "math,fruit,animal,color,count",
    "timeout": "5",
    "cooldown": "0",
    "notify": "1",
    "welcome": "👋 Привіт! Щоб підтвердити, що ви не бот, пройдіть коротку перевірку.",
}

TYPE_NAMES = {
    "math": "➕ Приклад",
    "fruit": "🍎 Фрукти",
    "animal": "🐶 Тварини",
    "color": "🎨 Кольори",
    "count": "🔢 Підрахунок",
}

FRUITS = {
    "🍎": "яблуко", "🍌": "банан", "🍇": "виноград", "🍓": "полуниця",
    "🍉": "кавун", "🍊": "апельсин", "🍒": "вишня", "🍍": "ананас",
    "🥝": "ківі", "🍑": "персик", "🍋": "лимон", "🍐": "груша",
}
ANIMALS = {
    "🐶": "собака", "🐱": "кіт", "🐭": "миша", "🐰": "кролик",
    "🦊": "лисиця", "🐻": "ведмідь", "🐼": "панда", "🐸": "жаба",
    "🐵": "мавпа", "🦁": "лев", "🐴": "кінь", "🐷": "свиня",
}
COLORS = {
    "🟥": "червоний", "🟦": "синій", "🟩": "зелений", "🟨": "жовтий",
    "🟧": "помаранчевий", "🟪": "фіолетовий", "⬛": "чорний", "⬜": "білий",
    "🟫": "коричневий",
}
POOLS = {"fruit": FRUITS, "animal": ANIMALS, "color": COLORS}
PROMPTS = {
    "fruit": "Натисніть кнопку, де зображено",
    "animal": "Натисніть кнопку, де зображено",
    "color": "Оберіть колір",
}

LABELS = {
    "approved": "✅ Прийнято",
    "declined": "❌ Відхилено (неправильна відповідь)",
    "timeout": "⌛ Не відповів — відхилено",
    "auto": "🤖 Прийнято без капчі",
}

CYCLES = {
    "timeout": ["0", "3", "5", "10", "30"],
    "cooldown": ["0", "5", "30", "60", "1440"],
}


# ---------- налаштування та база ----------

def cget(key):
    return cfg.get(key, DEFAULTS[key])


async def put(key, value):
    cfg[key] = value
    await db.execute(
        "INSERT INTO settings(key, value) VALUES($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        key, value,
    )


async def init_db():
    global db
    dsn = DATABASE_URL.split("?")[0]
    db = await asyncpg.create_pool(
        dsn,
        ssl="require",
        min_size=0,
        max_size=3,
        statement_cache_size=0,
        max_inactive_connection_lifetime=30,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key text PRIMARY KEY,
            value text NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stats (
            id bigserial PRIMARY KEY,
            ts timestamptz NOT NULL DEFAULT now(),
            chat_id bigint NOT NULL,
            user_id bigint NOT NULL,
            result text NOT NULL
        );
        CREATE TABLE IF NOT EXISTS blacklist (
            user_id bigint PRIMARY KEY,
            until timestamptz
        );
        """
    )
    for r in await db.fetch("SELECT key, value FROM settings"):
        cfg[r["key"]] = r["value"]


async def record(chat_id, user_id, result):
    try:
        await db.execute(
            "INSERT INTO stats(chat_id, user_id, result) VALUES($1, $2, $3)",
            chat_id, user_id, result,
        )
    except Exception:
        log.exception("record failed")


async def apply_cooldown(user_id):
    minutes = int(cget("cooldown"))
    if minutes <= 0:
        return
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    try:
        await db.execute(
            "INSERT INTO blacklist(user_id, until) VALUES($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET until = EXCLUDED.until "
            "WHERE blacklist.until IS NOT NULL",
            user_id, until,
        )
    except Exception:
        log.exception("cooldown failed")


# ---------- допоміжне ----------

def fmt_min(v):
    v = int(v)
    if v == 0:
        return "вимк"
    if v < 60:
        return f"{v} хв"
    return f"{v // 60} год"


def onoff(v):
    return "✅ УВІМК" if v == "1" else "⛔ ВИМК"


async def chat_title(chat_id):
    if chat_id not in chat_titles:
        try:
            chat = await bot.get_chat(chat_id)
            chat_titles[chat_id] = chat.title or str(chat_id)
        except TelegramAPIError:
            return str(chat_id)
    return chat_titles[chat_id]


async def try_approve(chat_id, user_id):
    try:
        await bot.approve_chat_join_request(chat_id, user_id)
        return True
    except TelegramBadRequest:
        return False


async def safe_decline(chat_id, user_id):
    try:
        await bot.decline_chat_join_request(chat_id, user_id)
    except TelegramAPIError:
        pass


async def notify(chat_id, user_id, name, username, result):
    if cget("notify") != "1":
        return
    uname = f"@{html.escape(username)}" if username else "без username"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚫 Заблокувати", callback_data=f"bl:{user_id}")]]
    )
    title = html.escape(await chat_title(chat_id))
    text = (
        f"{LABELS[result]}\n"
        f'👤 <a href="tg://user?id={user_id}">{html.escape(name)}</a> '
        f"({uname}, <code>{user_id}</code>)\n"
        f"📣 {title}"
    )
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=kb)
    except TelegramAPIError:
        pass


# ---------- капчі ----------

def build_captcha(kind):
    if kind == "math":
        op = random.choice(["+", "-", "×"])
        if op == "+":
            a, b = random.randint(2, 12), random.randint(2, 12)
            ans = a + b
        elif op == "-":
            a = random.randint(8, 20)
            b = random.randint(2, a - 1)
            ans = a - b
        else:
            a, b = random.randint(2, 6), random.randint(2, 6)
            ans = a * b
        options = {ans}
        while len(options) < 4:
            w = ans + random.randint(-5, 5)
            if w >= 0:
                options.add(w)
        options = list(options)
        random.shuffle(options)
        text = f"Розв'яжіть приклад:\n\n<b>{a} {op} {b} = ?</b>"
        return text, [(str(o), o == ans) for o in options]

    if kind in POOLS:
        items = random.sample(list(POOLS[kind].items()), 4)
        emoji, name = random.choice(items)
        text = f"{PROMPTS[kind]}: <b>{name}</b>"
        return text, [(e, e == emoji) for e, _ in items]

    # count
    emoji = random.choice(list(FRUITS))
    n = random.randint(2, 7)
    options = {n}
    while len(options) < 4:
        w = n + random.randint(-3, 3)
        if 1 <= w <= 10:
            options.add(w)
    options = list(options)
    random.shuffle(options)
    text = "Скільки предметів ви бачите?\n\n" + " ".join([emoji] * n)
    return text, [(str(o), o == n) for o in options]


def captcha_markup(chat_id, user_id, options):
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=label,
                callback_data=f"c:{chat_id}:{user_id}:{int(ok)}",
            )
            for label, ok in options
        ]]
    )


def enabled_kinds():
    kinds = [k for k in cget("types").split(",") if k in TYPE_NAMES]
    return kinds or ["math"]


# ---------- заявки на вступ ----------

@dp.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    if cget("enabled") != "1":
        return
    uid = req.from_user.id
    cid = req.chat.id
    chat_titles[cid] = req.chat.title or str(cid)

    try:
        row = await db.fetchrow("SELECT until FROM blacklist WHERE user_id = $1", uid)
    except Exception:
        log.exception("blacklist check failed")
        row = None
    if row:
        until = row["until"]
        if until is None or until > datetime.now(timezone.utc):
            await safe_decline(cid, uid)
            await record(cid, uid, "blocked")
            return
        await db.execute("DELETE FROM blacklist WHERE user_id = $1", uid)

    if cget("mode") == "auto":
        if await try_approve(cid, uid):
            await record(cid, uid, "auto")
            await notify(cid, uid, req.from_user.full_name, req.from_user.username, "auto")
        return

    question, options = build_captcha(random.choice(enabled_kinds()))
    try:
        msg = await bot.send_message(
            req.user_chat_id,
            f"{cget('welcome')}\n\n{question}",
            reply_markup=captcha_markup(cid, uid, options),
        )
    except TelegramAPIError:
        log.warning("cannot message user %s", uid)
        return
    pending[(cid, uid)] = {
        "ucid": req.user_chat_id,
        "mid": msg.message_id,
        "t": time.monotonic(),
        "name": req.from_user.full_name,
        "username": req.from_user.username,
    }


@dp.callback_query(F.data.startswith("c:"))
async def on_answer(cb: CallbackQuery):
    _, chat_id, user_id, ok = cb.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    if cb.from_user.id != user_id:
        await cb.answer()
        return
    correct = ok == "1"

    if chat_id == 0:  # тестова капча адміна
        text = "✅ Правильно! (тест)" if correct else "❌ Неправильно. (тест)"
        try:
            await cb.message.edit_text(text)
        except Exception:
            pass
        await cb.answer()
        return

    info = pending.pop((chat_id, user_id), None)
    name = info["name"] if info else cb.from_user.full_name
    username = info["username"] if info else cb.from_user.username

    result = None
    if correct:
        if await try_approve(chat_id, user_id):
            text = "✅ Капчу пройдено, вас прийнято до каналу!"
            result = "approved"
        else:
            text = "⚠️ Заявка вже недійсна. Подайте нову."
    else:
        await safe_decline(chat_id, user_id)
        await apply_cooldown(user_id)
        text = "❌ Неправильно. Заявку відхилено, можете подати нову."
        cd = int(cget("cooldown"))
        if cd > 0:
            text += f" Повторна спроба буде доступна через {fmt_min(cd)}."
        result = "declined"

    try:
        await cb.message.edit_text(text)
    except Exception:
        pass
    await cb.answer()

    if result:
        await record(chat_id, user_id, result)
        await notify(chat_id, user_id, name, username, result)


async def timeout_loop():
    while True:
        await asyncio.sleep(15)
        try:
            minutes = int(cget("timeout"))
            if minutes <= 0 or not pending:
                continue
            now = time.monotonic()
            for key, p in list(pending.items()):
                if now - p["t"] < minutes * 60:
                    continue
                pending.pop(key, None)
                cid, uid = key
                await safe_decline(cid, uid)
                await record(cid, uid, "timeout")
                await apply_cooldown(uid)
                try:
                    await bot.edit_message_text(
                        "⌛ Час вичерпано. Заявку відхилено, можете подати нову.",
                        chat_id=p["ucid"],
                        message_id=p["mid"],
                    )
                except TelegramAPIError:
                    pass
                await notify(cid, uid, p["name"], p["username"], "timeout")
        except Exception:
            log.exception("timeout loop")


# ---------- адмін-панель ----------

def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def main_view():
    mode = "Капча" if cget("mode") == "captcha" else "Приймати всіх"
    text = (
        "⚙️ <b>Панель адміністратора</b>\n\n"
        f"Бот: {onoff(cget('enabled'))}\n"
        f"Режим: {mode}\n"
        f"Типів капчі: {len(enabled_kinds())}\n"
        f"Чекають відповіді зараз: {len(pending)}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("📊 Статистика", "a:stats"), btn("🧩 Типи капчі", "a:types")],
        [btn(f"🔌 Бот: {onoff(cget('enabled'))}", "a:enabled")],
        [btn("🎯 Режим: " + mode, "a:mode")],
        [btn(f"⏱ Тайм-аут: {fmt_min(cget('timeout'))}", "a:timeout")],
        [btn(f"🧊 Пауза після помилки: {fmt_min(cget('cooldown'))}", "a:cooldown")],
        [btn(f"🔔 Сповіщення: {onoff(cget('notify'))}", "a:notify")],
        [btn("✏️ Привітання", "a:welcome"), btn("🧪 Тест капчі", "a:test")],
        [btn("🚫 Чорний список", "a:bl")],
    ])
    return text, kb


def types_view():
    on = enabled_kinds()
    text = "🧩 <b>Типи капчі</b>\n\nДля кожної заявки обирається випадковий тип серед увімкнених."
    rows = [
        [btn(f"{'✅' if k in on else '⬜'} {name}", f"a:tt:{k}")]
        for k, name in TYPE_NAMES.items()
    ]
    rows.append([btn("◀️ Назад", "a:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def stats_view():
    async def counts(since):
        rows = await db.fetch(
            "SELECT result, count(*) AS n FROM stats WHERE ts >= $1 GROUP BY result", since
        )
        return {r["result"]: r["n"] for r in rows}

    def line(label, c):
        ok = c.get("approved", 0) + c.get("auto", 0)
        return (
            f"<b>{label}</b>: ✅ {ok} · ❌ {c.get('declined', 0)} · "
            f"⌛ {c.get('timeout', 0)} · 🚫 {c.get('blocked', 0)}"
        )

    now = datetime.now(KYIV)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)

    text = "📊 <b>Статистика</b>\n\n"
    text += line("Сьогодні", await counts(today)) + "\n"
    text += line("7 днів", await counts(week)) + "\n"
    text += line("Всього", await counts(epoch)) + "\n"
    text += "\n✅ прийнято · ❌ відхилено · ⌛ тайм-аут · 🚫 заблоковано\n"

    rows = await db.fetch(
        "SELECT chat_id, "
        "count(*) FILTER (WHERE result IN ('approved', 'auto')) AS ok, "
        "count(*) AS total FROM stats GROUP BY chat_id ORDER BY total DESC LIMIT 5"
    )
    if rows:
        text += "\n<b>Канали (всього):</b>\n"
        for r in rows:
            title = html.escape(await chat_title(r["chat_id"]))
            text += f"📣 {title}: прийнято {r['ok']} із {r['total']}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔄 Оновити", "a:stats")],
        [btn("◀️ Назад", "a:main")],
    ])
    return text, kb


async def bl_view():
    await db.execute("DELETE FROM blacklist WHERE until IS NOT NULL AND until < now()")
    total = await db.fetchval("SELECT count(*) FROM blacklist")
    rows = await db.fetch("SELECT user_id, until FROM blacklist ORDER BY user_id DESC LIMIT 10")
    text = (
        f"🚫 <b>Чорний список</b>: {total}\n\n"
        "🚫 — назавжди, ⏳ — тимчасова пауза після помилки.\n"
        "Додати вручну: <code>/ban ID</code>"
    )
    kb_rows = [
        [btn(f"{'⏳' if r['until'] else '🚫'} {r['user_id']} — розблокувати", f"a:ub:{r['user_id']}")]
        for r in rows
    ]
    kb_rows.append([btn("◀️ Назад", "a:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def show(cb, text, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@dp.message(Command("admin", "start"), F.from_user.id == ADMIN_ID)
async def cmd_admin(m: Message):
    awaiting["welcome"] = False
    text, kb = main_view()
    await m.answer(text, reply_markup=kb)


@dp.message(Command("ban"), F.from_user.id == ADMIN_ID)
async def cmd_ban(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await m.answer("Використання: <code>/ban 123456789</code>")
        return
    await db.execute(
        "INSERT INTO blacklist(user_id, until) VALUES($1, NULL) "
        "ON CONFLICT (user_id) DO UPDATE SET until = NULL",
        int(arg),
    )
    await m.answer(f"🚫 Заблоковано: <code>{arg}</code>")


@dp.message(Command("unban"), F.from_user.id == ADMIN_ID)
async def cmd_unban(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await m.answer("Використання: <code>/unban 123456789</code>")
        return
    await db.execute("DELETE FROM blacklist WHERE user_id = $1", int(arg))
    await m.answer(f"✅ Розблоковано: <code>{arg}</code>")


def waiting_welcome(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id == ADMIN_ID and awaiting["welcome"] and m.text)


@dp.message(waiting_welcome)
async def on_welcome_text(m: Message):
    text = m.text.strip()
    if len(text) > 300:
        await m.answer("Занадто довго (максимум 300 символів). Спробуйте коротше.")
        return
    await put("welcome", html.escape(text))
    awaiting["welcome"] = False
    await m.answer("✅ Привітання збережено.")
    t, kb = main_view()
    await m.answer(t, reply_markup=kb)


@dp.callback_query(F.data.startswith("bl:"), F.from_user.id == ADMIN_ID)
async def on_block(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    await db.execute(
        "INSERT INTO blacklist(user_id, until) VALUES($1, NULL) "
        "ON CONFLICT (user_id) DO UPDATE SET until = NULL",
        uid,
    )
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("🚫 Заблоковано")


@dp.callback_query(F.data.startswith("a:"), F.from_user.id == ADMIN_ID)
async def on_admin(cb: CallbackQuery):
    parts = cb.data.split(":")
    action = parts[1]
    arg = parts[2] if len(parts) > 2 else None
    screen = "main"

    if action != "welcome":
        awaiting["welcome"] = False

    if action == "enabled":
        await put("enabled", "0" if cget("enabled") == "1" else "1")
    elif action == "notify":
        await put("notify", "0" if cget("notify") == "1" else "1")
    elif action == "mode":
        await put("mode", "auto" if cget("mode") == "captcha" else "captcha")
    elif action in CYCLES:
        vals = CYCLES[action]
        cur = cget(action)
        nxt = vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]
        await put(action, nxt)
    elif action == "wreset":
        await put("welcome", DEFAULTS["welcome"])
    elif action == "types":
        screen = "types"
    elif action == "tt":
        on = enabled_kinds()
        if arg in on:
            if len(on) == 1:
                await cb.answer("Має лишитись хоча б один тип", show_alert=True)
                return
            on.remove(arg)
        elif arg in TYPE_NAMES:
            on.append(arg)
        ordered = [k for k in TYPE_NAMES if k in on]
        await put("types", ",".join(ordered))
        screen = "types"
    elif action == "stats":
        screen = "stats"
    elif action == "bl":
        screen = "bl"
    elif action == "ub":
        await db.execute("DELETE FROM blacklist WHERE user_id = $1", int(arg))
        screen = "bl"
    elif action == "test":
        kind = random.choice(enabled_kinds())
        question, options = build_captcha(kind)
        await bot.send_message(
            ADMIN_ID,
            f"🧪 <b>Тест</b> ({TYPE_NAMES[kind]})\n\n{question}",
            reply_markup=captcha_markup(0, ADMIN_ID, options),
        )
        await cb.answer("Тест надіслано")
        return
    elif action == "welcome":
        awaiting["welcome"] = True
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("♻️ Стандартне", "a:wreset"), btn("◀️ Скасувати", "a:main")],
        ])
        await show(
            cb,
            "✏️ Надішліть наступним повідомленням новий текст привітання "
            "(до 300 символів). Він показується перед завданням капчі.\n\n"
            f"Поточне:\n{cget('welcome')}",
            kb,
        )
        await cb.answer()
        return

    if screen == "types":
        text, kb = types_view()
    elif screen == "stats":
        text, kb = await stats_view()
    elif screen == "bl":
        text, kb = await bl_view()
    else:
        text, kb = main_view()
    await show(cb, text, kb)
    await cb.answer()


# ---------- запуск ----------

async def on_startup(bot: Bot):
    await init_db()
    await bot.set_webhook(
        f"{BASE_URL}/webhook",
        secret_token=SECRET,
        allowed_updates=["message", "callback_query", "chat_join_request"],
        drop_pending_updates=False,
    )
    background.append(asyncio.create_task(timeout_loop()))


async def on_shutdown(bot: Bot):
    for t in background:
        t.cancel()
    if db:
        await db.close()


async def health(request):
    return web.Response(text="ok")


def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
