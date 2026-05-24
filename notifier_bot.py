import logging
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import telebot
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from telebot import types, util
from telebot.custom_filters import StateFilter
from telebot.handler_backends import State, StatesGroup
from telebot.storage import StateMemoryStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("notifier-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env var is required")

_owner = os.getenv("OWNER_ID")
OWNER_ID = int(_owner) if _owner else None

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "notifier.db")
JOBS_DB = os.path.join(DATA_DIR, "jobs.db")

state_storage = StateMemoryStorage()
bot = telebot.TeleBot(BOT_TOKEN, state_storage=state_storage)

scheduler = BackgroundScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{JOBS_DB}")},
    timezone=TIMEZONE,
)


class PostStates(StatesGroup):
    choosing_channels = State()
    waiting_text = State()
    waiting_buttons = State()
    waiting_time = State()
    confirming = State()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                updated_at TEXT
            )
            """
        )
        c.commit()


def upsert_channel(chat_id, title, username):
    with db() as c:
        c.execute(
            """
            INSERT INTO channels (chat_id, title, username, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                username=excluded.username,
                updated_at=excluded.updated_at
            """,
            (chat_id, title, username, datetime.now(TIMEZONE).isoformat()),
        )
        c.commit()


def remove_channel(chat_id):
    with db() as c:
        c.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
        c.commit()


def list_channels():
    with db() as c:
        rows = c.execute(
            "SELECT chat_id, title, username FROM channels ORDER BY title"
        ).fetchall()
        return [dict(r) for r in rows]


def is_authorized(user_id: int) -> bool:
    return OWNER_ID is None or user_id == OWNER_ID


def authorized(handler):
    def wrapped(message_or_call, *args, **kwargs):
        uid = message_or_call.from_user.id
        if not is_authorized(uid):
            if hasattr(message_or_call, "data"):
                bot.answer_callback_query(message_or_call.id, "Доступ запрещён.")
            else:
                bot.reply_to(message_or_call, "Доступ запрещён.")
            return
        return handler(message_or_call, *args, **kwargs)

    wrapped.__name__ = handler.__name__
    return wrapped


@bot.my_chat_member_handler()
def on_my_chat_member(upd: types.ChatMemberUpdated):
    chat = upd.chat
    new_status = upd.new_chat_member.status
    if chat.type not in ("channel", "supergroup", "group"):
        return
    if new_status in ("administrator", "creator"):
        upsert_channel(chat.id, chat.title or "", chat.username or "")
        log.info("Tracking channel %s (%s) — status=%s", chat.title, chat.id, new_status)
    else:
        remove_channel(chat.id)
        log.info("Dropped channel %s (%s) — status=%s", chat.title, chat.id, new_status)


@bot.channel_post_handler(
    content_types=[
        "text",
        "photo",
        "video",
        "document",
        "audio",
        "animation",
        "voice",
        "video_note",
        "sticker",
        "poll",
    ]
)
def on_channel_post(message):
    # Telegram only delivers channel_post updates for channels where the bot is admin
    # (and admin lacks the "messages" privacy filter). Use this to catch up on channels
    # we were already admin in before the bot started running.
    chat = message.chat
    if chat.type != "channel":
        return
    existing = {c["chat_id"] for c in list_channels()}
    if chat.id not in existing:
        upsert_channel(chat.id, chat.title or "", chat.username or "")
        log.info("Auto-registered channel %s (%s) from channel_post", chat.title, chat.id)


def _forwarded_chat(message):
    chat = getattr(message, "forward_from_chat", None)
    if chat is not None:
        return chat
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        return getattr(origin, "chat", None)
    return None


@bot.message_handler(commands=["start", "help"])
@authorized
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "Привет! Я бот для отложенных постов в каналы.\n\n"
        "Команды:\n"
        "/new — создать пост\n"
        "/channels — список каналов, где я админ\n"
        "/add @username — добавить канал вручную\n"
        "/jobs — запланированные посты\n"
        "/cancel_job <id> — отменить запланированный пост\n"
        "/cancel — сбросить текущий диалог\n\n"
        "Добавьте меня администратором в нужный канал, чтобы он появился в списке.",
    )


@bot.message_handler(commands=["cancel"])
@authorized
def cmd_cancel(message):
    bot.delete_state(message.from_user.id, message.chat.id)
    bot.send_message(message.chat.id, "Диалог сброшен.")


@bot.message_handler(commands=["channels"])
@authorized
def cmd_channels(message):
    chans = list_channels()
    if not chans:
        bot.send_message(
            message.chat.id,
            "Пока нет каналов.\n\n"
            "Telegram присылает событие «бот стал админом» только в момент изменения "
            "статуса. Если я уже был админом до запуска — событие не придёт. Варианты:\n"
            "• опубликуйте любое сообщение в канал — я зарегистрирую его автоматически;\n"
            "• перешлите любой пост из канала мне в личку;\n"
            "• используйте /add @username канала;\n"
            "• передобавьте меня в админы (снять и снова назначить).",
        )
        return
    lines = [f"• {c['title'] or c['chat_id']} ({c['chat_id']})" for c in chans]
    bot.send_message(message.chat.id, "Каналы:\n" + "\n".join(lines))


@bot.message_handler(commands=["add"])
@authorized
def cmd_add(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /add @username или /add -100123456789")
        return
    ident = parts[1].strip()
    try:
        chat = bot.get_chat(ident)
    except Exception as e:
        bot.reply_to(message, f"Не могу найти чат: {e}")
        return
    try:
        me = bot.get_chat_member(chat.id, bot.get_me().id)
    except Exception as e:
        bot.reply_to(message, f"Не могу проверить мой статус: {e}")
        return
    if me.status in ("administrator", "creator"):
        upsert_channel(chat.id, chat.title or "", chat.username or "")
        bot.reply_to(message, f"Канал «{chat.title}» добавлен.")
    else:
        bot.reply_to(message, "Я не админ в этом канале. Сначала сделайте меня админом.")


@bot.message_handler(commands=["jobs"])
@authorized
def cmd_jobs(message):
    jobs = scheduler.get_jobs()
    if not jobs:
        bot.send_message(message.chat.id, "Нет запланированных постов.")
        return
    lines = []
    for j in jobs:
        when = (
            j.next_run_time.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")
            if j.next_run_time
            else "?"
        )
        chat_id = j.args[0] if j.args else "?"
        lines.append(f"• {j.id} → {when} (chat {chat_id})")
    bot.send_message(message.chat.id, "Запланированные посты:\n" + "\n".join(lines))


@bot.message_handler(commands=["cancel_job"])
@authorized
def cmd_cancel_job(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /cancel_job <job_id>")
        return
    job_id = parts[1].strip()
    try:
        scheduler.remove_job(job_id)
        bot.reply_to(message, f"Удалено: {job_id}")
    except Exception as e:
        bot.reply_to(message, f"Не получилось: {e}")


@bot.message_handler(commands=["new"])
@authorized
def cmd_new(message):
    chans = list_channels()
    if not chans:
        bot.send_message(
            message.chat.id,
            "Нет каналов. Посмотрите /channels — там подсказка, как зарегистрировать "
            "канал, в котором я уже админ.",
        )
        return
    bot.set_state(message.from_user.id, PostStates.choosing_channels, message.chat.id)
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data["channels"] = []
        data["buttons"] = []
    bot.send_message(
        message.chat.id,
        "Выберите каналы для публикации (можно несколько):",
        reply_markup=channels_kb([]),
    )


def channels_kb(selected):
    kb = types.InlineKeyboardMarkup()
    for c in list_channels():
        mark = "✅ " if c["chat_id"] in selected else ""
        kb.add(
            types.InlineKeyboardButton(
                f"{mark}{c['title'] or c['chat_id']}",
                callback_data=f"ch:{c['chat_id']}",
            )
        )
    kb.add(types.InlineKeyboardButton("➡️ Далее", callback_data="ch:done"))
    return kb


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("ch:"),
    state=PostStates.choosing_channels,
)
@authorized
def cb_channels(call):
    payload = call.data.split(":", 1)[1]
    with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
        selected = set(data.get("channels", []))
        if payload == "done":
            if not selected:
                bot.answer_callback_query(call.id, "Выберите хотя бы один канал.")
                return
            data["channels"] = list(selected)
            bot.set_state(
                call.from_user.id, PostStates.waiting_text, call.message.chat.id
            )
            bot.edit_message_text(
                "Каналы выбраны. Теперь отправьте текст поста (поддерживается HTML).",
                call.message.chat.id,
                call.message.message_id,
            )
            bot.answer_callback_query(call.id)
            return
        try:
            chat_id = int(payload)
        except ValueError:
            bot.answer_callback_query(call.id)
            return
        if chat_id in selected:
            selected.remove(chat_id)
        else:
            selected.add(chat_id)
        data["channels"] = list(selected)
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=channels_kb(selected),
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.message_handler(state=PostStates.waiting_text, content_types=["text"])
@authorized
def on_text(message):
    if message.text.startswith("/"):
        bot.reply_to(message, "Похоже на команду. Сначала /cancel, если хотите выйти.")
        return
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data["text"] = message.html_text or message.text
        data["buttons"] = []
    bot.set_state(message.from_user.id, PostStates.waiting_buttons, message.chat.id)
    bot.send_message(
        message.chat.id,
        "Текст сохранён. Теперь добавьте inline-кнопки по одной за сообщение в формате:\n"
        "<code>Текст кнопки | https://example.com</code>\n\n"
        "Команды: /skip — без кнопок, /done — завершить добавление.",
        parse_mode="HTML",
    )


@bot.message_handler(state=PostStates.waiting_buttons, commands=["skip", "done"])
@authorized
def on_buttons_done(message):
    ask_time(message)


@bot.message_handler(state=PostStates.waiting_buttons, content_types=["text"])
@authorized
def on_button_line(message):
    line = message.text.strip()
    if "|" not in line:
        bot.send_message(
            message.chat.id,
            "Формат: <code>Текст | https://...</code>. Или /done.",
            parse_mode="HTML",
        )
        return
    text, url = [p.strip() for p in line.split("|", 1)]
    if not text or not url.startswith(("http://", "https://", "tg://")):
        bot.send_message(message.chat.id, "Нужен непустой текст и корректный URL.")
        return
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data.setdefault("buttons", []).append({"text": text, "url": url})
        n = len(data["buttons"])
    bot.send_message(message.chat.id, f"Добавлена кнопка #{n}. Ещё одна или /done.")


def ask_time(message):
    bot.set_state(message.from_user.id, PostStates.waiting_time, message.chat.id)
    bot.send_message(
        message.chat.id,
        f"Когда отправить пост?\n"
        f"Формат: <code>DD.MM.YYYY HH:MM</code> (часовой пояс {TIMEZONE}).\n"
        f"Или напишите <code>сейчас</code> для немедленной отправки.",
        parse_mode="HTML",
    )


@bot.message_handler(state=PostStates.waiting_time, content_types=["text"])
@authorized
def on_time(message):
    raw = message.text.strip()
    if raw.lower() in ("сейчас", "now"):
        when = datetime.now(TIMEZONE)
    else:
        try:
            when = datetime.strptime(raw, "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE)
        except ValueError:
            bot.send_message(
                message.chat.id,
                "Не понял формат. Пример: <code>25.05.2026 14:30</code> или <code>сейчас</code>.",
                parse_mode="HTML",
            )
            return
        if when <= datetime.now(TIMEZONE):
            bot.send_message(
                message.chat.id, "Время уже прошло. Введите будущее время."
            )
            return

    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data["when"] = when.isoformat()
        channels = data["channels"]
        text = data["text"]
        buttons = data.get("buttons", [])

    bot.set_state(message.from_user.id, PostStates.confirming, message.chat.id)
    preview = build_preview(text, buttons, channels, when)
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Запланировать", callback_data="confirm:yes"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="confirm:no"),
    )
    bot.send_message(
        message.chat.id,
        preview,
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def build_preview(text, buttons, channels, when):
    chan_titles = [
        (c["title"] or str(c["chat_id"]))
        for c in list_channels()
        if c["chat_id"] in channels
    ]
    btn_lines = (
        "\n".join(f"• {b['text']} → {b['url']}" for b in buttons) if buttons else "—"
    )
    return (
        f"<b>Предпросмотр</b>\n"
        f"Каналы: {', '.join(chan_titles)}\n"
        f"Время: {when.strftime('%d.%m.%Y %H:%M')} ({TIMEZONE})\n"
        f"Кнопки:\n{btn_lines}\n\n"
        f"<b>Текст:</b>\n{text}"
    )


@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith("confirm:"),
    state=PostStates.confirming,
)
@authorized
def cb_confirm(call):
    decision = call.data.split(":", 1)[1]
    if decision == "no":
        bot.delete_state(call.from_user.id, call.message.chat.id)
        bot.edit_message_text(
            "Отменено.", call.message.chat.id, call.message.message_id
        )
        bot.answer_callback_query(call.id)
        return

    with bot.retrieve_data(call.from_user.id, call.message.chat.id) as data:
        channels = data["channels"]
        text = data["text"]
        buttons = data.get("buttons", [])
        when = datetime.fromisoformat(data["when"])

    scheduled = []
    for ch in channels:
        job = scheduler.add_job(
            send_post,
            trigger=DateTrigger(run_date=when),
            args=[ch, text, buttons],
            misfire_grace_time=3600,
            replace_existing=False,
        )
        scheduled.append(job.id)

    bot.delete_state(call.from_user.id, call.message.chat.id)
    bot.edit_message_text(
        "Запланировано постов: {n} на {when}.\nID: {ids}".format(
            n=len(scheduled),
            when=when.strftime("%d.%m.%Y %H:%M"),
            ids=", ".join(scheduled) or "—",
        ),
        call.message.chat.id,
        call.message.message_id,
    )
    bot.answer_callback_query(call.id)


def send_post(chat_id, text, buttons):
    kb = None
    if buttons:
        kb = types.InlineKeyboardMarkup()
        for b in buttons:
            kb.add(types.InlineKeyboardButton(b["text"], url=b["url"]))
    try:
        bot.send_message(
            chat_id,
            text,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        log.info("Posted to %s", chat_id)
    except Exception as e:
        log.exception("Failed to post to %s: %s", chat_id, e)


@bot.message_handler(
    func=lambda m: _forwarded_chat(m) is not None,
    content_types=[
        "text",
        "photo",
        "video",
        "document",
        "audio",
        "animation",
        "voice",
    ],
)
@authorized
def on_forward(message):
    chat = _forwarded_chat(message)
    if chat is None or chat.type != "channel":
        return
    try:
        me = bot.get_chat_member(chat.id, bot.get_me().id)
    except Exception as e:
        bot.reply_to(message, f"Не могу проверить мой статус в «{chat.title}»: {e}")
        return
    if me.status in ("administrator", "creator"):
        upsert_channel(chat.id, chat.title or "", chat.username or "")
        bot.reply_to(message, f"Канал «{chat.title}» добавлен.")
    else:
        bot.reply_to(message, f"Я не админ в «{chat.title}».")


def main():
    init_db()
    bot.add_custom_filter(StateFilter(bot))
    scheduler.start()
    log.info("Bot is starting (tz=%s, owner=%s)", TIMEZONE, OWNER_ID)
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        allowed_updates=util.update_types,
    )


if __name__ == "__main__":
    main()
