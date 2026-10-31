import asyncio
import hashlib
import html
import io
import logging
import os
import random
import re
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
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from collections import deque

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except Exception:  # Pillow не встановлено — буде текстова статистика
    PIL_OK = False

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
RAID_N = int(os.getenv("RAID_N", "25"))   # стільки заявок за хвилину = рейд
RAID_WINDOW = 60
RAID_MINUTES = 15

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

LANGS = ("ru", "en", "uk")
LI = {"ru": 0, "en": 1, "uk": 2}
TYPES = ("math", "fruit", "animal", "color", "count")
CYCLES = {"timeout": [0, 3, 5, 10, 30], "cooldown": [0, 5, 30, 60, 1440], "max_tries": [1, 2, 3, 5]}
CYC = {"to": "timeout", "cd": "cooldown", "tr": "max_tries"}
DEFAULT_CH = {
    "enabled": True,
    "mode": "captcha",
    "types": ",".join(TYPES),
    "timeout": 5,
    "cooldown": 0,
    "notify": 0,
    "welcome": "",
    "notify_mode": "owner",
    "max_tries": 1,
    "raid_guard": True,
    "weekly": False,
    "last_report": None,
    "footer": False,
    "footer_link": "",
    "footer_text": "",
    "footer_sep": "─" * 18,
    "footer_pre": "🔗 ",
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
test_fails = {}   # (chat_id, user_id) -> помилки в тесті капчі
stat_buf = []
background = []
flush_lock = asyncio.Lock()
raid = {}         # chat_id -> {times, until}

# ---------- тексти: (ru, en, uk) ----------
S = {
    "pick_lang": "🌐 Выберите язык · Choose your language · Оберіть мову",
    "lang_set": ("✅ Язык: русский", "✅ Language: English", "✅ Мова: українська"),
    "home": (
        "🛡 <b>Captcha</b>\nЗащита заявок на вступление\n\n📣 Ваши каналы и группы: <b>{n}</b>\n\n🟢 защита включена · 🔴 выключена",
        "🛡 <b>Captcha</b>\nJoin request protection\n\n📣 Your channels and groups: <b>{n}</b>\n\n🟢 protection on · 🔴 off",
        "🛡 <b>Captcha</b>\nЗахист заявок на вступ\n\n📣 Ваші канали та групи: <b>{n}</b>\n\n🟢 захист увімкнено · 🔴 вимкнено",
    ),
    "home_empty": (
        "🛡 <b>Captcha</b>\nЗащита заявок на вступление\n\n<b>Как начать</b>\n1️⃣ Нажмите «➕ Канал» или «➕ Группа»\n2️⃣ Сделайте меня администратором с правом «Приглашать пользователей»\n3️⃣ В пригласительной ссылке включите одобрение заявок администратором\n\nПосле этого канал появится здесь.",
        "🛡 <b>Captcha</b>\nJoin request protection\n\n<b>How to start</b>\n1️⃣ Tap “➕ Channel” or “➕ Group”\n2️⃣ Make me an admin with the “Invite users” right\n3️⃣ In the invite link, turn on admin approval\n\nThe channel will then appear here.",
        "🛡 <b>Captcha</b>\nЗахист заявок на вступ\n\n<b>Як почати</b>\n1️⃣ Натисніть «➕ Канал» або «➕ Група»\n2️⃣ Зробіть мене адміністратором із правом «Запрошувати користувачів»\n3️⃣ У запрошувальному посиланні увімкніть схвалення заявок адміністратором\n\nПісля цього канал з'явиться тут.",
    ),
    "b_add_ch": ("➕ Канал", "➕ Channel", "➕ Канал"),
    "b_add_gr": ("➕ Группа", "➕ Group", "➕ Група"),
    "b_find": ("🔄 Найти каналы", "🔄 Find channels", "🔄 Знайти канали"),
    "b_lang": ("🌐 Язык", "🌐 Language", "🌐 Мова"),
    "b_back": ("◀️ Назад", "◀️ Back", "◀️ Назад"),
    "b_refresh": ("🔄 Обновить", "🔄 Refresh", "🔄 Оновити"),
    "no_access": ("Нет доступа", "No access", "Немає доступу"),
    "panel": (
        "🛡 <b>{title}</b>\n──────────────\n{status}\n\n🎯 Режим: <b>{mode}</b>\n⏱ Время на ответ: <b>{timeout}</b>\n🔁 Попыток: <b>{tries}</b>\n🔔 Уведомления: <b>{notify}</b>\n⏳ Ждут ответа: <b>{p}</b>{warn}",
        "🛡 <b>{title}</b>\n──────────────\n{status}\n\n🎯 Mode: <b>{mode}</b>\n⏱ Time limit: <b>{timeout}</b>\n🔁 Attempts: <b>{tries}</b>\n🔔 Notifications: <b>{notify}</b>\n⏳ Waiting for an answer: <b>{p}</b>{warn}",
        "🛡 <b>{title}</b>\n──────────────\n{status}\n\n🎯 Режим: <b>{mode}</b>\n⏱ Час на відповідь: <b>{timeout}</b>\n🔁 Спроб: <b>{tries}</b>\n🔔 Сповіщення: <b>{notify}</b>\n⏳ Чекають відповіді: <b>{p}</b>{warn}",
    ),
    "pr_on": ("🟢 <b>Защита включена</b>", "🟢 <b>Protection is on</b>", "🟢 <b>Захист увімкнено</b>"),
    "pr_off": (
        "🔴 <b>Защита выключена</b> — заявки не проверяются",
        "🔴 <b>Protection is off</b> — requests are not checked",
        "🔴 <b>Захист вимкнено</b> — заявки не перевіряються",
    ),
    "warn_rights": (
        "\n\n⚠️ У бота нет права «Приглашать пользователей». Выдайте его в настройках администраторов, иначе я не смогу одобрять заявки.",
        "\n\n⚠️ The bot lacks the “Invite users” admin right. Grant it in the admin settings, otherwise I cannot approve requests.",
        "\n\n⚠️ У бота немає права «Запрошувати користувачів». Надайте його в налаштуваннях адміністраторів, інакше я не зможу схвалювати заявки.",
    ),
    "mode_captcha": ("Капча", "Captcha", "Капча"),
    "mode_auto": ("Принимать всех", "Accept everyone", "Приймати всіх"),
    "nm_all": ("👥 Всем админам", "👥 All admins", "👥 Усім адмінам"),
    "nm_owner": ("👤 Только владельцу", "👤 Owner only", "👤 Лише власнику"),
    "nm_off": ("🔕 Выключены", "🔕 Off", "🔕 Вимкнено"),
    "b_pause": ("⏸ Выключить защиту", "⏸ Turn protection off", "⏸ Вимкнути захист"),
    "b_resume": ("▶️ Включить защиту", "▶️ Turn protection on", "▶️ Увімкнути захист"),
    "b_stats": ("📊 Статистика", "📊 Statistics", "📊 Статистика"),
    "b_types": ("🧩 Типы капчи", "🧩 Captcha types", "🧩 Типи капчі"),
    "b_settings": ("⚙️ Настройки", "⚙️ Settings", "⚙️ Налаштування"),
    "b_welcome": ("✏️ Приветствие", "✏️ Greeting", "✏️ Привітання"),
    "b_test": ("🧪 Тест капчи", "🧪 Test captcha", "🧪 Тест капчі"),
    "b_bl": ("🚫 Чёрный список", "🚫 Blacklist", "🚫 Чорний список"),
    "b_mode": ("🎯 Режим: {v}", "🎯 Mode: {v}", "🎯 Режим: {v}"),
    "b_timeout": ("⏱ Время на ответ: {v}", "⏱ Time limit: {v}", "⏱ Час на відповідь: {v}"),
    "b_tries": ("🔁 Попыток: {v}", "🔁 Attempts: {v}", "🔁 Спроб: {v}"),
    "b_cool": ("🧊 Пауза после отказа: {v}", "🧊 Pause after a decline: {v}", "🧊 Пауза після відмови: {v}"),
    "b_notify": ("🔔 Уведомления: {v}", "🔔 Notifications: {v}", "🔔 Сповіщення: {v}"),
    "m_off": ("выкл", "off", "вимк"),
    "m_min": ("{v} мин", "{v} min", "{v} хв"),
    "m_hour": ("{v} ч", "{v} h", "{v} год"),
    "set_title": (
        "⚙️ <b>Настройки</b>\n📣 {title}\n\n🎯 <b>Режим</b> — капча или приём без проверки\n⏱ <b>Время</b> — после него заявка отклоняется\n🔁 <b>Попытки</b> — сколько раз можно ошибиться (больше попыток — слабее защита)\n🧊 <b>Пауза</b> — как долго нельзя подать заявку снова\n🔔 <b>Уведомления</b> — кто получает отчёты\n\nНажмите на пункт, чтобы переключить значение.",
        "⚙️ <b>Settings</b>\n📣 {title}\n\n🎯 <b>Mode</b> — captcha or accept without a check\n⏱ <b>Time</b> — the request is declined after it\n🔁 <b>Attempts</b> — how many mistakes are allowed (more attempts — weaker protection)\n🧊 <b>Pause</b> — how long a person cannot apply again\n🔔 <b>Notifications</b> — who receives reports\n\nTap an item to switch its value.",
        "⚙️ <b>Налаштування</b>\n📣 {title}\n\n🎯 <b>Режим</b> — капча або приймання без перевірки\n⏱ <b>Час</b> — після нього заявку відхиляють\n🔁 <b>Спроби</b> — скільки разів можна помилитись (більше спроб — слабший захист)\n🧊 <b>Пауза</b> — як довго не можна подати заявку знову\n🔔 <b>Сповіщення</b> — хто отримує звіти\n\nНатисніть на пункт, щоб перемкнути значення.",
    ),
    "nf_title": (
        "🔔 <b>Уведомления</b>\n📣 {title}\n\nКто получает сообщение о каждой заявке: принята, отклонена или без ответа.\n\n<i>Сообщение получат только те, кто хотя бы раз запускал бота.</i>",
        "🔔 <b>Notifications</b>\n📣 {title}\n\nWho receives a message about each request: accepted, declined or no answer.\n\n<i>Only people who have started the bot at least once can receive them.</i>",
        "🔔 <b>Сповіщення</b>\n📣 {title}\n\nХто отримує повідомлення про кожну заявку: прийнято, відхилено або без відповіді.\n\n<i>Отримати їх можуть лише ті, хто хоч раз запускав бота.</i>",
    ),
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
    "st_all": ("Всё время", "All time", "Весь час"),
    "st_total": ("Всего заявок: <b>{n}</b>", "Total requests: <b>{n}</b>", "Усього заявок: <b>{n}</b>"),
    "st_empty": ("За этот период заявок пока нет.", "No requests in this period yet.", "За цей період заявок поки немає."),
    "sl_ok": ("Принято", "Accepted", "Прийнято"),
    "sl_dec": ("Отклонено", "Declined", "Відхилено"),
    "sl_to": ("Без ответа", "No answer", "Без відповіді"),
    "sl_bl": ("Заблокировано", "Blocked", "Заблоковано"),
    "bl_title": (
        "🚫 <b>Чёрный список</b>: {n}\n\n🚫 — навсегда, ⏳ — временная пауза после отказа.\nДобавить вручную: <code>/ban ID</code>",
        "🚫 <b>Blacklist</b>: {n}\n\n🚫 — permanent, ⏳ — temporary pause after a decline.\nAdd manually: <code>/ban ID</code>",
        "🚫 <b>Чорний список</b>: {n}\n\n🚫 — назавжди, ⏳ — тимчасова пауза після відмови.\nДодати вручну: <code>/ban ID</code>",
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
    "h_time": ("⏱ На ответ: {v}", "⏱ Time limit: {v}", "⏱ Час на відповідь: {v}"),
    "h_tries": ("🔁 Попыток: {n}", "🔁 Attempts: {n}", "🔁 Спроб: {n}"),
    "retry": (
        "❌ Неверно. Осталось попыток: {k}",
        "❌ Wrong. Attempts left: {k}",
        "❌ Неправильно. Залишилось спроб: {k}",
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

S.update({
    "img_title": ("Статистика", "Statistics", "Статистика"),
    "img_for": ("заявок · {p}", "requests · {p}", "заявок · {p}"),
    "img_acc": ("принято", "accepted", "прийнято"),
    "img_vs": ("к прошлому", "vs previous", "до минулого"),
    "img_upd": ("Обновлено {hm} UTC", "Updated {hm} UTC", "Оновлено {hm} UTC"),
    "img_c_d": ("Последние 24 часа (часов назад)", "Last 24 hours (hours ago)", "Останні 24 години (годин тому)"),
    "img_c_w": ("Последние 7 дней", "Last 7 days", "Останні 7 днів"),
    "img_c_a": ("Последние 8 недель", "Last 8 weeks", "Останні 8 тижнів"),
    "v_on": ("вкл", "on", "увімк"),
    "v_off": ("выкл", "off", "вимк"),
    "b_raid": ("🚨 Антирейд: {v}", "🚨 Anti-raid: {v}", "🚨 Антирейд: {v}"),
    "b_weekly": ("📬 Недельный отчёт: {v}", "📬 Weekly report: {v}", "📬 Тижневий звіт: {v}"),
    "set_title": (
        "⚙️ <b>Настройки</b>\n📣 {title}\n\n🎯 <b>Режим</b> — капча или приём без проверки\n⏱ <b>Время</b> — после него заявка отклоняется\n🔁 <b>Попытки</b> — сколько раз можно ошибиться (больше попыток — слабее защита)\n🧊 <b>Пауза</b> — как долго нельзя подать заявку снова\n🚨 <b>Антирейд</b> — при наплыве заявок включает строгий режим\n📬 <b>Отчёт</b> — по понедельникам картинка со статистикой\n🔔 <b>Уведомления</b> — кто получает сообщения\n\nНажмите на пункт, чтобы переключить значение.",
        "⚙️ <b>Settings</b>\n📣 {title}\n\n🎯 <b>Mode</b> — captcha or accept without a check\n⏱ <b>Time</b> — the request is declined after it\n🔁 <b>Attempts</b> — how many mistakes are allowed (more attempts — weaker protection)\n🧊 <b>Pause</b> — how long a person cannot apply again\n🚨 <b>Anti-raid</b> — turns on a strict mode when requests flood in\n📬 <b>Report</b> — a statistics picture every Monday\n🔔 <b>Notifications</b> — who receives messages\n\nTap an item to switch its value.",
        "⚙️ <b>Налаштування</b>\n📣 {title}\n\n🎯 <b>Режим</b> — капча або приймання без перевірки\n⏱ <b>Час</b> — після нього заявку відхиляють\n🔁 <b>Спроби</b> — скільки разів можна помилитись (більше спроб — слабший захист)\n🧊 <b>Пауза</b> — як довго не можна подати заявку знову\n🚨 <b>Антирейд</b> — при напливі заявок вмикає суворий режим\n📬 <b>Звіт</b> — щопонеділка картинка зі статистикою\n🔔 <b>Сповіщення</b> — хто отримує повідомлення\n\nНатисніть на пункт, щоб перемкнути значення.",
    ),
    "raid_alert": (
        "🚨 <b>Антирейд</b> · {title}\nЗа минуту пришло заявок: <b>{n}</b>. На {m} мин включён строгий режим: одна попытка, до 2 мин на ответ, пауза 30 мин после ошибки.\n\nОтключить защиту можно в настройках канала.",
        "🚨 <b>Anti-raid</b> · {title}\nRequests in the last minute: <b>{n}</b>. A strict mode is on for {m} min: one attempt, up to 2 min to answer, 30 min pause after a mistake.\n\nYou can turn this protection off in the channel settings.",
        "🚨 <b>Антирейд</b> · {title}\nЗа хвилину надійшло заявок: <b>{n}</b>. На {m} хв увімкнено суворий режим: одна спроба, до 2 хв на відповідь, пауза 30 хв після помилки.\n\nВимкнути захист можна в налаштуваннях каналу.",
    ),
    "raid_on": (
        "\n\n🚨 Активен режим антирейда ещё {m} мин.",
        "\n\n🚨 Anti-raid mode is active for {m} more min.",
        "\n\n🚨 Активний режим антирейду ще {m} хв.",
    ),
    "weekly_head": ("📬 <b>Недельный отчёт</b>", "📬 <b>Weekly report</b>", "📬 <b>Тижневий звіт</b>"),
})


S.update({
    "b_help": ("❓ Подробнее", "❓ Explain", "❓ Докладно"),
    "test_note": (
        "🧪 Это был тест — настоящие заявки не затронуты.",
        "🧪 This was a test — real requests are not affected.",
        "🧪 Це був тест — справжні заявки не змінено.",
    ),
    "bn_shield": ("Защита заявок", "Join request protection", "Захист заявок"),
    "bn_gear": ("Настройки", "Settings", "Налаштування"),
    "bn_grid": ("Типы капчи", "Captcha types", "Типи капчі"),
    "bn_bell": ("Уведомления", "Notifications", "Сповіщення"),
    "bn_ban": ("Чёрный список", "Blacklist", "Чорний список"),
    "bn_bubble": ("Приветствие", "Greeting", "Привітання"),
    "set_title": (
        "⚙️ <b>Настройки</b>\n📣 {title}\n\n"
        "🎯 <b>Режим</b> — проверять заявки капчей или принимать всех без проверки\n"
        "⏱ <b>Время</b> — сколько есть на ответ, потом заявка отклоняется\n"
        "🔁 <b>Попытки</b> — сколько раз можно ошибиться\n"
        "🧊 <b>Пауза</b> — как долго нельзя подать новую заявку после отказа\n"
        "🚨 <b>Антирейд</b> — если ботов вдруг приходит очень много, проверка на {m} мин становится строже\n"
        "📬 <b>Отчёт</b> — по понедельникам картинка со статистикой\n"
        "🔔 <b>Уведомления</b> — кто получает сообщения о заявках\n\n"
        "На кнопках — текущие значения. Нажмите, чтобы изменить. Кнопка «❓ Подробнее» объяснит всё с примерами.",
        "⚙️ <b>Settings</b>\n📣 {title}\n\n"
        "🎯 <b>Mode</b> — check applicants with a captcha or accept everyone without a check\n"
        "⏱ <b>Time</b> — how long a person has to answer, then the request is declined\n"
        "🔁 <b>Attempts</b> — how many mistakes are allowed\n"
        "🧊 <b>Pause</b> — how long a person cannot apply again after a decline\n"
        "🚨 <b>Anti-raid</b> — if a flood of bots suddenly arrives, the check gets stricter for {m} min\n"
        "📬 <b>Report</b> — a statistics picture every Monday\n"
        "🔔 <b>Notifications</b> — who receives messages about requests\n\n"
        "The buttons show current values. Tap one to change it. The “❓ Explain” button describes everything with examples.",
        "⚙️ <b>Налаштування</b>\n📣 {title}\n\n"
        "🎯 <b>Режим</b> — перевіряти заявки капчею чи приймати всіх без перевірки\n"
        "⏱ <b>Час</b> — скільки є часу на відповідь, потім заявку відхиляють\n"
        "🔁 <b>Спроби</b> — скільки разів можна помилитись\n"
        "🧊 <b>Пауза</b> — як довго не можна подати нову заявку після відмови\n"
        "🚨 <b>Антирейд</b> — якщо ботів раптом приходить дуже багато, перевірка на {m} хв стає суворішою\n"
        "📬 <b>Звіт</b> — щопонеділка картинка зі статистикою\n"
        "🔔 <b>Сповіщення</b> — хто отримує повідомлення про заявки\n\n"
        "На кнопках — поточні значення. Натисніть, щоб змінити. Кнопка «❓ Докладно» поясне все на прикладах.",
    ),
    "set_help": (
        "❓ <b>Настройки простыми словами</b>\n\n"
        "🎯 <b>Режим</b>\n"
        "<b>Капча</b> — человек, подавший заявку, получает в личные сообщения простое задание (пример, картинка и т. д.). Ответил верно — его принимают. Так отсеиваются боты.\n"
        "<b>Принимать всех</b> — все заявки одобряются сразу, без проверки.\n\n"
        "⏱ <b>Время на ответ</b>\n"
        "Сколько человек может отвечать на капчу. Время вышло — заявка отклоняется сама. Для обычного канала хватает 5 минут. «выкл» — время не ограничено, заявки могут висеть долго.\n\n"
        "🔁 <b>Попытки</b>\n"
        "Сколько раз можно ошибиться. При 1 попытке первая же ошибка отклоняет заявку. При 3 попытках можно ошибиться дважды. Больше попыток — удобнее живым людям, но и боту проще угадать.\n\n"
        "🧊 <b>Пауза после отказа</b>\n"
        "После отказа (ошибка или время вышло) человек не может сразу подать новую заявку: пока идёт пауза, бот отклоняет её без капчи. Это мешает ботам перебирать ответы снова и снова. «выкл» — пробовать можно сразу.\n\n"
        "🚨 <b>Антирейд</b>\n"
        "Рейд — это когда боты массово шлют заявки одновременно. Если за 1 минуту приходит {n} заявок или больше (в режиме капчи), бот на {m} минут включает строгий режим: только 1 попытка, до 2 минут на ответ и пауза 30 минут после ошибки. Вам сразу придёт сообщение. Когда время пройдёт, бот вернётся к вашим настройкам. Рекомендуем оставить включённым.\n\n"
        "📬 <b>Недельный отчёт</b>\n"
        "По понедельникам бот присылает картинку: сколько заявок пришло за неделю, сколько принято и отклонено. Получают те, кто выбран в уведомлениях; если они выключены — владелец канала.\n\n"
        "🔔 <b>Уведомления</b>\n"
        "Бот пишет в личные сообщения о каждой заявке: принят, отклонён или не ответил. Можно выбрать: всем админам, только владельцу или выключить. Чтобы получать сообщения, нужно хотя бы раз нажать /start в боте.",
        "❓ <b>Settings in plain words</b>\n\n"
        "🎯 <b>Mode</b>\n"
        "<b>Captcha</b> — a person who applies receives a simple task in a private message (a sum, a picture, etc.). A correct answer gets them accepted. This filters out bots.\n"
        "<b>Accept everyone</b> — all requests are approved at once, with no check.\n\n"
        "⏱ <b>Time limit</b>\n"
        "How long a person can take to answer the captcha. When time runs out the request is declined automatically. 5 minutes is enough for a normal channel. “off” means no limit, so requests can hang for a long time.\n\n"
        "🔁 <b>Attempts</b>\n"
        "How many mistakes are allowed. With 1 attempt the first mistake declines the request. With 3 attempts a person can be wrong twice. More attempts are friendlier to real people, but a bot can also guess more easily.\n\n"
        "🧊 <b>Pause after a decline</b>\n"
        "After a decline (a mistake or timeout) the person cannot apply again right away: while the pause lasts, the bot declines the new request without a captcha. This stops bots from trying answers again and again. “off” means they can retry immediately.\n\n"
        "🚨 <b>Anti-raid</b>\n"
        "A raid is when bots send many requests at the same time. If {n} or more requests arrive within 1 minute (in captcha mode), the bot turns on a strict mode for {m} minutes: only 1 attempt, up to 2 minutes to answer and a 30-minute pause after a mistake. You get a message right away. When the time is over, the bot returns to your settings. We recommend keeping it on.\n\n"
        "📬 <b>Weekly report</b>\n"
        "Every Monday the bot sends a picture: how many requests arrived this week, how many were accepted and declined. It goes to those chosen in notifications; if those are off, to the channel owner.\n\n"
        "🔔 <b>Notifications</b>\n"
        "The bot writes in private messages about every request: accepted, declined or no answer. You can choose all admins, owner only, or off. To receive them, a person must press /start in the bot at least once.",
        "❓ <b>Налаштування простими словами</b>\n\n"
        "🎯 <b>Режим</b>\n"
        "<b>Капча</b> — людина, яка подала заявку, отримує в особисті повідомлення просте завдання (приклад, картинка тощо). Відповіла правильно — її приймають. Так відсіюються боти.\n"
        "<b>Приймати всіх</b> — усі заявки схвалюються одразу, без перевірки.\n\n"
        "⏱ <b>Час на відповідь</b>\n"
        "Скільки людина може відповідати на капчу. Час минув — заявку відхиляють автоматично. Для звичайного каналу вистачає 5 хвилин. «вимк» — час не обмежений, заявки можуть висіти довго.\n\n"
        "🔁 <b>Спроби</b>\n"
        "Скільки разів можна помилитись. При 1 спробі перша ж помилка відхиляє заявку. При 3 спробах можна схибити двічі. Більше спроб — зручніше живим людям, але й боту легше вгадати.\n\n"
        "🧊 <b>Пауза після відмови</b>\n"
        "Після відмови (помилка чи вичерпаний час) людина не може одразу подати нову заявку: поки триває пауза, бот відхиляє її без капчі. Це заважає ботам перебирати відповіді знову й знову. «вимк» — пробувати можна одразу.\n\n"
        "🚨 <b>Антирейд</b>\n"
        "Рейд — це коли боти масово шлють заявки одночасно. Якщо за 1 хвилину надходить {n} заявок або більше (у режимі капчі), бот на {m} хвилин вмикає суворий режим: лише 1 спроба, до 2 хвилин на відповідь і пауза 30 хвилин після помилки. Вам одразу прийде повідомлення. Коли час мине, бот повернеться до ваших налаштувань. Рекомендуємо залишити ввімкненим.\n\n"
        "📬 <b>Тижневий звіт</b>\n"
        "Щопонеділка бот надсилає картинку: скільки заявок надійшло за тиждень, скільки прийнято й відхилено. Отримують ті, кого обрано в сповіщеннях; якщо вони вимкнені — власник каналу.\n\n"
        "🔔 <b>Сповіщення</b>\n"
        "Бот пише в особисті повідомлення про кожну заявку: прийнято, відхилено чи не відповіла. Можна обрати: усім адмінам, лише власнику або вимкнути. Щоб отримувати повідомлення, потрібно хоч раз натиснути /start у боті.",
    ),
    "nf_title": (
        "🔔 <b>Уведомления</b>\n📣 {title}\n\n"
        "Бот пишет в личные сообщения о каждой заявке: ✅ принят, ❌ отклонён или ⌛ не ответил. Там указано, кто подал заявку, и есть кнопка «Заблокировать».\n\n"
        "👥 <b>Всем админам</b> — получают все администраторы канала\n"
        "👤 <b>Только владельцу</b> — получает только владелец канала\n"
        "🔕 <b>Выключены</b> — сообщений нет\n\n"
        "<i>Сообщения придут только тем, кто хотя бы раз нажимал /start в этом боте.</i>",
        "🔔 <b>Notifications</b>\n📣 {title}\n\n"
        "The bot writes in private messages about every request: ✅ accepted, ❌ declined or ⌛ no answer. It shows who applied and has a “Block” button.\n\n"
        "👥 <b>All admins</b> — every channel administrator receives them\n"
        "👤 <b>Owner only</b> — only the channel owner receives them\n"
        "🔕 <b>Off</b> — no messages\n\n"
        "<i>Only people who have pressed /start in this bot at least once can receive them.</i>",
        "🔔 <b>Сповіщення</b>\n📣 {title}\n\n"
        "Бот пише в особисті повідомлення про кожну заявку: ✅ прийнято, ❌ відхилено або ⌛ не відповіла. Там видно, хто подав заявку, і є кнопка «Заблокувати».\n\n"
        "👥 <b>Усім адмінам</b> — отримують усі адміністратори каналу\n"
        "👤 <b>Лише власнику</b> — отримує лише власник каналу\n"
        "🔕 <b>Вимкнено</b> — повідомлень немає\n\n"
        "<i>Повідомлення прийдуть лише тим, хто хоч раз натискав /start у цьому боті.</i>",
    ),
    "types_title": (
        "🧩 <b>Типы капчи</b>\n\n"
        "➕ <b>Пример</b> — решить арифметику\n"
        "🍎 <b>Фрукты</b>, 🐶 <b>Животные</b>, 🎨 <b>Цвета</b> — найти нужную картинку по названию\n"
        "🔢 <b>Подсчёт</b> — сосчитать предметы\n\n"
        "Для каждой заявки бот выбирает случайный тип из включённых (✅). Чем больше типов, тем сложнее ботам. Нажмите на тип, чтобы включить или выключить его.",
        "🧩 <b>Captcha types</b>\n\n"
        "➕ <b>Math</b> — solve a sum\n"
        "🍎 <b>Fruits</b>, 🐶 <b>Animals</b>, 🎨 <b>Colors</b> — find the right picture by its name\n"
        "🔢 <b>Counting</b> — count the items\n\n"
        "For each request the bot picks a random type among the enabled ones (✅). The more types, the harder it is for bots. Tap a type to turn it on or off.",
        "🧩 <b>Типи капчі</b>\n\n"
        "➕ <b>Приклад</b> — розв'язати арифметику\n"
        "🍎 <b>Фрукти</b>, 🐶 <b>Тварини</b>, 🎨 <b>Кольори</b> — знайти потрібну картинку за назвою\n"
        "🔢 <b>Підрахунок</b> — порахувати предмети\n\n"
        "Для кожної заявки бот обирає випадковий тип серед увімкнених (✅). Чим більше типів, тим складніше ботам. Натисніть на тип, щоб увімкнути або вимкнути його.",
    ),
})


S.update({
    "b_footer": ("🔗 Ссылка в постах", "🔗 Link in posts", "🔗 Посилання в постах"),
    "b_ft_on": ("▶️ Включить", "▶️ Turn on", "▶️ Увімкнути"),
    "b_ft_off": ("⏸ Выключить", "⏸ Turn off", "⏸ Вимкнути"),
    "b_ft_text": ("✏️ Текст", "✏️ Text", "✏️ Текст"),
    "b_ft_sep": ("✏️ Линия", "✏️ Line", "✏️ Лінія"),
    "b_ft_styles": ("🎨 Стили", "🎨 Styles", "🎨 Стилі"),
    "b_ft_link": ("🔗 Своя ссылка", "🔗 Custom link", "🔗 Своє посилання"),
    "b_ft_auto": ("♻️ Авто-ссылка", "♻️ Auto link", "♻️ Авто-посилання"),
    "ft_default": ("Подписаться на канал", "Subscribe to the channel", "Підписатися на канал"),
    "ft_st_on": ("🟢 <b>Включено</b>", "🟢 <b>On</b>", "🟢 <b>Увімкнено</b>"),
    "ft_st_off": ("🔴 <b>Выключено</b>", "🔴 <b>Off</b>", "🔴 <b>Вимкнено</b>"),
    "ft_title": (
        "🔗 <b>Ссылка в постах</b>\n──────────────\n{status}\n\n"
        "В конец каждого нового поста бот добавляет ссылку для вступления. Если пост перешлют, по ссылке можно подать заявку.\n\n"
        "<b>Вид:</b>\n{preview}\n\n🔗 Ссылка: {link}{warn}",
        "🔗 <b>Link in posts</b>\n──────────────\n{status}\n\n"
        "The bot adds a join link to the end of every new post. If a post is forwarded, people can request to join through the link.\n\n"
        "<b>Look:</b>\n{preview}\n\n🔗 Link: {link}{warn}",
        "🔗 <b>Посилання в постах</b>\n──────────────\n{status}\n\n"
        "В кінець кожного нового допису бот додає посилання для вступу. Якщо допис перешлють, за посиланням можна подати заявку.\n\n"
        "<b>Вигляд:</b>\n{preview}\n\n🔗 Посилання: {link}{warn}",
    ),
    "ft_warn": (
        "\n\n⚠️ У бота нет права «Редактировать сообщения». Выдайте его в настройках администраторов канала, иначе ссылка не будет добавляться.",
        "\n\n⚠️ The bot lacks the “Edit messages” admin right. Grant it in the channel admin settings, otherwise the link will not be added.",
        "\n\n⚠️ У бота немає права «Редагувати повідомлення». Надайте його в налаштуваннях адміністраторів каналу, інакше посилання не додаватиметься.",
    ),
    "ft_rights": (
        "Нужны права «Приглашать пользователей» и «Редактировать сообщения». Выдайте их боту в настройках администраторов канала.",
        "The bot needs the “Invite users” and “Edit messages” rights. Grant them in the channel admin settings.",
        "Потрібні права «Запрошувати користувачів» і «Редагувати повідомлення». Надайте їх боту в налаштуваннях адміністраторів каналу.",
    ),
    "ft_err": (
        "Не удалось создать ссылку. Попробуйте ещё раз.",
        "Could not create the link. Please try again.",
        "Не вдалося створити посилання. Спробуйте ще раз.",
    ),
    "ft_prompt": (
        "✏️ Отправьте следующим сообщением новый текст ссылки (до 60 символов). Можно с эмодзи и красивыми буквами, например: Ｌｕｎｅｘ - подписаться\n\nСейчас: <b>{cur}</b>",
        "✏️ Send the new link text in your next message (up to 60 characters). Emoji and fancy letters work, e.g. Ｌｕｎｅｘ - subscribe\n\nNow: <b>{cur}</b>",
        "✏️ Надішліть наступним повідомленням новий текст посилання (до 60 символів). Можна з емодзі та гарними літерами, наприклад: Ｌｕｎｅｘ - підписатися\n\nЗараз: <b>{cur}</b>",
    ),
    "ft_sep_prompt": (
        "✏️ Отправьте следующим сообщением линию, которая будет над ссылкой (до 40 символов). Сама линия ссылкой не будет. Отправьте «-», чтобы убрать линию.\n\nСейчас: {cur}",
        "✏️ Send the line that goes above the link in your next message (up to 40 characters). The line itself is not a link. Send “-” to remove the line.\n\nNow: {cur}",
        "✏️ Надішліть наступним повідомленням лінію, яка буде над посиланням (до 40 символів). Сама лінія посиланням не буде. Надішліть «-», щоб прибрати лінію.\n\nЗараз: {cur}",
    ),
    "ft_link_prompt": (
        "🔗 Отправьте свою ссылку (начинается с https://). Чтобы заявки проходили капчу, в ссылке должно быть включено одобрение заявок администратором. Кнопка «Авто-ссылка» создаст такую ссылку сама.\n\nСейчас: {cur}",
        "🔗 Send your own link (starting with https://). For requests to pass the captcha, the link must have admin approval turned on. The “Auto link” button creates such a link for you.\n\nNow: {cur}",
        "🔗 Надішліть своє посилання (починається з https://). Щоб заявки проходили капчу, у посиланні має бути увімкнене схвалення заявок адміністратором. Кнопка «Авто-посилання» створить таке посилання сама.\n\nЗараз: {cur}",
    ),
    "ft_styles": (
        "🎨 <b>Стили оформления</b>\n\nВыберите готовый вид. Текст ссылки останется вашим, меняются только линия и значок. Линию и текст потом можно изменить вручную.\n\n{list}",
        "🎨 <b>Styles</b>\n\nPick a ready-made look. Your link text stays, only the line and the icon change. You can edit the line and the text by hand afterwards.\n\n{list}",
        "🎨 <b>Стилі оформлення</b>\n\nОберіть готовий вигляд. Текст посилання залишиться вашим, змінюються лише лінія та значок. Лінію й текст потім можна змінити вручну.\n\n{list}",
    ),
    "ft_long": (
        "Слишком длинно (максимум {n} символов). Попробуйте короче.",
        "Too long ({n} characters max). Try a shorter one.",
        "Занадто довго (максимум {n} символів). Спробуйте коротше.",
    ),
    "ft_link_bad": (
        "Это не похоже на ссылку. Она должна начинаться с https://",
        "That does not look like a link. It must start with https://",
        "Це не схоже на посилання. Воно має починатися з https://",
    ),
    "ft_saved": ("✅ Текст сохранён.", "✅ Text saved.", "✅ Текст збережено."),
    "ft_sep_saved": ("✅ Линия сохранена.", "✅ Line saved.", "✅ Лінію збережено."),
    "ft_link_saved": ("✅ Ссылка сохранена.", "✅ Link saved.", "✅ Посилання збережено."),
})


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
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS notify_mode text NOT NULL DEFAULT 'owner';
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS max_tries integer NOT NULL DEFAULT 1;
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS owner_id bigint;
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS raid_guard boolean NOT NULL DEFAULT true;
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS weekly boolean NOT NULL DEFAULT false;
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_report date;
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS footer boolean NOT NULL DEFAULT false;
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS footer_link text NOT NULL DEFAULT '';
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS footer_text text NOT NULL DEFAULT '';
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS footer_sep text NOT NULL DEFAULT '──────────────────';
        ALTER TABLE chats ADD COLUMN IF NOT EXISTS footer_pre text NOT NULL DEFAULT '🔗 ';
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
    ch = {"chat_id": cid, "title": title, "ctype": chat.type, "added_by": added_by, "owner_id": None, **DEFAULT_CH}
    chats[cid] = ch
    await db.execute(
        "INSERT INTO chats(chat_id, title, ctype, added_by) VALUES($1, $2, $3, $4) "
        "ON CONFLICT (chat_id) DO NOTHING",
        cid, title, chat.type, added_by,
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
    for m in lst:
        if st(m) == "creator" and not m.user.is_bot and cid in chats:
            if chats[cid].get("owner_id") != m.user.id:
                await upd(cid, owner_id=m.user.id)
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


# ---------- оформлення: ілюстрації (1280x720 JPG поруч з bot.py) ----------
DIR = os.path.dirname(os.path.abspath(__file__))


def img(name):
    return FSInputFile(os.path.join(DIR, f"{name}.jpg"))


async def edit_card(chat_id, mid, key, text, kb=None):
    """Міняє картинку й підпис у повідомленні; для старих текстових повідомлень — запасний варіант."""
    try:
        await bot.edit_message_media(
            chat_id=chat_id,
            message_id=mid,
            media=InputMediaPhoto(media=img(key), caption=text, parse_mode=ParseMode.HTML),
            reply_markup=kb,
        )
        return True
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return True
    except TelegramAPIError:
        return False
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=mid, reply_markup=kb)
        return True
    except TelegramAPIError:
        return False


async def show_home(cb, text, kb):
    """Головне меню з ілюстрацією зверху."""
    msg = cb.message
    if getattr(msg, "photo", None):
        try:
            await msg.edit_media(media=InputMediaPhoto(media=img("menu"), caption=text, parse_mode=ParseMode.HTML), reply_markup=kb)
        except TelegramBadRequest:
            pass
        return
    try:
        await msg.delete()
    except TelegramAPIError:
        pass
    await bot.send_photo(msg.chat.id, img("menu"), caption=text, reply_markup=kb)


def fmt_min(lang, v):
    if v == 0:
        return t(lang, "m_off")
    if v < 60:
        return t(lang, "m_min", v=v)
    return t(lang, "m_hour", v=v // 60)


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


def recipients(ch):
    mode = ch.get("notify_mode", "owner")
    cid = ch["chat_id"]
    if mode == "off":
        return []
    if mode == "all":
        return [u for u, s in admins.items() if cid in s]
    uid = ch.get("owner_id") or ch.get("added_by")
    return [uid] if uid else []


async def notify(cid, uid, name, username, result):
    ch = chats.get(cid)
    if not ch or ch.get("notify_mode") == "off":
        return
    if ch.get("notify_mode", "owner") == "owner" and not (ch.get("owner_id") or ch.get("added_by")):
        await sync_admins(cid)
    for to in recipients(ch):
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


# ---------- антирейд ----------

def raid_hit(cid):
    """Фіксує заявку; повертає кількість заявок за хвилину, якщо щойно почався рейд."""
    r = raid.setdefault(cid, {"times": deque(), "until": 0.0})
    now = time.monotonic()
    r["times"].append(now)
    while r["times"] and now - r["times"][0] > RAID_WINDOW:
        r["times"].popleft()
    if len(r["times"]) >= RAID_N and r["until"] <= now:
        r["until"] = now + RAID_MINUTES * 60
        return len(r["times"])
    return 0


def eff(ch):
    """Ефективні налаштування: під час рейду діє суворий режим."""
    r = raid.get(ch["chat_id"])
    if r and r["until"] > time.monotonic():
        return {
            **ch,
            "max_tries": 1,
            "timeout": min(ch["timeout"] or 2, 2),
            "cooldown": max(ch["cooldown"], 30),
        }
    return ch


async def raid_alert(cid, n):
    ch = chats.get(cid)
    if not ch:
        return
    targets = set(recipients(ch))
    if not targets:
        owner = ch.get("owner_id") or ch.get("added_by")
        if owner:
            targets.add(owner)
    for to in targets:
        lang = lang_by_id(to)
        try:
            await bot.send_message(to, t(lang, "raid_alert", title=esc(ch["title"]), n=n, m=RAID_MINUTES))
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


def captcha_markup(cid, uid, options, prefix="c"):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"{prefix}:{cid}:{uid}:{int(ok)}")
        for label, ok in options
    ]])


def hint_line(lang, ch):
    parts = []
    if ch["timeout"] > 0:
        parts.append(t(lang, "h_time", v=fmt_min(lang, ch["timeout"])))
    if ch["max_tries"] > 1:
        parts.append(t(lang, "h_tries", n=ch["max_tries"]))
    return "  ·  ".join(parts)


def captcha_message(ch, lang, cid, uid, head=None, prefix="c"):
    q, opts = build_captcha(random.choice(kinds_of(ch)), lang)
    if head is None:
        head = esc(ch["welcome"]) if ch["welcome"] else t(lang, "welcome_def")
    hint = hint_line(lang, eff(ch))
    text = f"{head}\n\n{q}" + (f"\n\n{hint}" if hint else "")
    return text, captcha_markup(cid, uid, opts, prefix)


# ---------- заявки на вступ ----------

@dp.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    cid, uid = req.chat.id, req.from_user.id
    ch = chats.get(cid)
    if ch is None:
        ch = await register_chat(req.chat)
    if not ch["enabled"]:
        return

    if ch["mode"] == "captcha" and ch.get("raid_guard", True):
        n = raid_hit(cid)
        if n:
            background.append(asyncio.create_task(raid_alert(cid, n)))

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
            pic = "captcha"
        else:
            text, markup = S["pick_lang"], picker_markup(f"cl:{cid}:{uid}")
            pic = "menu"
        msg = await bot.send_photo(req.user_chat_id, img(pic), caption=text, reply_markup=markup)
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
    await edit_card(cb.message.chat.id, cb.message.message_id, "captcha", text, markup)
    await cb.answer()


@dp.callback_query(F.data.startswith("t:"))
async def on_test_answer(cb: CallbackQuery):
    """Тест капчі: усе так само, як бачить заявник (картинки, спроби, пауза), але нічого не змінюється."""
    _, cid, uid, ok = cb.data.split(":")
    cid, uid = int(cid), int(uid)
    if cb.from_user.id != uid or cid not in chats:
        await cb.answer()
        return
    lang = lang_of(cb.from_user)
    raw = chats[cid]
    ch = eff(raw)
    chat_id, mid = cb.message.chat.id, cb.message.message_id
    key = (cid, uid)
    correct = ok == "1"
    if not correct:
        fails = test_fails.get(key, 0) + 1
        left = ch["max_tries"] - fails
        if left > 0:
            test_fails[key] = fails
            text, markup = captcha_message(raw, lang, cid, uid, head=t(lang, "retry", k=left), prefix="t")
            await edit_card(chat_id, mid, "captcha", text, markup)
            await cb.answer()
            return
    test_fails.pop(key, None)
    if correct:
        text, pic = t(lang, "ok_join"), "success"
    else:
        text, pic = t(lang, "wrong"), "fail"
        if ch["cooldown"] > 0:
            text += " " + t(lang, "wait", v=fmt_min(lang, ch["cooldown"]))
    text += "\n\n" + t(lang, "test_note")
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn(t(lang, "b_back"), f"o:{cid}")]])
    await edit_card(chat_id, mid, pic, text, kb)
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

    key = (cid, uid)
    ch = eff(chats[cid]) if cid in chats else None
    info = pending.get(key)
    if info is None:
        info = {
            "ucid": cb.message.chat.id,
            "mid": cb.message.message_id,
            "t": time.monotonic(),
            "name": cb.from_user.full_name,
            "username": cb.from_user.username,
            "lang": None,
            "fails": 0,
        }
    lang = info.get("lang") or lang_of(cb.from_user)

    if not correct and ch:
        info["fails"] = info.get("fails", 0) + 1
        left = ch["max_tries"] - info["fails"]
        if left > 0:
            pending[key] = info
            text, markup = captcha_message(ch, lang, cid, uid, head=t(lang, "retry", k=left))
            await edit_card(cb.message.chat.id, cb.message.message_id, "captcha", text, markup)
            await cb.answer()
            return

    pending.pop(key, None)
    name, username = info["name"], info["username"]

    result = None
    pic = "fail"
    if correct:
        if await try_approve(cid, uid):
            text = t(lang, "ok_join")
            pic = "success"
            result = "approved"
        else:
            text = t(lang, "stale")
    else:
        await safe_decline(cid, uid)
        text = t(lang, "wrong")
        cd = ch["cooldown"] if ch else 0
        if cd > 0:
            if key not in blocks or blocks[key] is not None:
                blocks[key] = datetime.now(timezone.utc) + timedelta(minutes=cd)
            text += " " + t(lang, "wait", v=fmt_min(lang, cd))
        result = "declined"

    await edit_card(cb.message.chat.id, cb.message.message_id, pic, text)
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
                if ch:
                    ch = eff(ch)
                minutes = ch["timeout"] if ch else 5
                if minutes <= 0 or now - p["t"] < minutes * 60:
                    continue
                pending.pop(key, None)
                await safe_decline(cid, uid)
                record(cid, uid, "timeout")
                if ch and ch["cooldown"] > 0:
                    if key not in blocks or blocks[key] is not None:
                        blocks[key] = datetime.now(timezone.utc) + timedelta(minutes=ch["cooldown"])
                await edit_card(p["ucid"], p["mid"], "fail", t(p["lang"] or "en", "timeout_user"))
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
    msg = cb.message
    if getattr(msg, "photo", None):  # з картинки не можна зробити текст — надсилаємо нове повідомлення
        try:
            await msg.delete()
        except TelegramAPIError:
            pass
        await bot.send_message(msg.chat.id, text, reply_markup=kb)
        return
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


async def show_photo(cb, data, caption, kb, name="stats.png"):
    msg = cb.message
    if getattr(msg, "photo", None):
        try:
            await msg.edit_media(
                media=InputMediaPhoto(media=BufferedInputFile(data, name), caption=caption, parse_mode=ParseMode.HTML),
                reply_markup=kb,
            )
        except TelegramBadRequest:
            pass
        return
    await bot.send_photo(msg.chat.id, BufferedInputFile(data, name), caption=caption, reply_markup=kb)
    try:
        await msg.delete()
    except TelegramAPIError:
        pass


def home_view(uid, lang):
    cids = [c for c in admins.get(uid, ()) if c in chats]
    rows = []
    for cid in sorted(cids, key=lambda c: chats[c]["title"].lower())[:25]:
        dot = "🟢" if chats[cid]["enabled"] else "🔴"
        rows.append([btn(f"{dot}  {chats[cid]['title'][:40]}", f"o:{cid}")])
    rows.append([
        InlineKeyboardButton(
            text=t(lang, "b_add_ch"),
            url=f"https://t.me/{BOT_USER}?startchannel=botstart&admin=invite_users",
        ),
        InlineKeyboardButton(
            text=t(lang, "b_add_gr"),
            url=f"https://t.me/{BOT_USER}?startgroup=botstart&admin=invite_users",
        ),
    ])
    rows.append([btn(t(lang, "b_find"), "lc"), btn(t(lang, "b_lang"), "lg")])
    if uid == OWNER_ID:
        rows.append([btn("👑 Панель автора", "ow:0")])
    text = t(lang, "home", n=len(cids)) if cids else t(lang, "home_empty")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def panel_view(cid, lang, uid):
    ch = chats[cid]
    warn = ""
    try:
        me = await bot.get_chat_member(cid, bot.id)
        if st(me) != "administrator" or not getattr(me, "can_invite_users", False):
            warn = t(lang, "warn_rights")
    except TelegramAPIError:
        pass
    rd_ = raid.get(cid)
    if rd_ and rd_["until"] > time.monotonic():
        warn += t(lang, "raid_on", m=max(1, int((rd_["until"] - time.monotonic()) / 60)))
    mode = t(lang, "mode_captcha" if ch["mode"] == "captcha" else "mode_auto")
    text = t(
        lang, "panel",
        title=esc(ch["title"]),
        status=t(lang, "pr_on" if ch["enabled"] else "pr_off"),
        mode=mode,
        timeout=fmt_min(lang, ch["timeout"]),
        tries=ch["max_tries"],
        notify=t(lang, "nm_" + ch["notify_mode"]),
        p=sum(1 for k in pending if k[0] == cid),
        warn=warn,
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(t(lang, "b_pause" if ch["enabled"] else "b_resume"), f"x:en:{cid}")],
        [btn(t(lang, "b_stats"), f"x:st:{cid}"), btn(t(lang, "b_settings"), f"x:se:{cid}")],
        [btn(t(lang, "b_types"), f"x:ty:{cid}"), btn(t(lang, "b_bl"), f"x:bl:{cid}")],
        [btn(t(lang, "b_welcome"), f"x:wl:{cid}"), btn(t(lang, "b_test"), f"x:ts:{cid}")],
        [btn(t(lang, "b_back"), "h")],
    ])
    if ch.get("ctype") == "channel":
        kb.inline_keyboard.insert(-1, [btn(t(lang, "b_footer"), f"x:fo:{cid}")])
    return text, kb


def settings_view(cid, lang):
    ch = chats[cid]
    mode = t(lang, "mode_captcha" if ch["mode"] == "captcha" else "mode_auto")
    onoff = lambda v: t(lang, "v_on" if v else "v_off")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(t(lang, "b_mode", v=mode), f"x:md:{cid}")],
        [btn(t(lang, "b_timeout", v=fmt_min(lang, ch["timeout"])), f"x:to:{cid}")],
        [btn(t(lang, "b_tries", v=ch["max_tries"]), f"x:tr:{cid}")],
        [btn(t(lang, "b_cool", v=fmt_min(lang, ch["cooldown"])), f"x:cd:{cid}")],
        [btn(t(lang, "b_raid", v=onoff(ch["raid_guard"])), f"x:rg:{cid}")],
        [btn(t(lang, "b_weekly", v=onoff(ch["weekly"])), f"x:wk:{cid}")],
        [btn(t(lang, "b_notify", v=t(lang, "nm_" + ch["notify_mode"])), f"x:nm:{cid}")],
        [btn(t(lang, "b_help"), f"x:hp:{cid}")],
        [btn(t(lang, "b_back"), f"o:{cid}")],
    ])
    return t(lang, "set_title", title=esc(ch["title"]), n=RAID_N, m=RAID_MINUTES), kb


def notif_view(cid, lang):
    cur = chats[cid]["notify_mode"]
    rows = [
        [btn(("✅ " if cur == m else "▫️ ") + t(lang, "nm_" + m), f"x:ns:{cid}:{m}")]
        for m in ("all", "owner", "off")
    ]
    rows.append([btn(t(lang, "b_back"), f"x:se:{cid}")])
    return t(lang, "nf_title", title=esc(chats[cid]["title"])), InlineKeyboardMarkup(inline_keyboard=rows)


def types_view(cid, lang):
    on = kinds_of(chats[cid])
    rows = [
        [btn(f"{'✅' if k in on else '⬜'} {t(lang, 'tn_' + k)}", f"x:tt:{cid}:{k}")]
        for k in TYPES
    ]
    rows.append([btn(t(lang, "b_back"), f"o:{cid}")])
    return t(lang, "types_title"), InlineKeyboardMarkup(inline_keyboard=rows)


def bar(n, total, width=10):
    if not total or not n:
        return "▱" * width
    filled = max(1, round(width * n / total))
    return "▰" * filled + "▱" * (width - filled)


def stats_kb(cid, lang, period):
    tabs = [("d", "st_24h"), ("w", "st_7d"), ("a", "st_all")]
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(("• " if k == period else "") + t(lang, lbl), f"x:st:{cid}:{k}") for k, lbl in tabs],
        [btn(t(lang, "b_refresh"), f"x:st:{cid}:{period}")],
        [btn(t(lang, "b_back"), f"o:{cid}")],
    ])


async def stats_view(cid, lang, period="d"):
    """Текстова статистика (запасний варіант, якщо картинку намалювати не вдалося)."""
    await flush_stats()
    now = datetime.now(timezone.utc)
    since = {"d": now - timedelta(days=1), "w": now - timedelta(days=7)}.get(
        period, datetime(2000, 1, 1, tzinfo=timezone.utc)
    )
    rows = await db.fetch(
        "SELECT result, count(*) AS n FROM chat_stats "
        "WHERE chat_id = $1 AND ts >= $2 GROUP BY result",
        cid, since,
    )
    c = {r["result"]: r["n"] for r in rows}
    ok = c.get("approved", 0) + c.get("auto", 0)
    items = [
        ("✅", "sl_ok", ok),
        ("❌", "sl_dec", c.get("declined", 0)),
        ("⌛", "sl_to", c.get("timeout", 0)),
        ("🚫", "sl_bl", c.get("blocked", 0)),
    ]
    total = sum(n for _, _, n in items)
    head = f"{t(lang, 'stats_title')}\n📣 {esc(chats[cid]['title'])}\n\n"
    if total == 0:
        body = t(lang, "st_empty")
    else:
        parts = [
            f"{e} {t(lang, k)}: <b>{n}</b>\n{bar(n, total)}  {round(100 * n / total)}%"
            for e, k, n in items
            if n or k == "sl_ok"
        ]
        body = t(lang, "st_total", n=total) + "\n\n" + "\n\n".join(parts)
    return head + body, stats_kb(cid, lang, period)


PERIODS = {"d": (timedelta(days=1), 8, 10800), "w": (timedelta(days=7), 7, 86400), "a": (None, 8, 604800)}
DAYNAMES = {
    "ru": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "uk": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"),
}


async def stats_data(cid, period):
    await flush_stats()
    now = datetime.now(timezone.utc)
    span, nb, bs = PERIODS[period]
    since = now - span if span else datetime(2000, 1, 1, tzinfo=timezone.utc)
    rows = await db.fetch(
        "SELECT result, count(*) AS n FROM chat_stats "
        "WHERE chat_id = $1 AND ts >= $2 GROUP BY result",
        cid, since,
    )
    c = {r["result"]: r["n"] for r in rows}
    prev = 0
    if span:
        prev = await db.fetchval(
            "SELECT count(*) FROM chat_stats WHERE chat_id = $1 AND ts >= $2 AND ts < $3",
            cid, now - 2 * span, since,
        )
    rows = await db.fetch(
        "SELECT floor(extract(epoch FROM ($2::timestamptz - ts))::float8 / $3::float8)::int AS b, "
        "result, count(*) AS n FROM chat_stats "
        "WHERE chat_id = $1 AND ts >= $4 GROUP BY 1, 2",
        cid, now, float(bs), now - timedelta(seconds=bs * nb),
    )
    series = [[0, 0] for _ in range(nb)]
    for r in rows:
        b = r["b"]
        if 0 <= b < nb:
            series[b][0 if r["result"] in ("approved", "auto") else 1] += r["n"]
    series.reverse()  # зліва найстарше, справа найновіше
    return {"c": c, "prev": prev or 0, "series": series, "now": now}


def chart_labels(lang, period, now, nb):
    out = []
    for k in range(nb - 1, -1, -1):
        if period == "d":
            out.append(f"-{(k + 1) * 3}")
        elif period == "w":
            out.append(DAYNAMES[lang][(now - timedelta(days=k)).weekday()])
        else:
            out.append((now - timedelta(weeks=k + 1)).strftime("%d.%m"))
    return out


_fonts = {}


def _font(sz, bold=False):
    key = (sz, bold)
    if key in _fonts:
        return _fonts[key]
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    here = os.path.dirname(os.path.abspath(__file__))
    f = None
    for base in (here, "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            f = ImageFont.truetype(p, sz)
            break
    _fonts[key] = f
    return f


def clean_title(s):
    s = "".join(ch for ch in (s or "") if ord(ch) < 0x2600 and ch.isprintable()).strip()
    if len(s) > 30:
        s = s[:29] + "…"
    return s or "—"


def render_stats(lang, title, period, data):
    """Малює картку статистики. Повертає PNG-байти або None, якщо немає Pillow чи шрифту."""
    if not PIL_OK or _font(10) is None or _font(10, True) is None:
        return None
    K, U = 2, 900 / 1080
    R = lambda v: int(round(v * K * U))
    F = lambda sz, bold=False: _font(R(sz), bold)
    BG, CARD, TRACK = "#0E1621", "#17212B", "#243040"
    TXT, MUTED = "#FFFFFF", "#8A97A8"
    GREEN, RED, AMBER, GRAY, BLUE = "#3DDC84", "#FF5C6C", "#FFB547", "#6B7889", "#2F80ED"

    c, prev, series, now = data["c"], data["prev"], data["series"], data["now"]
    ok = c.get("approved", 0) + c.get("auto", 0)
    dec, to, bl = c.get("declined", 0), c.get("timeout", 0), c.get("blocked", 0)
    total = ok + dec + to + bl
    label = t(lang, {"d": "st_24h", "w": "st_7d", "a": "st_all"}[period])

    W, H = 1080, 1240
    img = Image.new("RGB", (R(W), R(H)), BG)
    d = ImageDraw.Draw(img)

    def card(x0, y0, x1, y1, r=36):
        d.rounded_rectangle((R(x0), R(y0), R(x1), R(y1)), radius=R(r), fill=CARD)

    # шапка
    d.text((R(60), R(70)), t(lang, "img_title"), font=F(58, True), fill=TXT, anchor="lm")
    d.text((R(60), R(130)), clean_title(title), font=F(32), fill=MUTED, anchor="lm")
    d.text((R(1020), R(70)), t(lang, "img_upd", hm=now.strftime("%H:%M")), font=F(22), fill=MUTED, anchor="rm")

    # вкладки
    x = 60
    for k, key in (("d", "st_24h"), ("w", "st_7d"), ("a", "st_all")):
        sel = k == period
        d.rounded_rectangle((R(x), R(190), R(x + 300), R(262)), radius=R(36), fill=BLUE if sel else CARD)
        d.text((R(x + 150), R(226)), t(lang, key), font=F(31, sel), fill=TXT if sel else MUTED, anchor="mm")
        x += 320

    # підсумок
    card(40, 300, 1040, 520)
    d.text((R(80), R(390)), str(total), font=F(118, True), fill=TXT, anchor="lm")
    d.text((R(80), R(470)), t(lang, "img_for", p=label), font=F(33), fill=MUTED, anchor="lm")
    if period != "a" and prev > 0:
        pct = round(100 * (total - prev) / prev)
        up = pct >= 0
        d.rounded_rectangle((R(630), R(350), R(1000), R(420)), radius=R(35), fill="#1C3A2C" if up else "#3A1C24")
        d.text(
            (R(815), R(385)),
            f"{'▲' if up else '▼'} {pct:+d}% {t(lang, 'img_vs')}",
            font=F(22, True), fill=GREEN if up else RED, anchor="mm",
        )

    # кільце + легенда
    card(40, 550, 1040, 900)
    cx, cy, r, wd = 270, 725, 140, 34
    box = (R(cx - r), R(cy - r), R(cx + r), R(cy + r))
    d.arc(box, 0, 360, fill=TRACK, width=R(wd))
    parts = [("sl_ok", ok, GREEN), ("sl_dec", dec, RED), ("sl_to", to, AMBER), ("sl_bl", bl, GRAY)]
    nonzero = [p for p in parts if p[1] > 0]
    if len(nonzero) == 1:
        d.arc(box, 0, 360, fill=nonzero[0][2], width=R(wd))
    else:
        ang = -90.0
        for _, n, col in nonzero:
            sw = 360.0 * n / total
            d.arc(box, ang + 1.2, ang + sw - 1.2, fill=col, width=R(wd))
            ang += sw
    pct_ok = f"{round(100 * ok / total)}%" if total else "—"
    d.text((R(cx), R(cy - 12)), pct_ok, font=F(54, True), fill=TXT, anchor="mm")
    d.text((R(cx), R(cy + 36)), t(lang, "img_acc"), font=F(25), fill=MUTED, anchor="mm")
    ly = 612
    for key, n, col in parts:
        d.ellipse((R(480), R(ly + 8), R(508), R(ly + 36)), fill=col)
        d.text((R(526), R(ly + 22)), t(lang, key), font=F(30), fill=TXT, anchor="lm")
        d.text((R(990), R(ly + 22)), str(n), font=F(34, True), fill=TXT, anchor="rm")
        ly += 70

    # стовпчики
    card(40, 930, 1040, 1200)
    d.text((R(80), R(985)), t(lang, "img_c_" + period), font=F(30, True), fill=TXT, anchor="lm")
    n = len(series)
    labels = chart_labels(lang, period, now, n)
    top, base, bw = 1040, 1150, 80
    gap = (920 - n * bw) / (n - 1) if n > 1 else 0
    mx = max([a + o for a, o in series] + [1])
    for i, (a, o) in enumerate(series):
        x = 80 + i * (bw + gap)
        if a + o == 0:
            d.rounded_rectangle((R(x), R(base - 8), R(x + bw), R(base)), radius=R(4), fill=TRACK)
        else:
            ha = max((base - top) * a / mx, 10) if a else 0
            ho = max((base - top) * o / mx, 10) if o else 0
            if a:
                d.rounded_rectangle((R(x), R(base - ha), R(x + bw), R(base)), radius=R(12), fill=GREEN)
            if o:
                y1 = base - ha - (3 if a else 0)
                d.rounded_rectangle((R(x), R(y1 - ho), R(x + bw), R(y1)), radius=R(12), fill=RED)
        d.text((R(x + bw / 2), R(base + 28)), labels[i], font=F(22), fill=MUTED, anchor="mm")

    img = img.reduce(K)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- ілюстрації з назвою каналу (малюються на льоту) ----------

def _g(s):
    return s.strip("\n").split("\n")


ICON_GRIDS = {
    "shield": (
        _g("""
.###########.
#############
#############
#############
#############
#############
#############
#############
.###########.
.###########.
..#########..
...#######...
....#####....
.....###.....
......#......"""),
        _g("""
.............
.............
.............
.............
.........###.
........###..
..##...###...
..###.###....
...#####.....
....###......
.....#.......
.............
.............
.............
............."""),
    ),
    "gear": (
        _g("""
....###....
.#..###..#.
###########
.#########.
.#########.
###########
.#########.
.#########.
###########
.#..###..#.
....###...."""),
        _g("""
...........
...........
...........
...#####...
...#####...
...#####...
...#####...
...#####...
...........
...........
..........."""),
    ),
    "grid": (
        _g("""
#####.#####
#####.#####
#####.#####
#####.#####
#####.#####
...........
#####.#####
#####.#####
#####.#####
#####.#####
#####.#####"""),
        None,
    ),
    "bell": (
        _g("""
.....#.....
....###....
...#####...
..#######..
..#######..
..#######..
.#########.
.#########.
###########
###########
...........
....###...."""),
        None,
    ),
    "ban": (
        _g("""
...#####...
..#######..
.#########.
###########
###########
###########
###########
###########
.#########.
..#######..
...#####..."""),
        _g("""
...........
...........
...........
...........
..#######..
..#######..
..#######..
...........
...........
...........
..........."""),
    ),
    "bubble": (
        _g("""
.###########.
#############
#############
#############
#############
#############
.###########.
...####......
....###......
.....##......
......#......"""),
        _g("""
.............
.............
.............
...#..#..#...
...#..#..#...
.............
.............
.............
.............
.............
............."""),
    ),
}
BANNER_BLOCKS = [
    (50, 100, 225, 65), (50, 165, 125, 65), (1000, 950, 225, 65), (1100, 1015, 125, 65),
    (950, 175, 175, 55), (1025, 230, 100, 55), (75, 1050, 175, 55), (75, 1105, 100, 55),
]
BANNER_BLOCKS = [(x, y // 2, w, h) for x, y, w, h in BANNER_BLOCKS]


def _lerp_color(t, stops):
    t = min(max(t, 0.0), 1.0)
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3))
    return stops[-1][1]


def render_banner(icon, title, label):
    """Ілюстрація 1280x720: піксельна іконка + назва каналу. JPEG-байти або None."""
    if not PIL_OK or _font(10) is None or _font(10, True) is None:
        return None
    grid, over = ICON_GRIDS[icon]
    W, H, S, D = 1280, 720, 20, 26
    stops = [(0.0, (230, 230, 230)), (0.5, (122, 122, 122)), (1.0, (43, 43, 43))]
    img = Image.new("RGB", (W, H), "#0F0F0F")
    d = ImageDraw.Draw(img)
    for x, y, w, h in BANNER_BLOCKS:
        d.rectangle((x, y, x + w, y + h), fill="#1D1D1D")
    cells = [(x, y) for y, r in enumerate(grid) for x, c in enumerate(r) if c == "#"]
    w, h = len(grid[0]) * S, len(grid) * S
    ox = (W - w - D) // 2
    oy = 70 + (310 - h) // 2
    tot = float(w + h + 2 * D)
    for k in range(D, 0, -3):
        for cx, cy in cells:
            x0, y0 = ox + cx * S + k, oy + cy * S + k
            col = _lerp_color(((x0 - ox) + (y0 - oy) + S) / tot, stops)
            d.rectangle((x0, y0, x0 + S - 1, y0 + S - 1), fill=col)
    for cx, cy in cells:
        x0, y0 = ox + cx * S, oy + cy * S
        d.rectangle((x0, y0, x0 + S - 1, y0 + S - 1), fill="#FFFFFF")
    if over:
        for y, r in enumerate(over):
            for x, c in enumerate(r):
                if c == "#":
                    x0, y0 = ox + x * S, oy + y * S
                    d.rectangle((x0, y0, x0 + S - 1, y0 + S - 1), fill="#0F0F0F")
    text = title
    size = 72
    while size > 36 and d.textlength(text, font=_font(size, True)) > 1040:
        size -= 2
    f = _font(size, True)
    while len(text) > 3 and d.textlength(text, font=f) > 1040:
        text = text[:-2].rstrip() + "…"
    d.text((W // 2, 490), text, font=f, fill="#FFFFFF", anchor="mm")
    d.text((W // 2, 570), label, font=_font(32), fill="#8A8A8A", anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()


ICON = {"main": "shield", "set": "gear", "types": "grid", "notif": "bell", "bl": "ban", "wl": "bubble"}
_bn_cache = {}


async def chan_banner(cid, icon, lang):
    ch = chats.get(cid)
    if not ch or not PIL_OK:
        return None
    key = (icon, ch["title"], lang)
    if key in _bn_cache:
        return _bn_cache[key]
    try:
        data = await asyncio.to_thread(
            render_banner, icon, clean_title(ch["title"]), t(lang, "bn_" + icon)
        )
    except Exception:
        log.exception("banner failed")
        return None
    if data:
        if len(_bn_cache) > 150:
            _bn_cache.clear()
        _bn_cache[key] = data
    return data


def plain_len(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


async def show_chan(cb, cid, icon, text, kb):
    """Екран каналу: ілюстрація з назвою каналу зверху, текст і кнопки знизу."""
    png = None
    if plain_len(text) <= 1000:
        png = await chan_banner(cid, icon, lang_of(cb.from_user))
    if png:
        await show_photo(cb, png, text, kb, "banner.jpg")
    else:
        await show(cb, text, kb)


async def send_chan(chat_id, cid, icon, lang, text, kb):
    png = None
    if plain_len(text) <= 1000:
        png = await chan_banner(cid, icon, lang)
    if png:
        await bot.send_photo(chat_id, BufferedInputFile(png, "banner.jpg"), caption=text, reply_markup=kb)
    else:
        await bot.send_message(chat_id, text, reply_markup=kb)


async def stats_screen(cid, lang, period):
    """Повертає (png | None, підпис або текст, клавіатура)."""
    if PIL_OK:
        try:
            data = await stats_data(cid, period)
            png = await asyncio.to_thread(render_stats, lang, chats[cid]["title"], period, data)
            if png:
                c = data["c"]
                ok = c.get("approved", 0) + c.get("auto", 0)
                dec, to, bl = c.get("declined", 0), c.get("timeout", 0), c.get("blocked", 0)
                caption = (
                    f"{t(lang, 'stats_title')} · {esc(chats[cid]['title'])}\n"
                    f"{t(lang, 'st_total', n=ok + dec + to + bl)}\n"
                    f"✅ {ok} · ❌ {dec} · ⌛ {to} · 🚫 {bl}"
                )
                return png, caption, stats_kb(cid, lang, period)
        except Exception:
            log.exception("stats image failed")
    text, kb = await stats_view(cid, lang, period)
    return None, text, kb


# ---------- тижневий звіт ----------

async def send_report(cid):
    ch = chats.get(cid)
    if not ch:
        return
    targets = set(recipients(ch))
    if not targets:
        owner = ch.get("owner_id") or ch.get("added_by")
        if owner:
            targets.add(owner)
    cache = {}
    for to in targets:
        lang = lang_by_id(to)
        if lang not in cache:
            cache[lang] = await stats_screen(cid, lang, "w")
        png, caption, _ = cache[lang]
        head = t(lang, "weekly_head")
        try:
            if png:
                await bot.send_photo(to, BufferedInputFile(png, "report.png"), caption=f"{head}\n{caption}")
            else:
                await bot.send_message(to, f"{head}\n\n{caption}")
        except TelegramAPIError:
            pass


async def weekly_loop():
    while True:
        await asyncio.sleep(600)
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() != 0 or now.hour < 9:  # понеділок, після 09:00 UTC
                continue
            today = now.date()
            for cid, ch in list(chats.items()):
                if not ch.get("weekly"):
                    continue
                last = ch.get("last_report")
                if last and (today - last).days < 6:
                    continue
                await upd(cid, last_report=today)
                await send_report(cid)
        except Exception:
            log.exception("weekly loop")


OWNER_PAGE = 6


def owner_view(page):
    items = sorted(chats.values(), key=lambda c: c["title"].lower())
    pages = max(1, (len(items) + OWNER_PAGE - 1) // OWNER_PAGE)
    page = min(max(page, 0), pages - 1)
    chunk = items[page * OWNER_PAGE:(page + 1) * OWNER_PAGE]
    on = sum(1 for c in items if c["enabled"])
    lines = [
        "👑 <b>Панель автора</b>",
        "──────────────",
        f"📣 Каналів і груп: <b>{len(items)}</b> (🟢 {on})",
        f"👥 Адмінів: <b>{len(admins)}</b>",
        f"⏳ Чекають капчі: <b>{len(pending)}</b> · 🚫 блокувань: <b>{len(blocks)}</b>",
        f"🕒 Аптайм: <b>{int((time.time() - STARTED) / 3600)} год</b>",
        "",
    ]
    if not chunk:
        lines.append("Каналів поки немає.")
    kinds = {"channel": "📢 канал", "group": "👥 група", "supergroup": "👥 група"}
    for i, c in enumerate(chunk, page * OWNER_PAGE + 1):
        cid = c["chat_id"]
        dot = "🟢" if c["enabled"] else "🔴"
        n_adm = sum(1 for s in admins.values() if cid in s)
        who = c.get("owner_id") or c.get("added_by")
        who_txt = f'<a href="tg://user?id={who}">{who}</a>' if who else "—"
        lines.append(
            f"{i}. {dot} <b>{esc(c['title'])}</b>\n"
            f"    {kinds.get(c['ctype'], esc(c['ctype']))} · 👥 {n_adm} · 👤 {who_txt}\n"
            f"    <code>{cid}</code>"
        )
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"ow:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", f"ow:{page}"))
    if page < pages - 1:
        nav.append(btn("▶️", f"ow:{page + 1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav, [btn("🔄 Оновити", f"ow:{page}")], [btn("◀️ Меню", "h")]])
    return "\n".join(lines), kb


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
        await m.answer_photo(img("menu"), caption=S["pick_lang"], reply_markup=picker_markup("sl"))
        return
    text, kb = home_view(m.from_user.id, lang)
    await m.answer_photo(img("menu"), caption=text, reply_markup=kb)


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
    text, kb = owner_view(0)
    await m.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("ow:"), F.from_user.id == OWNER_ID)
async def on_owner_page(cb: CallbackQuery):
    text, kb = owner_view(int(cb.data.split(":")[1]))
    await show(cb, text, kb)
    await cb.answer()


def waiting_text(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id in awaiting and m.text and not m.text.startswith("/"))


@dp.message(F.chat.type == "private", waiting_text)
async def on_welcome_text(m: Message):
    uid, lang = m.from_user.id, lang_of(m.from_user)
    v = awaiting.get(uid)
    footer_mode = isinstance(v, tuple)
    kind = v[0] if footer_mode else None
    cid = v[1] if footer_mode else v
    text = m.text.strip()
    if footer_mode:
        if len(text) > FT_LIMITS[kind]:
            await m.answer(t(lang, "ft_long", n=FT_LIMITS[kind]))
            return
        link = None
        if kind == "l":
            link = norm_link(text)
            if not link:
                await m.answer(t(lang, "ft_link_bad"))
                return
    elif len(text) > 300:
        await m.answer(t(lang, "w_long"))
        return
    awaiting.pop(uid, None)
    if cid not in chats or not await is_admin(cid, uid):
        await m.answer(t(lang, "no_access"))
        return
    if footer_mode:
        if kind == "f":
            await upd(cid, footer_text=text)
            done = "ft_saved"
        elif kind == "s":
            await upd(cid, footer_sep="" if text in ("-", "0") else text)
            done = "ft_sep_saved"
        else:
            await upd(cid, footer_link=link)
            done = "ft_link_saved"
        await m.answer(t(lang, done))
        ftext, fkb = await footer_view(cid, lang)
        await send_chan(m.chat.id, cid, "shield", lang, ftext, fkb)
        return
    await upd(cid, welcome=text)
    await m.answer(t(lang, "w_saved"))
    panel, kb = await panel_view(cid, lang, uid)
    await send_chan(m.chat.id, cid, "shield", lang, panel, kb)


@dp.callback_query(F.data.startswith("sl:"))
async def on_set_lang(cb: CallbackQuery):
    lang = cb.data.split(":")[1]
    if lang not in LANGS:
        await cb.answer()
        return
    await set_lang(cb.from_user.id, lang)
    text, kb = home_view(cb.from_user.id, lang)
    await show_home(cb, text, kb)
    await cb.answer(t(lang, "lang_set"))


@dp.callback_query(F.data == "lg")
async def on_lang_menu(cb: CallbackQuery):
    await show_home(cb, S["pick_lang"], picker_markup("sl"))
    await cb.answer()


@dp.callback_query(F.data == "h")
async def on_home(cb: CallbackQuery):
    awaiting.pop(cb.from_user.id, None)
    text, kb = home_view(cb.from_user.id, lang_of(cb.from_user))
    await show_home(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "lc")
async def on_find(cb: CallbackQuery):
    uid = cb.from_user.id
    todo = [c for c in chats if c not in admins.get(uid, ())][:40]
    if todo:
        await asyncio.gather(*(is_admin(c, uid) for c in todo))
    text, kb = home_view(uid, lang_of(cb.from_user))
    await show_home(cb, text, kb)
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
    await show_chan(cb, cid, "shield", text, kb)
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
    if act not in ("wl", "fl", "fs", "fk"):
        awaiting.pop(uid, None)
    screen, period = "main", "d"

    if act == "en":
        await upd(cid, enabled=not ch["enabled"])
    elif act == "md":
        await upd(cid, mode="auto" if ch["mode"] == "captcha" else "captcha")
        screen = "set"
    elif act in CYC:
        key = CYC[act]
        vals, cur = CYCLES[key], ch[key]
        nxt = vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]
        await upd(cid, **{key: nxt})
        screen = "set"
    elif act == "se":
        screen = "set"
    elif act == "rg":
        await upd(cid, raid_guard=not ch["raid_guard"])
        screen = "set"
    elif act == "wk":
        await upd(cid, weekly=not ch["weekly"])
        screen = "set"
    elif act == "nm":
        screen = "notif"
    elif act == "ns":
        if arg in ("all", "owner", "off"):
            await upd(cid, notify_mode=arg)
        screen = "notif"
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
    elif act == "fo":
        screen = "footer"
    elif act == "ft":
        if ch["footer"]:
            await upd(cid, footer=False)
        else:
            ok, err = await footer_prepare(cid, lang)
            if not ok:
                await cb.answer(err, show_alert=True)
                return
            await upd(cid, footer=True)
        screen = "footer"
    elif act == "fl":
        awaiting[uid] = ("f", cid)
        cur = esc(ch.get("footer_text") or t(lang, "ft_default"))
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn(t(lang, "b_cancel"), f"x:fo:{cid}")]])
        await show_chan(cb, cid, "bubble", t(lang, "ft_prompt", cur=cur), kb)
        await cb.answer()
        return
    elif act == "fy":
        screen = "styles"
    elif act == "fp":
        idx = int(arg) if arg and arg.isdigit() else -1
        if 0 <= idx < len(PRESETS):
            await upd(cid, footer_sep=PRESETS[idx][0], footer_pre=PRESETS[idx][1])
        screen = "footer"
    elif act == "fa":
        ok, err = await footer_new_link(cid, lang)
        if not ok:
            await cb.answer(err, show_alert=True)
            return
        screen = "footer"
    elif act == "fs":
        awaiting[uid] = ("s", cid)
        cur = f"<code>{esc(ch['footer_sep'])}</code>" if ch.get("footer_sep") else "—"
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn(t(lang, "b_cancel"), f"x:fo:{cid}")]])
        await show_chan(cb, cid, "bubble", t(lang, "ft_sep_prompt", cur=cur), kb)
        await cb.answer()
        return
    elif act == "fk":
        awaiting[uid] = ("l", cid)
        cur = f"<code>{esc(ch['footer_link'])}</code>" if ch.get("footer_link") else "—"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn(t(lang, "b_ft_auto"), f"x:fa:{cid}"), btn(t(lang, "b_cancel"), f"x:fo:{cid}")],
        ])
        await show_chan(cb, cid, "bubble", t(lang, "ft_link_prompt", cur=cur), kb)
        await cb.answer()
        return
    elif act == "st":
        screen = "stats"
        period = arg if arg in ("d", "w", "a") else "d"
    elif act == "bl":
        screen = "bl"
    elif act == "ub":
        await unblock(cid, int(arg))
        screen = "bl"
    elif act == "ts":
        test_fails.pop((cid, uid), None)
        text, markup = captcha_message(ch, lang, cid, uid, prefix="t")
        await bot.send_photo(uid, img("captcha"), caption=text, reply_markup=markup)
        await cb.answer(t(lang, "test_sent"))
        return
    elif act == "hp":
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn(t(lang, "b_back"), f"x:se:{cid}")]])
        await show(cb, t(lang, "set_help", n=RAID_N, m=RAID_MINUTES), kb)
        await cb.answer()
        return
    elif act == "wl":
        awaiting[uid] = cid
        cur = esc(ch["welcome"]) if ch["welcome"] else t(lang, "welcome_def")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn(t(lang, "b_default"), f"x:wr:{cid}"), btn(t(lang, "b_cancel"), f"o:{cid}")],
        ])
        await show_chan(cb, cid, "bubble", t(lang, "w_prompt", cur=cur), kb)
        await cb.answer()
        return

    if screen == "types":
        text, kb = types_view(cid, lang)
    elif screen == "stats":
        png, text, kb = await stats_screen(cid, lang, period)
        if png:
            await show_photo(cb, png, text, kb)
            await cb.answer()
            return
    elif screen == "bl":
        text, kb = bl_view(cid, lang)
    elif screen == "set":
        text, kb = settings_view(cid, lang)
    elif screen == "notif":
        text, kb = notif_view(cid, lang)
    elif screen == "footer":
        text, kb = await footer_view(cid, lang)
    elif screen == "styles":
        text, kb = styles_view(cid, lang)
    else:
        text, kb = await panel_view(cid, lang, uid)
    if screen == "stats":
        await show(cb, text, kb)
    else:
        await show_chan(cb, cid, ICON.get(screen, "shield"), text, kb)
    await cb.answer()



# ---------- посилання в кінці постів ----------

PRESETS = [
    ("─" * 18, "🔗 "),
    ("━" * 18, "➤ "),
    ("═" * 18, "✦ "),
    ("▬" * 14, "» "),
    ("", "👉 "),
]
FT_LIMITS = {"f": 60, "s": 40, "l": 200}
albums = {}
footer_tasks = set()


def render_footer(sep, pre, text, link):
    a = f'<a href="{esc(link)}">{esc(pre)}{esc(text)}</a>'
    return f"{esc(sep)}\n{a}" if sep else a


def footer_html(ch):
    return render_footer(ch["footer_sep"], ch["footer_pre"], ch["footer_text"], ch["footer_link"])


def norm_link(s):
    s = s.strip()
    if re.match(r"(?i)^(t|telegram)\.me/", s):
        s = "https://" + s
    return s if re.fullmatch(r"https?://\S{3,190}", s) else None


async def footer_rights_ok(cid):
    try:
        me = await bot.get_chat_member(cid, bot.id)
    except TelegramAPIError:
        return None, None
    if st(me) != "administrator":
        return False, False
    return bool(getattr(me, "can_invite_users", False)), bool(getattr(me, "can_edit_messages", False))


async def footer_new_link(cid, lang):
    inv, _ = await footer_rights_ok(cid)
    if inv is None:
        return False, t(lang, "ft_err")
    if not inv:
        return False, t(lang, "ft_rights")
    try:
        link = await bot.create_chat_invite_link(cid, name="Footer", creates_join_request=True)
    except TelegramAPIError:
        return False, t(lang, "ft_err")
    await upd(cid, footer_link=link.invite_link)
    return True, ""


async def footer_prepare(cid, lang):
    inv, edt = await footer_rights_ok(cid)
    if inv is None:
        return False, t(lang, "ft_err")
    ch = chats[cid]
    if not edt or (not inv and not ch.get("footer_link")):
        return False, t(lang, "ft_rights")
    if not ch.get("footer_link"):
        ok, err = await footer_new_link(cid, lang)
        if not ok:
            return False, err
    if not ch.get("footer_text"):
        await upd(cid, footer_text=t(lang, "ft_default"))
    return True, ""


async def footer_view(cid, lang):
    ch = chats[cid]
    warn = ""
    if ch["footer"]:
        _, edt = await footer_rights_ok(cid)
        if edt is False:
            warn = t(lang, "ft_warn")
    label = ch.get("footer_text") or t(lang, "ft_default")
    href = ch.get("footer_link") or "https://t.me/"
    preview = render_footer(ch["footer_sep"], ch["footer_pre"], label, href)
    link = f"<code>{esc(ch['footer_link'])}</code>" if ch.get("footer_link") else "—"
    text = t(
        lang, "ft_title",
        status=t(lang, "ft_st_on" if ch["footer"] else "ft_st_off"),
        preview=preview,
        link=link,
        warn=warn,
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(t(lang, "b_ft_off" if ch["footer"] else "b_ft_on"), f"x:ft:{cid}")],
        [btn(t(lang, "b_ft_text"), f"x:fl:{cid}"), btn(t(lang, "b_ft_sep"), f"x:fs:{cid}")],
        [btn(t(lang, "b_ft_styles"), f"x:fy:{cid}")],
        [btn(t(lang, "b_ft_link"), f"x:fk:{cid}"), btn(t(lang, "b_ft_auto"), f"x:fa:{cid}")],
        [btn(t(lang, "b_back"), f"o:{cid}")],
    ])
    return text, kb


def styles_view(cid, lang):
    ch = chats[cid]
    label = ch.get("footer_text") or t(lang, "ft_default")
    href = ch.get("footer_link") or "https://t.me/"
    items = []
    for i, (sep, pre) in enumerate(PRESETS, 1):
        items.append(f"<b>{i}.</b>\n{render_footer(sep, pre, label, href)}")
    text = t(lang, "ft_styles", list="\n\n".join(items))
    nums = [btn(str(i + 1), f"x:fp:{cid}:{i}") for i in range(len(PRESETS))]
    kb = InlineKeyboardMarkup(inline_keyboard=[nums, [btn(t(lang, "b_back"), f"x:fo:{cid}")]])
    return text, kb


async def add_footer(m, ch):
    link = ch["footer_link"]
    plain = m.text or m.caption or ""
    body = m.html_text if plain else ""
    if link in body:
        return
    foot = footer_html(ch)
    extra = 4 + len(ch["footer_sep"]) + len(ch["footer_pre"]) + len(ch["footer_text"])
    try:
        if m.text is not None:
            if len(plain) + extra > 4096:
                return
            has_link = any(e.type in ("url", "text_link") for e in (m.entities or []))
            lpo = m.link_preview_options if has_link else LinkPreviewOptions(is_disabled=True)
            await bot.edit_message_text(
                f"{body}\n\n{foot}",
                chat_id=m.chat.id,
                message_id=m.message_id,
                reply_markup=m.reply_markup,
                link_preview_options=lpo,
            )
        elif any((m.photo, m.video, m.animation, m.document, m.audio, m.voice)):
            if len(plain) + extra > 1024:
                return
            await bot.edit_message_caption(
                chat_id=m.chat.id,
                message_id=m.message_id,
                caption=f"{body}\n\n{foot}" if body else foot,
                reply_markup=m.reply_markup,
            )
    except TelegramAPIError as e:
        log.warning("footer failed in %s: %s", m.chat.id, e)


async def finish_album(key):
    await asyncio.sleep(3)
    msgs = albums.pop(key, [])
    ch = chats.get(key[0])
    if not msgs or not ch or not ch.get("footer") or not ch.get("footer_link"):
        return
    target = next((x for x in msgs if x.caption), None) or min(msgs, key=lambda x: x.message_id)
    await add_footer(target, ch)


@dp.channel_post()
async def on_channel_post(m: Message):
    ch = chats.get(m.chat.id)
    if not ch or not ch.get("footer") or not ch.get("footer_link"):
        return
    if m.media_group_id:
        key = (m.chat.id, m.media_group_id)
        if key in albums:
            albums[key].append(m)
        else:
            albums[key] = [m]
            task = asyncio.create_task(finish_album(key))
            footer_tasks.add(task)
            task.add_done_callback(footer_tasks.discard)
        return
    await add_footer(m, ch)


# ---------- запуск ----------

async def on_startup(bot: Bot):
    global BOT_USER
    await init_db()
    BOT_USER = (await bot.get_me()).username
    await bot.set_webhook(
        f"{BASE_URL}/webhook",
        secret_token=SECRET,
        allowed_updates=["message", "callback_query", "chat_join_request", "my_chat_member", "channel_post"],
        drop_pending_updates=False,
    )
    background.append(asyncio.create_task(timeout_loop()))
    background.append(asyncio.create_task(flush_loop()))
    background.append(asyncio.create_task(weekly_loop()))


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
