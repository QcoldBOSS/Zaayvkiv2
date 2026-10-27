import asyncio
import hashlib
import html
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

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
    ChatMemberUpdated,
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
OWNER_ID = int(os.getenv("ADMIN_ID", "1321815165"))
PORT = int(os.getenv("PORT", "10000"))
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]

FLUSH_EVERY = 1200  # секунд між записами статистики в базу
FLUSH_MAX = 100     # або коли назбиралось стільки записів

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

LANGS = ("ru", "en", "uk")
LI = {"ru": 0, "en": 1, "uk": 2}
TYPES = ("math", "fruit", "animal", "color", "count")
CYCLES = {"timeout": [0, 3, 5, 10, 30], "cooldown": [0, 5, 30, 60, 1440]}
DEFAULT_CH = {
    "enabled": True,
    "mode": "captcha",
    "types": ",".join(TYPES),
    "timeout": 5,
    "cooldown": 0,
    "notify": 0,
    "welcome": "",
}

# ---------- стан у пам'яті ----------
db = None
BOT_USER = ""
STARTED = time.time()
chats = {}        # chat_id -> dict (налаштування каналу)
admins = {}       # user_id -> set(chat_id)
ulang = {}        # user_id -> обрана мова
seen = {}         # user_id -> мова з профілю Telegram
blocks = {}       # (chat_id, user_id) -> None (назавжди) або datetime (пауза)
pending = {}      # (chat_id, user_id) -> dict
awaiting = {}     # user_id -> chat_id (очікуємо текст привітання)
sel = {}          # user_id -> chat_id (відкритий канал, для /ban)
admin_cache = {}  # (chat_id, user_id) -> (bool, time)
stat_buf = []
background = []
flush_lock = asyncio.Lock()

# ---------- тексти: (ru, en, uk) ----------
S = {
    "pick_lang": "🌐 Выберите язык · Choose your language · Оберіть мову",
    "lang_set": ("✅ Язык: русский", "✅ Language: English", "✅ Мова: українська"),
    "home": (
        "👋 <b>Captcha</b> — защита заявок на вступление.\n\nДобавьте меня в канал или группу администратором с правом «Приглашать пользователей», а в пригласительной ссылке включите одобрение заявок администратором. Я буду проверять каждую заявку капчей.\n\n📣 Ваши каналы и группы: {n}",
        "👋 <b>Captcha</b> — join request protection.\n\nAdd me to your channel or group as an admin with the “Invite users” right, and turn on admin approval in the invite link. I will check every join request with a captcha.\n\n📣 Your channels and groups: {n}",
        "👋 <b>Captcha</b> — захист заявок на вступ.\n\nДодайте мене в канал або групу адміністратором із правом «Запрошувати користувачів», а в запрошувальному посиланні увімкніть схвалення заявок адміністратором. Я перевірятиму кожну заявку капчею.\n\n📣 Ваші канали та групи: {n}",
    ),
    "b_add_ch": ("➕ Добавить в канал", "➕ Add to a channel", "➕ Додати в канал"),
    "b_add_gr": ("➕ Добавить в группу", "➕ Add to a group", "➕ Додати в групу"),
    "b_find": ("🔄 Найти мои каналы", "🔄 Find my channels", "🔄 Знайти мої канали"),
    "b_lang": ("🌐 Язык", "🌐 Language", "🌐 Мова"),
    "b_back": ("◀️ Назад", "◀️ Back", "◀️ Назад"),
    "b_refresh": ("🔄 Обновить", "🔄 Refresh", "🔄 Оновити"),
    "no_access": ("Нет доступа", "No access", "Немає доступу"),
    "panel": (
        "⚙️ <b>{title}</b>\n\nБот: {enabled}\nРежим: {mode}\nТипов капчи: {n}\nЖдут ответа: {p}{warn}",
        "⚙️ <b>{title}</b>\n\nBot: {enabled}\nMode: {mode}\nCaptcha types: {n}\nWaiting for an answer: {p}{warn}",
        "⚙️ <b>{title}</b>\n\nБот: {enabled}\nРежим: {mode}\nТипів капчі: {n}\nЧекають відповіді: {p}{warn}",
    ),
    "warn_rights": (
        "\n\n⚠️ У бота нет права «Приглашать пользователей». Выдайте его в настройках администраторов, иначе я не смогу одобрять заявки.",
        "\n\n⚠️ The bot lacks the “Invite users” admin right. Grant it in the admin settings, otherwise I cannot approve requests.",
        "\n\n⚠️ У бота немає права «Запрошувати користувачів». Надайте його в налаштуваннях адміністраторів, інакше я не зможу схвалювати заявки.",
    ),
    "mode_captcha": ("Капча", "Captcha", "Капча"),
    "mode_auto": ("Принимать всех", "Accept everyone", "Приймати всіх"),
    "on": ("✅ ВКЛ", "✅ ON", "✅ УВІМК"),
    "off": ("⛔ ВЫКЛ", "⛔ OFF", "⛔ ВИМК"),
    "nf_me": ("✅ мне", "✅ to me", "✅ мені"),
    "nf_other": ("✅ другому админу", "✅ to another admin", "✅ іншому адміну"),
    "b_stats": ("📊 Статистика", "📊 Statistics", "📊 Статистика"),
    "b_types": ("🧩 Типы капчи", "🧩 Captcha types", "🧩 Типи капчі"),
    "b_bot": ("🔌 Бот: {v}", "🔌 Bot: {v}", "🔌 Бот: {v}"),
    "b_mode": ("🎯 Режим: {v}", "🎯 Mode: {v}", "🎯 Режим: {v}"),
    "b_timeout": ("⏱ Тайм-аут: {v}", "⏱ Timeout: {v}", "⏱ Тайм-аут: {v}"),
    "b_cool": ("🧊 Пауза после ошибки: {v}", "🧊 Pause after a mistake: {v}", "🧊 Пауза після помилки: {v}"),
    "b_notify": ("🔔 Уведомления: {v}", "🔔 Notifications: {v}", "🔔 Сповіщення: {v}"),
    "b_welcome": ("✏️ Приветствие", "✏️ Greeting", "✏️ Привітання"),
    "b_test": ("🧪 Тест капчи", "🧪 Test captcha", "🧪 Тест капчі"),
    "b_bl": ("🚫 Чёрный список", "🚫 Blacklist", "🚫 Чорний список"),
    "m_off": ("выкл", "off", "вимк"),
    "m_min": ("{v} мин", "{v} min", "{v} хв"),
    "m_hour": ("{v} ч", "{v} h", "{v} год"),
    "types_title": (
        "🧩 <b>Типы капчи</b>\n\nДля каждой заявки выбирается случайный тип из включённых.",
        "🧩 <b>Captcha types</b>\n\nA random type among the enabled ones is chosen for each request.",
        "🧩 <b>Типи капчі</b>\n\nДля кожної заявки обирається випадковий тип серед увімкнених.",
    ),
    "tn_math": ("➕ Пример", "➕ Math", "➕ Приклад"),
    "tn_fruit": ("🍎 Фрукты", "🍎 Fruits", "🍎 Фрукти"),
    "tn_animal": ("🐶 Животные", "🐶 Animals", "🐶 Тварини"),
    "tn_color": ("🎨 Цвета", "🎨 Colors", "🎨 Кольори"),
    "tn_count": ("🔢 Подсчёт", "🔢 Counting", "🔢 Підрахунок"),
    "min_one": (
        "Должен остаться хотя бы один тип",
        "At least one type must stay enabled",
        "Має лишитись хоча б один тип",
    ),
    "stats_title": ("📊 <b>Статистика</b>", "📊 <b>Statistics</b>", "📊 <b>Статистика</b>"),
    "st_24h": ("24 часа", "24 hours", "24 години"),
    "st_7d": ("7 дней", "7 days", "7 днів"),
    "st_all": ("Всего", "All time", "Всього"),
    "st_legend": (
        "✅ принято · ❌ отклонено · ⌛ тайм-аут · 🚫 заблокировано",
        "✅ approved · ❌ declined · ⌛ timeout · 🚫 blocked",
        "✅ прийнято · ❌ відхилено · ⌛ тайм-аут · 🚫 заблоковано",
    ),
    "bl_title": (
        "🚫 <b>Чёрный список</b>: {n}\n\n🚫 — навсегда, ⏳ — временная пауза после ошибки.\nДобавить вручную: <code>/ban ID</code>",
        "🚫 <b>Blacklist</b>: {n}\n\n🚫 — permanent, ⏳ — temporary pause after a mistake.\nAdd manually: <code>/ban ID</code>",
        "🚫 <b>Чорний список</b>: {n}\n\n🚫 — назавжди, ⏳ — тимчасова пауза після помилки.\nДодати вручну: <code>/ban ID</code>",
    ),
    "b_unblock": (
        "{i} {id} — разблокировать",
        "{i} {id} — unblock",
        "{i} {id} — розблокувати",
    ),
    "ban_usage": (
        "Использование: <code>/ban 123456789</code>",
        "Usage: <code>/ban 123456789</code>",
        "Використання: <code>/ban 123456789</code>",
    ),
    "no_sel": (
        "Сначала откройте нужный канал в панели: /start",
        "First open the channel in the panel: /start",
        "Спершу відкрийте потрібний канал у панелі: /start",
    ),
    "banned": ("🚫 Заблокирован: <code>{id}</code>", "🚫 Blocked: <code>{id}</code>", "🚫 Заблоковано: <code>{id}</code>"),
    "unbanned": ("✅ Разблокирован: <code>{id}</code>", "✅ Unblocked: <code>{id}</code>", "✅ Розблоковано: <code>{id}</code>"),
    "w_prompt": (
        "✏️ Отправьте следующим сообщением новый текст приветствия (до 300 символов). Он показывается перед заданием капчи.\n\nТекущее:\n{cur}",
        "✏️ Send the new greeting text in your next message (up to 300 characters). It is shown before the captcha task.\n\nCurrent:\n{cur}",
        "✏️ Надішліть наступним повідомленням новий текст привітання (до 300 символів). Він показується перед завданням капчі.\n\nПоточне:\n{cur}",
    ),
    "b_default": ("♻️ Стандартное", "♻️ Default", "♻️ Стандартне"),
    "b_cancel": ("◀️ Отмена", "◀️ Cancel", "◀️ Скасувати"),
    "w_long": (
        "Слишком длинно (максимум 300 символов). Попробуйте короче.",
        "Too long (300 characters max). Try a shorter one.",
        "Занадто довго (максимум 300 символів). Спробуйте коротше.",
    ),
    "w_saved": ("✅ Приветствие сохранено.", "✅ Greeting saved.", "✅ Привітання збережено."),
    "welcome_def": (
        "👋 Привет! Чтобы подтвердить, что вы не бот, пройдите короткую проверку.",
        "👋 Hi! To confirm you are not a bot, please pass a quick check.",
        "👋 Привіт! Щоб підтвердити, що ви не бот, пройдіть коротку перевірку.",
    ),
    "test_head": ("🧪 <b>Тест</b> ({kn})\n\n{q}", "🧪 <b>Test</b> ({kn})\n\n{q}", "🧪 <b>Тест</b> ({kn})\n\n{q}"),
    "test_sent": ("Тест отправлен", "Test sent", "Тест надіслано"),
    "test_ok": ("✅ Верно! (тест)", "✅ Correct! (test)", "✅ Правильно! (тест)"),
    "test_bad": ("❌ Неверно. (тест)", "❌ Wrong. (test)", "❌ Неправильно. (тест)"),
    "q_math": (
        "Решите пример:\n\n<b>{a} {op} {b} = ?</b>",
        "Solve the problem:\n\n<b>{a} {op} {b} = ?</b>",
        "Розв'яжіть приклад:\n\n<b>{a} {op} {b} = ?</b>",
    ),
    "q_pick": (
        "Нажмите кнопку, где изображено: <b>{n}</b>",
        "Tap the button that shows: <b>{n}</b>",
        "Натисніть кнопку, де зображено: <b>{n}</b>",
    ),
    "q_color": ("Выберите цвет: <b>{n}</b>", "Choose the color: <b>{n}</b>", "Оберіть колір: <b>{n}</b>"),
    "q_count": (
        "Сколько предметов вы видите?\n\n{items}",
        "How many items do you see?\n\n{items}",
        "Скільки предметів ви бачите?\n\n{items}",
    ),
    "ok_join": (
        "✅ Капча пройдена, заявка одобрена!",
        "✅ Captcha passed, your request is approved!",
        "✅ Капчу пройдено, заявку схвалено!",
    ),
    "stale": (
        "⚠️ Заявка уже недействительна. Подайте новую.",
        "⚠️ This request is no longer valid. Please send a new one.",
        "⚠️ Заявка вже недійсна. Подайте нову.",
    ),
    "wrong": (
        "❌ Неверно. Заявка отклонена, можете подать новую.",
        "❌ Wrong answer. Your request was declined, you can send a new one.",
        "❌ Неправильно. Заявку відхилено, можете подати нову.",
    ),
    "wait": (
        "Повторная попытка будет доступна через {v}.",
        "You can try again in {v}.",
        "Повторна спроба буде доступна через {v}.",
    ),
    "timeout_user": (
        "⌛ Время вышло. Заявка отклонена, можете подать новую.",
        "⌛ Time is up. Your request was declined, you can send a new one.",
        "⌛ Час вичерпано. Заявку відхилено, можете подати нову.",
    ),
    "n_approved": ("✅ Принят", "✅ Accepted", "✅ Прийнято"),
    "n_declined": ("❌ Отклонён (неверный ответ)", "❌ Declined (wrong answer)", "❌ Відхилено (неправильна відповідь)"),
    "n_timeout": ("⌛ Не ответил — отклонён", "⌛ No answer — declined", "⌛ Не відповів — відхилено"),
    "n_auto": ("🤖 Принят без капчи", "🤖 Accepted without captcha", "🤖 Прийнято без капчі"),
    "n_nouser": ("без username", "no username", "без username"),
    "b_block": ("🚫 Заблокировать", "🚫 Block", "🚫 Заблокувати"),
    "blocked_toast": ("🚫 Заблокирован", "🚫 Blocked", "🚫 Заблоковано"),
    "connected": (
        "✅ Бот подключён к «{title}». Откройте панель: /start",
        "✅ The bot is connected to “{title}”. Open the panel: /start",
        "✅ Бота підключено до «{title}». Відкрийте панель: /start",
    ),
    "no_rights": (
        "⚠️ Бот добавлен в «{title}», но у него нет права «Приглашать пользователей». Выдайте его, иначе заявки не будут одобряться.",
        "⚠️ The bot was added to “{title}” but has no “Invite users” right. Grant it, otherwise requests will not be approved.",
        "⚠️ Бота додано в «{title}», але в нього немає права «Запрошувати користувачів». Надайте його, інакше заявки не схвалюватимуться.",
    ),
}


def t(lang, key, **kw):
    v = S[key]
    s = v if isinstance(v, str) else v[LI.get(lang, 1)]
    return s.format(**kw) if kw else s


POOLS = {
    "fruit": {
        "🍎": ("яблоко", "apple", "яблуко"), "🍌": ("банан", "banana", "банан"),
        "🍇": ("виноград", "grapes", "виноград"), "🍓": ("клубника", "strawberry", "полуниця"),
        "🍉": ("арбуз", "watermelon", "кавун"), "🍊": ("апельсин", "orange", "апельсин"),
        "🍒": ("вишня", "cherry", "вишня"), "🍍": ("ананас", "pineapple", "ананас"),
        "🥝": ("киви", "kiwi", "ківі"), "🍑": ("персик", "peach", "персик"),
        "🍋": ("лимон", "lemon", "лимон"), "🍐": ("груша", "pear", "груша"),
    },
    "animal": {
        "🐶": ("собака", "dog", "собака"), "🐱": ("кот", "cat", "кіт"),
        "🐭": ("мышь", "mouse", "миша"), "🐰": ("кролик", "rabbit", "кролик"),
        "🦊": ("лиса", "fox", "лисиця"), "🐻": ("медведь", "bear", "ведмідь"),
        "🐼": ("панда", "panda", "панда"), "🐸": ("лягушка", "frog", "жаба"),
        "🐵": ("обезьяна", "monkey", "мавпа"), "🦁": ("лев", "lion", "лев"),
        "🐴": ("лошадь", "horse", "кінь"), "🐷": ("свинья", "pig", "свиня"),
    },
    "color": {
        "🟥": ("красный", "red", "червоний"), "🟦": ("синий", "blue", "синій"),
        "🟩": ("зелёный", "green", "зелений"), "🟨": ("жёлтый", "yellow", "жовтий"),
        "🟧": ("оранжевый", "orange", "помаранчевий"), "🟪": ("фиолетовый", "purple", "фіолетовий"),
        "⬛": ("чёрный", "black", "чорний"), "⬜": ("белый", "white", "білий"),
        "🟫": ("коричневый", "brown", "коричневий"),
    },
}


# ---------- мови ----------

def detect(code):
    c = (code or "").lower().split("-")[0]
    return c if c in LANGS else None


def get_lang(user):
    saved = ulang.get(user.id)
    if saved:
        return saved
    d = detect(user.language_code)
    if d:
        seen[user.id] = d
    return d


def lang_of(user):
    return get_lang(user) or "en"


def lang_by_id(uid):
    return ulang.get(uid) or seen.get(uid) or "en"


async def set_lang(uid, lang):
    ulang[uid] = lang
    await db.execute(
        "INSERT INTO user_lang(user_id, lang) VALUES($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET lang = EXCLUDED.lang",
        uid, lang,
    )


def picker_markup(prefix):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data=f"{prefix}:ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data=f"{prefix}:en"),
        InlineKeyboardButton(text="🇺🇦 Українська", callback_data=f"{prefix}:uk"),
    ]])


# ---------- база ----------

async def init_db():
    global db
    db = await asyncpg.create_pool(
        DATABASE_URL.split("?")[0],
        ssl="require",
        min_size=0,
        max_size=3,
        statement_cache_size=0,
        max_inactive_connection_lifetime=30,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id bigint PRIMARY KEY,
            title text NOT NULL DEFAULT '',
            ctype text NOT NULL DEFAULT '',
            enabled boolean NOT NULL DEFAULT true,
            mode text NOT NULL DEFAULT 'captcha',
            types text NOT NULL DEFAULT 'math,fruit,animal,color,count',
            timeout integer NOT NULL DEFAULT 5,
            cooldown integer NOT NULL DEFAULT 0,
            notify bigint NOT NULL DEFAULT 0,
            welcome text NOT NULL DEFAULT '',
            added_by bigint
        );
        CREATE TABLE IF NOT EXISTS chat_admins (
            chat_id bigint NOT NULL,
            user_id bigint NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS chat_blocks (
            chat_id bigint NOT NULL,
            user_id bigint NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS chat_stats (
            ts timestamptz NOT NULL,
            chat_id bigint NOT NULL,
            user_id bigint NOT NULL,
            result text NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chat_stats_idx ON chat_stats (chat_id, ts);
        CREATE TABLE IF NOT EXISTS user_lang (
            user_id bigint PRIMARY KEY,
            lang text NOT NULL
        );
        """
    )
    for r in await db.fetch("SELECT * FROM chats"):
        chats[r["chat_id"]] = dict(r)
    for r in await db.fetch("SELECT chat_id, user_id FROM chat_admins"):
        admins.setdefault(r["user_id"], set()).add(r["chat_id"])
    for r in await db.fetch("SELECT user_id, lang FROM user_lang"):
        ulang[r["user_id"]] = r["lang"]
    for r in await db.fetch("SELECT chat_id, user_id FROM chat_blocks"):
        blocks[(r["chat_id"], r["user_id"])] = None


async def upd(cid, **fields):
    chats[cid].update(fields)
    keys = list(fields)
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(keys))
    await db.execute(f"UPDATE chats SET {sets} WHERE chat_id = $1", cid, *[fields[k] for k in keys])


async def register_chat(chat, added_by=None):
    cid = chat.id
    title = chat.title or str(cid)
    ch = chats.get(cid)
    if ch:
        if ch["title"] != title:
            await upd(cid, title=title)
        return ch
    ch = {"chat_id": cid, "title": title, "ctype": chat.type, "added_by": added_by, **DEFAULT_CH}
    ch["notify"] = added_by or 0
    chats[cid] = ch
    await db.execute(
        "INSERT INTO chats(chat_id, title, ctype, notify, added_by) VALUES($1, $2, $3, $4, $5) "
        "ON CONFLICT (chat_id) DO NOTHING",
        cid, title, chat.type, ch["notify"], added_by,
    )
    return ch


async def forget_chat(cid):
    chats.pop(cid, None)
    for s in admins.values():
        s.discard(cid)
    for k in [k for k in blocks if k[0] == cid]:
        del blocks[k]
    for k in [k for k in pending if k[0] == cid]:
        del pending[k]
    for q in ("DELETE FROM chats WHERE chat_id = $1",
              "DELETE FROM chat_admins WHERE chat_id = $1",
              "DELETE FROM chat_blocks WHERE chat_id = $1"):
        await db.execute(q, cid)


async def add_admin(cid, uid):
    s = admins.setdefault(uid, set())
    if cid in s:
        return
    s.add(cid)
    try:
        await db.execute(
            "INSERT INTO chat_admins(chat_id, user_id) VALUES($1, $2) ON CONFLICT DO NOTHING",
            cid, uid,
        )
    except Exception:
        log.exception("add_admin failed")


async def drop_admin(cid, uid):
    admins.get(uid, set()).discard(cid)
    try:
        await db.execute("DELETE FROM chat_admins WHERE chat_id = $1 AND user_id = $2", cid, uid)
    except Exception:
        log.exception("drop_admin failed")


async def sync_admins(cid):
    try:
        lst = await bot.get_chat_administrators(cid)
    except TelegramAPIError:
        return
    ids = {m.user.id for m in lst if not m.user.is_bot}
    for uid in ids:
        await add_admin(cid, uid)
    for uid, s in list(admins.items()):
        if cid in s and uid not in ids:
            await drop_admin(cid, uid)


async def block_forever(cid, uid):
    blocks[(cid, uid)] = None
    await db.execute(
        "INSERT INTO chat_blocks(chat_id, user_id) VALUES($1, $2) ON CONFLICT DO NOTHING",
        cid, uid,
    )


async def unblock(cid, uid):
    blocks.pop((cid, uid), None)
    await db.execute("DELETE FROM chat_blocks WHERE chat_id = $1 AND user_id = $2", cid, uid)


# ---------- статистика (запис пачками, щоб не будити Neon) ----------

def record(cid, uid, result):
    stat_buf.append((datetime.now(timezone.utc), cid, uid, result))


async def flush_stats():
    async with flush_lock:
        if not stat_buf:
            return
        rows = stat_buf[:]
        del stat_buf[:len(rows)]
        try:
            await db.executemany(
                "INSERT INTO chat_stats(ts, chat_id, user_id, result) VALUES($1, $2, $3, $4)",
                rows,
            )
        except Exception:
            log.exception("flush failed")
            stat_buf[:0] = rows


async def flush_loop():
    last = time.monotonic()
    while True:
        await asyncio.sleep(30)
        try:
            if stat_buf and (len(stat_buf) >= FLUSH_MAX or time.monotonic() - last >= FLUSH_EVERY):
                await flush_stats()
                last = time.monotonic()
        except Exception:
            log.exception("flush loop")


# ---------- допоміжне ----------

def esc(s):
    return html.escape(s or "")


def st(m):
    return getattr(m.status, "value", m.status)


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def fmt_min(lang, v):
    if v == 0:
        return t(lang, "m_off")
    if v < 60:
        return t(lang, "m_min", v=v)
    return t(lang, "m_hour", v=v // 60)


def onoff(lang, v):
    return t(lang, "on" if v else "off")


def kinds_of(ch):
    return [k for k in ch["types"].split(",") if k in TYPES] or ["math"]


async def is_admin(cid, uid):
    c = admin_cache.get((cid, uid))
    if c and time.monotonic() - c[1] < 300:
        return c[0]
    try:
        m = await bot.get_chat_member(cid, uid)
        ok = st(m) in ("creator", "administrator")
    except TelegramAPIError:
        ok = False
    admin_cache[(cid, uid)] = (ok, time.monotonic())
    if ok:
        await add_admin(cid, uid)
    elif cid in admins.get(uid, ()):
        await drop_admin(cid, uid)
    return ok


async def try_approve(cid, uid):
    try:
        await bot.approve_chat_join_request(cid, uid)
        return True
    except TelegramBadRequest:
        return False


async def safe_decline(cid, uid):
    try:
        await bot.decline_chat_join_request(cid, uid)
    except TelegramAPIError:
        pass


async def notify(cid, uid, name, username, result):
    ch = chats.get(cid)
    if not ch or not ch["notify"]:
        return
    to = ch["notify"]
    lang = lang_by_id(to)
    uname = f"@{esc(username)}" if username else t(lang, "n_nouser")
    text = (
        f"{t(lang, 'n_' + result)}\n"
        f'👤 <a href="tg://user?id={uid}">{esc(name)}</a> ({uname}, <code>{uid}</code>)\n'
        f"📣 {esc(ch['title'])}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn(t(lang, "b_block"), f"bl:{cid}:{uid}")]])
    try:
        await bot.send_message(to, text, reply_markup=kb)
    except TelegramAPIError:
        pass


# ---------- капчі ----------

def build_captcha(kind, lang):
    li = LI.get(lang, 1)
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
        return t(lang, "q_math", a=a, op=op, b=b), [(str(o), o == ans) for o in options]

    if kind in POOLS:
        items = random.sample(list(POOLS[kind].items()), 4)
        emoji, names = random.choice(items)
        key = "q_color" if kind == "color" else "q_pick"
        return t(lang, key, n=names[li]), [(e, e == emoji) for e, _ in items]

    emoji = random.choice(list(POOLS["fruit"]))
    n = random.randint(2, 7)
    options = {n}
    while len(options) < 4:
        w = n + random.randint(-3, 3)
        if 1 <= w <= 10:
            options.add(w)
    options = list(options)
    random.shuffle(options)
    return t(lang, "q_count", items=" ".join([emoji] * n)), [(str(o), o == n) for o in options]


def captcha_markup(cid, uid, options):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"c:{cid}:{uid}:{int(ok)}")
        for label, ok in options
    ]])


def captcha_message(ch, lang, cid, uid):
    q, opts = build_captcha(random.choice(kinds_of(ch)), lang)
    welcome = esc(ch["welcome"]) if ch["welcome"] else t(lang, "welcome_def")
    return f"{welcome}\n\n{q}", captcha_markup(cid, uid, opts)


# ---------- заявки на вступ ----------

@dp.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    cid, uid = req.chat.id, req.from_user.id
    ch = chats.get(cid)
    if ch is None:
        ch = await register_chat(req.chat)
    if not ch["enabled"]:
        return

    key = (cid, uid)
    if key in blocks:
        until = blocks[key]
        if until is None or until > datetime.now(timezone.utc):
            await safe_decline(cid, uid)
            record(cid, uid, "blocked")
            return
        del blocks[key]

    user = req.from_user
    if ch["mode"] == "auto":
        if await try_approve(cid, uid):
            record(cid, uid, "auto")
            await notify(cid, uid, user.full_name, user.username, "auto")
        return

    lang = get_lang(user)
    try:
        if lang:
            text, markup = captcha_message(ch, lang, cid, uid)
        else:
            text, markup = S["pick_lang"], picker_markup(f"cl:{cid}:{uid}")
        msg = await bot.send_message(req.user_chat_id, text, reply_markup=markup)
    except TelegramAPIError:
        log.warning("cannot message user %s", uid)
        return
    pending[key] = {
        "ucid": req.user_chat_id,
        "mid": msg.message_id,
        "t": time.monotonic(),
        "name": user.full_name,
        "username": user.username,
        "lang": lang,
    }


@dp.callback_query(F.data.startswith("cl:"))
async def on_applicant_lang(cb: CallbackQuery):
    _, cid, uid, lang = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    ch = chats.get(cid)
    if cb.from_user.id != uid or lang not in LANGS or not ch:
        await cb.answer()
        return
    ulang[uid] = lang
    key = (cid, uid)
    p = pending.get(key)
    if p:
        p["lang"] = lang
    else:
        pending[key] = {
            "ucid": cb.message.chat.id,
            "mid": cb.message.message_id,
            "t": time.monotonic(),
            "name": cb.from_user.full_name,
            "username": cb.from_user.username,
            "lang": lang,
        }
    text, markup = captcha_message(ch, lang, cid, uid)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramAPIError:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("c:"))
async def on_answer(cb: CallbackQuery):
    _, cid, uid, ok = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    if cb.from_user.id != uid:
        await cb.answer()
        return
    correct = ok == "1"

    if cid == 0:  # тестова капча адміна
        lang = lang_of(cb.from_user)
        try:
            await cb.message.edit_text(t(lang, "test_ok" if correct else "test_bad"))
        except Exception:
            pass
        await cb.answer()
        return

    info = pending.pop((cid, uid), None)
    lang = (info or {}).get("lang") or lang_of(cb.from_user)
    name = info["name"] if info else cb.from_user.full_name
    username = info["username"] if info else cb.from_user.username
    ch = chats.get(cid)

    result = None
    if correct:
        if await try_approve(cid, uid):
            text = t(lang, "ok_join")
            result = "approved"
        else:
            text = t(lang, "stale")
    else:
        await safe_decline(cid, uid)
        text = t(lang, "wrong")
        cd = ch["cooldown"] if ch else 0
        if cd > 0:
            if (cid, uid) not in blocks or blocks[(cid, uid)] is not None:
                blocks[(cid, uid)] = datetime.now(timezone.utc) + timedelta(minutes=cd)
            text += " " + t(lang, "wait", v=fmt_min(lang, cd))
        result = "declined"

    try:
        await cb.message.edit_text(text)
    except Exception:
        pass
    await cb.answer()

    if result:
        record(cid, uid, result)
        await notify(cid, uid, name, username, result)


async def timeout_loop():
    while True:
        await asyncio.sleep(15)
        try:
            if not pending:
                continue
            now = time.monotonic()
            for key, p in list(pending.items()):
                cid, uid = key
                ch = chats.get(cid)
                minutes = ch["timeout"] if ch else 5
                if minutes <= 0 or now - p["t"] < minutes * 60:
                    continue
                pending.pop(key, None)
                await safe_decline(cid, uid)
                record(cid, uid, "timeout")
                if ch and ch["cooldown"] > 0:
                    if key not in blocks or blocks[key] is not None:
                        blocks[key] = datetime.now(timezone.utc) + timedelta(minutes=ch["cooldown"])
                try:
                    await bot.edit_message_text(
                        t(p["lang"] or "en", "timeout_user"),
                        chat_id=p["ucid"],
                        message_id=p["mid"],
                    )
                except TelegramAPIError:
                    pass
                await notify(cid, uid, p["name"], p["username"], "timeout")
        except Exception:
            log.exception("timeout loop")


# ---------- підключення бота до каналів ----------

@dp.my_chat_member()
async def on_my_member(ev: ChatMemberUpdated):
    chat = ev.chat
    if chat.type == "private":
        return
    cid = chat.id
    status = st(ev.new_chat_member)

    if status == "administrator":
        who = ev.from_user.id if ev.from_user and not ev.from_user.is_bot else None
        ch = await register_chat(chat, who)
        await sync_admins(cid)
        if who:
            await add_admin(cid, who)
            can_invite = bool(getattr(ev.new_chat_member, "can_invite_users", False))
            lang = lang_by_id(who)
            try:
                await bot.send_message(
                    who,
                    t(lang, "connected" if can_invite else "no_rights", title=esc(ch["title"])),
                )
            except TelegramAPIError:
                pass
    elif status in ("left", "kicked"):
        if cid in chats:
            await forget_chat(cid)


# ---------- панель ----------

async def show(cb, text, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


def home_view(uid, lang):
    cids = [c for c in admins.get(uid, ()) if c in chats]
    rows = []
    for cid in sorted(cids, key=lambda c: chats[c]["title"].lower())[:25]:
        rows.append([btn("📣 " + chats[cid]["title"][:40], f"o:{cid}")])
    rows.append([InlineKeyboardButton(
        text=t(lang, "b_add_ch"),
        url=f"https://t.me/{BOT_USER}?startchannel=botstart&admin=invite_users",
    )])
    rows.append([InlineKeyboardButton(
        text=t(lang, "b_add_gr"),
        url=f"https://t.me/{BOT_USER}?startgroup=botstart&admin=invite_users",
    )])
    rows.append([btn(t(lang, "b_find"), "lc"), btn(t(lang, "b_lang"), "lg")])
    return t(lang, "home", n=len(cids)), InlineKeyboardMarkup(inline_keyboard=rows)


async def panel_view(cid, lang, uid):
    ch = chats[cid]
    warn = ""
    try:
        me = await bot.get_chat_member(cid, bot.id)
        if st(me) != "administrator" or not getattr(me, "can_invite_users", False):
            warn = t(lang, "warn_rights")
    except TelegramAPIError:
        pass
    mode = t(lang, "mode_captcha" if ch["mode"] == "captcha" else "mode_auto")
    text = t(
        lang, "panel",
        title=esc(ch["title"]), enabled=onoff(lang, ch["enabled"]), mode=mode,
        n=len(kinds_of(ch)), p=sum(1 for k in pending if k[0] == cid), warn=warn,
    )
    nf = ch["notify"]
    nf_label = t(lang, "off") if not nf else t(lang, "nf_me" if nf == uid else "nf_other")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(t(lang, "b_stats"), f"x:st:{cid}"), btn(t(lang, "b_types"), f"x:ty:{cid}")],
        [btn(t(lang, "b_bot", v=onoff(lang, ch["enabled"])), f"x:en:{cid}")],
        [btn(t(lang, "b_mode", v=mode), f"x:md:{cid}")],
        [btn(t(lang, "b_timeout", v=fmt_min(lang, ch["timeout"])), f"x:to:{cid}")],
        [btn(t(lang, "b_cool", v=fmt_min(lang, ch["cooldown"])), f"x:cd:{cid}")],
        [btn(t(lang, "b_notify", v=nf_label), f"x:nf:{cid}")],
        [btn(t(lang, "b_welcome"), f"x:wl:{cid}"), btn(t(lang, "b_test"), f"x:ts:{cid}")],
        [btn(t(lang, "b_bl"), f"x:bl:{cid}")],
        [btn(t(lang, "b_back"), "h")],
    ])
    return text, kb


def types_view(cid, lang):
    on = kinds_of(chats[cid])
    rows = [
        [btn(f"{'✅' if k in on else '⬜'} {t(lang, 'tn_' + k)}", f"x:tt:{cid}:{k}")]
        for k in TYPES
    ]
    rows.append([btn(t(lang, "b_back"), f"o:{cid}")])
    return t(lang, "types_title"), InlineKeyboardMarkup(inline_keyboard=rows)


async def stats_view(cid, lang):
    await flush_stats()
    now = datetime.now(timezone.utc)
    rows = await db.fetch(
        "SELECT result, "
        "count(*) FILTER (WHERE ts >= $2) AS d1, "
        "count(*) FILTER (WHERE ts >= $3) AS d7, "
        "count(*) AS al "
        "FROM chat_stats WHERE chat_id = $1 GROUP BY result",
        cid, now - timedelta(days=1), now - timedelta(days=7),
    )
    periods = {"d1": {}, "d7": {}, "al": {}}
    for r in rows:
        for p in periods:
            periods[p][r["result"]] = r[p]

    def line(label, c):
        ok = c.get("approved", 0) + c.get("auto", 0)
        return (
            f"<b>{label}</b>: ✅ {ok} · ❌ {c.get('declined', 0)} · "
            f"⌛ {c.get('timeout', 0)} · 🚫 {c.get('blocked', 0)}"
        )

    text = (
        f"{t(lang, 'stats_title')} — {esc(chats[cid]['title'])}\n\n"
        f"{line(t(lang, 'st_24h'), periods['d1'])}\n"
        f"{line(t(lang, 'st_7d'), periods['d7'])}\n"
        f"{line(t(lang, 'st_all'), periods['al'])}\n\n"
        f"{t(lang, 'st_legend')}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(t(lang, "b_refresh"), f"x:st:{cid}")],
        [btn(t(lang, "b_back"), f"o:{cid}")],
    ])
    return text, kb


def bl_view(cid, lang):
    now = datetime.now(timezone.utc)
    items = [
        (uid, until) for (c, uid), until in blocks.items()
        if c == cid and (until is None or until > now)
    ]
    rows = [
        [btn(t(lang, "b_unblock", i="⏳" if until else "🚫", id=uid), f"x:ub:{cid}:{uid}")]
        for uid, until in items[:10]
    ]
    rows.append([btn(t(lang, "b_back"), f"o:{cid}")])
    return t(lang, "bl_title", n=len(items)), InlineKeyboardMarkup(inline_keyboard=rows)


async def guard(cb, cid, lang):
    if cid in chats and await is_admin(cid, cb.from_user.id):
        return True
    await cb.answer(t(lang, "no_access"), show_alert=True)
    return False


@dp.message(Command("start", "admin"), F.chat.type == "private")
async def cmd_start(m: Message):
    awaiting.pop(m.from_user.id, None)
    lang = get_lang(m.from_user)
    if not lang:
        await m.answer(S["pick_lang"], reply_markup=picker_markup("sl"))
        return
    text, kb = home_view(m.from_user.id, lang)
    await m.answer(text, reply_markup=kb)


@dp.message(Command("ban"), F.chat.type == "private")
async def cmd_ban(m: Message, command: CommandObject):
    uid, lang = m.from_user.id, lang_of(m.from_user)
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await m.answer(t(lang, "ban_usage"))
        return
    cid = sel.get(uid)
    if not cid or not await is_admin(cid, uid):
        await m.answer(t(lang, "no_sel"))
        return
    await block_forever(cid, int(arg))
    await m.answer(t(lang, "banned", id=arg))


@dp.message(Command("unban"), F.chat.type == "private")
async def cmd_unban(m: Message, command: CommandObject):
    uid, lang = m.from_user.id, lang_of(m.from_user)
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await m.answer(t(lang, "ban_usage").replace("/ban", "/unban"))
        return
    cid = sel.get(uid)
    if not cid or not await is_admin(cid, uid):
        await m.answer(t(lang, "no_sel"))
        return
    await unblock(cid, int(arg))
    await m.answer(t(lang, "unbanned", id=arg))


@dp.message(Command("owner"), F.from_user.id == OWNER_ID, F.chat.type == "private")
async def cmd_owner(m: Message):
    await m.answer(
        f"📈 Канали: {len(chats)} (увімкнено: {sum(1 for c in chats.values() if c['enabled'])})\n"
        f"Адмінів: {len(admins)}\n"
        f"Чекають капчі: {len(pending)}\n"
        f"Блокувань: {len(blocks)}\n"
        f"Статистика в буфері: {len(stat_buf)}\n"
        f"Аптайм: {int((time.time() - STARTED) / 3600)} год"
    )


def waiting_text(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id in awaiting and m.text and not m.text.startswith("/"))


@dp.message(F.chat.type == "private", waiting_text)
async def on_welcome_text(m: Message):
    uid, lang = m.from_user.id, lang_of(m.from_user)
    cid = awaiting.get(uid)
    text = m.text.strip()
    if len(text) > 300:
        await m.answer(t(lang, "w_long"))
        return
    awaiting.pop(uid, None)
    if cid not in chats or not await is_admin(cid, uid):
        await m.answer(t(lang, "no_access"))
        return
    await upd(cid, welcome=text)
    await m.answer(t(lang, "w_saved"))
    panel, kb = await panel_view(cid, lang, uid)
    await m.answer(panel, reply_markup=kb)


@dp.callback_query(F.data.startswith("sl:"))
async def on_set_lang(cb: CallbackQuery):
    lang = cb.data.split(":")[1]
    if lang not in LANGS:
        await cb.answer()
        return
    await set_lang(cb.from_user.id, lang)
    text, kb = home_view(cb.from_user.id, lang)
    await show(cb, text, kb)
    await cb.answer(t(lang, "lang_set"))


@dp.callback_query(F.data == "lg")
async def on_lang_menu(cb: CallbackQuery):
    await show(cb, S["pick_lang"], picker_markup("sl"))
    await cb.answer()


@dp.callback_query(F.data == "h")
async def on_home(cb: CallbackQuery):
    awaiting.pop(cb.from_user.id, None)
    text, kb = home_view(cb.from_user.id, lang_of(cb.from_user))
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "lc")
async def on_find(cb: CallbackQuery):
    uid = cb.from_user.id
    todo = [c for c in chats if c not in admins.get(uid, ())][:40]
    if todo:
        await asyncio.gather(*(is_admin(c, uid) for c in todo))
    text, kb = home_view(uid, lang_of(cb.from_user))
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("o:"))
async def on_open(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    uid, lang = cb.from_user.id, lang_of(cb.from_user)
    if not await guard(cb, cid, lang):
        return
    awaiting.pop(uid, None)
    sel[uid] = cid
    text, kb = await panel_view(cid, lang, uid)
    await show(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("bl:"))
async def on_block(cb: CallbackQuery):
    _, cid, target = cb.data.split(":")
    cid, target = int(cid), int(target)
    lang = lang_of(cb.from_user)
    if not await is_admin(cid, cb.from_user.id):
        await cb.answer(t(lang, "no_access"), show_alert=True)
        return
    await block_forever(cid, target)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer(t(lang, "blocked_toast"))


@dp.callback_query(F.data.startswith("x:"))
async def on_action(cb: CallbackQuery):
    parts = cb.data.split(":")
    act, cid = parts[1], int(parts[2])
    arg = parts[3] if len(parts) > 3 else None
    uid, lang = cb.from_user.id, lang_of(cb.from_user)
    if not await guard(cb, cid, lang):
        return
    ch = chats[cid]
    sel[uid] = cid
    if act != "wl":
        awaiting.pop(uid, None)
    screen = "main"

    if act == "en":
        await upd(cid, enabled=not ch["enabled"])
    elif act == "md":
        await upd(cid, mode="auto" if ch["mode"] == "captcha" else "captcha")
    elif act in ("to", "cd"):
        key = "timeout" if act == "to" else "cooldown"
        vals, cur = CYCLES[key], ch[key]
        nxt = vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]
        await upd(cid, **{key: nxt})
    elif act == "nf":
        await upd(cid, notify=0 if ch["notify"] == uid else uid)
    elif act == "wr":
        await upd(cid, welcome="")
    elif act == "ty":
        screen = "types"
    elif act == "tt":
        on = kinds_of(ch)
        if arg in on:
            if len(on) == 1:
                await cb.answer(t(lang, "min_one"), show_alert=True)
                return
            on.remove(arg)
        elif arg in TYPES:
            on.append(arg)
        await upd(cid, types=",".join(k for k in TYPES if k in on))
        screen = "types"
    elif act == "st":
        screen = "stats"
    elif act == "bl":
        screen = "bl"
    elif act == "ub":
        await unblock(cid, int(arg))
        screen = "bl"
    elif act == "ts":
        kind = random.choice(kinds_of(ch))
        q, opts = build_captcha(kind, lang)
        await bot.send_message(
            uid,
            t(lang, "test_head", kn=t(lang, "tn_" + kind), q=q),
            reply_markup=captcha_markup(0, uid, opts),
        )
        await cb.answer(t(lang, "test_sent"))
        return
    elif act == "wl":
        awaiting[uid] = cid
        cur = esc(ch["welcome"]) if ch["welcome"] else t(lang, "welcome_def")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn(t(lang, "b_default"), f"x:wr:{cid}"), btn(t(lang, "b_cancel"), f"o:{cid}")],
        ])
        await show(cb, t(lang, "w_prompt", cur=cur), kb)
        await cb.answer()
        return

    if screen == "types":
        text, kb = types_view(cid, lang)
    elif screen == "stats":
        text, kb = await stats_view(cid, lang)
    elif screen == "bl":
        text, kb = bl_view(cid, lang)
    else:
        text, kb = await panel_view(cid, lang, uid)
    await show(cb, text, kb)
    await cb.answer()


# ---------- запуск ----------

async def on_startup(bot: Bot):
    global BOT_USER
    await init_db()
    BOT_USER = (await bot.get_me()).username
    await bot.set_webhook(
        f"{BASE_URL}/webhook",
        secret_token=SECRET,
        allowed_updates=["message", "callback_query", "chat_join_request", "my_chat_member"],
        drop_pending_updates=False,
    )
    background.append(asyncio.create_task(timeout_loop()))
    background.append(asyncio.create_task(flush_loop()))


async def on_shutdown(bot: Bot):
    for task in background:
        task.cancel()
    if db:
        await flush_stats()
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
