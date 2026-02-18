import os
import asyncio
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from db import DB
import sheets

load_dotenv(dotenv_path=".env")


# ---------- Admin helpers ----------
def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "").strip()
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in _admin_ids())


# ---------- Conversation states ----------
# Добавили DISH_CONFIRM_NEW, чтобы не принимать “новое блюдо” без подтверждения
DISH, DISH_CONFIRM_NEW, COMMENT, REPLY, EDIT_REPLY, BULK_DISHES = range(6)


# ---------- Cleanup helpers ----------
# Мы трекаем все промежуточные сообщения (и ваши, и бота), чтобы потом удалить.
def _cleanup_list(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[int, int]]:
    return context.user_data.setdefault("cleanup_ids", [])


def _track(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    _cleanup_list(context).append((chat_id, message_id))


async def _track_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        _track(context, update.message.chat_id, update.message.message_id)


async def _send_tracked(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    **kwargs,
):
    msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, **kwargs)
    _track(context, msg.chat_id, msg.message_id)
    return msg


async def _cleanup_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    items = context.user_data.get("cleanup_ids", [])
    for chat_id, message_id in reversed(items):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    context.user_data["cleanup_ids"] = []


# ---------- UI helpers ----------
def dish_keyboard(options: list[str]) -> ReplyKeyboardMarkup:
    rows, row = [], []
    for i, name in enumerate(options, start=1):
        row.append(name)
        if i % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def confirm_new_dish_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["➕ Добавить как новое", "🔎 Попробовать ещё раз"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def card_text(fid: int, date_str: str, dish: str, comment: str, reply: str | None) -> str:
    rep = reply if reply else "— (пока нет ответа кухни)"
    return (
        f"🧾 ОС #{fid}\n"
        f"📅 Дата: {date_str}\n"
        f"🍽 Блюдо: {dish}\n\n"
        f"💬 Комментарий гостя:\n{comment}\n\n"
        f"👨‍🍳 Ответ кухни:\n{rep}"
    )


def card_keyboard(fid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Ответ кухни", callback_data=f"edit:{fid}"),
                InlineKeyboardButton("➕ Новая запись", callback_data="new"),
            ],
            [
                InlineKeyboardButton("🗑 Удалить запись", callback_data=f"delask:{fid}"),
                InlineKeyboardButton("❓ Помощь", callback_data="help"),
            ],
        ]
    )


# ---------- Common helpers ----------
def _set_auto_date(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now().astimezone()
    context.user_data["date_obj"] = now.date()
    context.user_data["date_str"] = now.strftime("%d/%m/%y")


def _norm(s: str) -> str:
    # нормализация: пробелы, регистр, ё->е
    s = " ".join((s or "").strip().split()).lower()
    s = s.replace("ё", "е")
    return s


async def search_dishes_strict(db: DB, query: str, limit: int = 10) -> list[str]:
    """
    Очень тщательный поиск:
    1) пробуем db.search_dishes(query)
    2) если пусто — ищем по каждому слову (AND) через SQL LIKE %word%
    3) если всё равно пусто — fallback по первому слову
    """
    q = _norm(query)
    if len(q) < 2:
        return []

    opts: list[str] = []
    # 1) базовый поиск через твой метод
    try:
        opts = await db.search_dishes(q, limit=limit)
    except Exception:
        opts = []

    # 2) по словам (AND), если ничего не нашли
    if not opts:
        parts = [p for p in q.split(" ") if len(p) >= 2]
        if parts:
            conds = " AND ".join([f"replace(lower(name),'ё','е') LIKE ${i+1}" for i in range(len(parts))])
            params = [f"%{p}%" for p in parts] + [limit]
            sql = f"""
                SELECT name
                FROM dishes
                WHERE {conds}
                ORDER BY name
                LIMIT ${len(parts)+1}
            """
            rows = await db.pool.fetch(sql, *params)  # type: ignore
            opts = [r["name"] for r in rows]

    # 3) fallback по первому слову
    if not opts:
        first = q.split(" ")[0]
        if len(first) >= 2:
            rows = await db.pool.fetch(
                """
                SELECT name
                FROM dishes
                WHERE replace(lower(name),'ё','е') LIKE $1
                ORDER BY name
                LIMIT $2
                """,
                f"%{first}%",
                limit,
            )  # type: ignore
            opts = [r["name"] for r in rows]

    # уникализируем, сохраняем порядок
    seen = set()
    uniq: list[str] = []
    for x in opts:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:limit]


# ---------- Help ----------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🤖 Помощь\n\n"
        "📝 Запись обратной связи:\n"
        "• /start или /new — начать новую запись\n"
        "• /skip — пропустить ответ кухни\n"
        "• /cancel — отменить текущий шаг\n\n"
        "На карточке:\n"
        "• ✏️ Ответ кухни — добавить/изменить позже\n"
        "• ➕ Новая запись — начать следующую\n"
        "• 🗑 Удалить запись — удалить из базы и таблицы\n\n"
        "🍽 Блюда (для админов):\n"
        "• /dbulk — загрузить список блюд (по одному в строке)\n"
        "• /dadd Название — добавить блюдо\n"
        "• /ddel Название — удалить блюдо\n"
        "• /dlist — сколько блюд в базе\n"
        "• /whoami — ваш user_id\n"
    )
    await update.message.reply_text(txt)


async def help_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Напишите /help — покажу все команды и подсказки.")


# ---------- Admin commands (optional) ----------
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш user_id: {update.effective_user.id}")


async def dadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")
    name = " ".join(context.args).strip()
    if not name:
        return await update.message.reply_text("Использование: /dadd Название блюда")
    db: DB = context.application.bot_data["db"]
    await db.upsert_dish(name)
    await update.message.reply_text(f"✅ Добавил: {name}")


async def ddel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")
    name = " ".join(context.args).strip()
    if not name:
        return await update.message.reply_text("Использование: /ddel Название блюда")
    db: DB = context.application.bot_data["db"]
    await db.pool.execute("DELETE FROM dishes WHERE name=$1", name)  # type: ignore
    await update.message.reply_text(f"🗑 Удалил (если было): {name}")


async def dlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")
    db: DB = context.application.bot_data["db"]
    row = await db.pool.fetchrow("SELECT COUNT(*) AS c FROM dishes")  # type: ignore
    await update.message.reply_text(f"🍽 Блюд в базе: {row['c']}")


async def dbulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")
    await update.message.reply_text(
        "Пришлите одним сообщением список блюд (по одному в строке).",
        reply_markup=ReplyKeyboardRemove(),
    )
    return BULK_DISHES


async def dbulk_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        await update.message.reply_text("Пусто. Пришлите список блюд строками.")
        return BULK_DISHES

    db: DB = context.application.bot_data["db"]
    added = 0
    for name in lines:
        await db.upsert_dish(name)
        added += 1
    await update.message.reply_text(f"✅ Импортировал блюд: {added}")
    return ConversationHandler.END


# ---------- Main flow ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _set_auto_date(context)

    await _track_user_message(update, context)
    await _send_tracked(
        update,
        context,
        "Записываем ОС.\n\n1) Введите слово/буквы из названия блюда (найду варианты в базе):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DISH


async def start_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data["cleanup_ids"] = []
    _set_auto_date(context)

    await _send_tracked(
        update,
        context,
        "Записываем ОС.\n\n1) Введите слово/буквы из названия блюда (найду варианты в базе):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DISH


async def get_dish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: DB = context.application.bot_data["db"]
    text_raw = (update.message.text or "").strip()

    await _track_user_message(update, context)

    # если человек нажал кнопки подтверждения “новое блюдо” не в том шаге — мягко вернём
    if text_raw in ("➕ Добавить как новое", "🔎 Попробовать ещё раз"):
        await _send_tracked(update, context, "Введите слово/буквы из названия блюда:", reply_markup=ReplyKeyboardRemove())
        return DISH

    q = _norm(text_raw)
    if len(q) < 2:
        await _send_tracked(update, context, "Нужно минимум 2 символа. Повторите:")
        return DISH

    # Тщательный поиск
    options = await search_dishes_strict(db, q, limit=10)

    # Если совпадения есть — ВСЕГДА показываем варианты и НЕ пропускаем дальше,
    # пока не будет точного совпадения (или выбора)
    if options:
        for o in options:
            if _norm(o) == q:
                context.user_data["dish"] = o
                await _send_tracked(update, context, "2) Комментарий гостя:", reply_markup=ReplyKeyboardRemove())
                return COMMENT

        await _send_tracked(
            update,
            context,
            "Нашёл совпадения. Выберите блюдо кнопкой (или уточните запрос):",
            reply_markup=dish_keyboard(options),
        )
        return DISH

    # Совпадений нет — НЕ принимаем молча как новое, просим подтверждение
    context.user_data["pending_dish"] = text_raw
    await _send_tracked(
        update,
        context,
        f"Не нашёл совпадений в базе для: «{text_raw}».\nДобавить как новое блюдо или попробовать ещё раз?",
        reply_markup=confirm_new_dish_keyboard(),
    )
    return DISH_CONFIRM_NEW


async def dish_confirm_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = (update.message.text or "").strip()
    await _track_user_message(update, context)

    if choice == "🔎 Попробовать ещё раз":
        await _send_tracked(update, context, "Ок. Введите слово/буквы из названия блюда ещё раз:", reply_markup=ReplyKeyboardRemove())
        return DISH

    if choice == "➕ Добавить как новое":
        dish = (context.user_data.get("pending_dish") or "").strip()
        if not dish:
            await _send_tracked(update, context, "Не понял название. Введите блюдо ещё раз:", reply_markup=ReplyKeyboardRemove())
            return DISH
        context.user_data["dish"] = dish
        context.user_data.pop("pending_dish", None)
        await _send_tracked(update, context, "2) Комментарий гостя:", reply_markup=ReplyKeyboardRemove())
        return COMMENT

    await _send_tracked(update, context, "Выберите действие кнопкой:", reply_markup=confirm_new_dish_keyboard())
    return DISH_CONFIRM_NEW


async def get_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    await _track_user_message(update, context)

    if not text:
        await _send_tracked(update, context, "Комментарий не должен быть пустым. Повторите:")
        return COMMENT

    context.user_data["comment"] = text
    await _send_tracked(update, context, "3) Ответ кухни (или /skip чтобы пропустить):")
    return REPLY


async def get_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    await _track_user_message(update, context)

    if not text:
        await _send_tracked(update, context, "Ответ пустой. Введите текст или /skip:")
        return REPLY

    return await finalize(update, context, kitchen_reply=text)


async def skip_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user_message(update, context)
    return await finalize(update, context, kitchen_reply=None)


async def finalize(update: Update, context: ContextTypes.DEFAULT_TYPE, kitchen_reply: str | None):
    db: DB = context.application.bot_data["db"]

    date_str = context.user_data["date_str"]
    date_obj = context.user_data["date_obj"]
    dish = context.user_data["dish"]
    comment = context.user_data["comment"]

    await db.upsert_dish(dish)
    fid = await db.create_feedback(date_obj, dish, comment, kitchen_reply)

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=card_text(fid, date_str, dish, comment, kitchen_reply),
        reply_markup=card_keyboard(fid),
    )
    await db.set_message_refs(fid, msg.chat_id, msg.message_id)

    await asyncio.to_thread(sheets.append_feedback_row, fid, date_str, dish, comment, kitchen_reply)

    await _cleanup_messages(context)
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user_message(update, context)
    await _cleanup_messages(context)
    context.user_data.clear()
    return ConversationHandler.END


# ---------- Edit flow ----------
async def on_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    fid = int(q.data.split(":", 1)[1])
    context.user_data["edit_fid"] = fid
    await _send_tracked(update, context, "Введите ответ кухни (сообщением):")
    return EDIT_REPLY


async def save_edited_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: DB = context.application.bot_data["db"]
    fid = int(context.user_data["edit_fid"])

    reply_text = (update.message.text or "").strip()
    await _track_user_message(update, context)

    if not reply_text:
        await _send_tracked(update, context, "Ответ не должен быть пустым. Введите ещё раз:")
        return EDIT_REPLY

    await db.update_kitchen_reply(fid, reply_text)
    row = await db.get_feedback(fid)
    if not row:
        await _cleanup_messages(context)
        context.user_data.clear()
        return ConversationHandler.END

    date_str = row["feedback_date"].strftime("%d/%m/%y")
    dish = row["dish_name"]
    comment = row["guest_comment"]
    reply = row["kitchen_reply"]
    chat_id = row["telegram_chat_id"]
    message_id = row["telegram_message_id"]

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=card_text(fid, date_str, dish, comment, reply),
        reply_markup=card_keyboard(fid),
    )

    await asyncio.to_thread(sheets.update_feedback_row, fid, date_str, dish, comment, reply)

    await _cleanup_messages(context)
    context.user_data.clear()
    return ConversationHandler.END


# ---------- Delete flow ----------
def delete_confirm_keyboard(fid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"del:{fid}"),
            InlineKeyboardButton("✖️ Отмена", callback_data=f"delcancel:{fid}"),
        ]]
    )


async def on_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    fid = int(q.data.split(":", 1)[1])

    await q.message.reply_text(
        f"Удалить запись ОС #{fid}?",
        reply_markup=delete_confirm_keyboard(fid),
    )


async def on_delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass


async def on_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    fid = int(q.data.split(":", 1)[1])

    db: DB = context.application.bot_data["db"]
    row = await db.get_feedback(fid)

    # 1) СРАЗУ убираем сообщение подтверждения
    try:
        await q.message.delete()
    except Exception:
        pass

    if not row:
        return

    chat_id = row["telegram_chat_id"]
    message_id = row["telegram_message_id"]

    # 2) Удаляем карточку
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

    # 3) Удаляем строку в Sheets
    try:
        await asyncio.to_thread(sheets.delete_feedback_row, fid)
    except Exception:
        pass

    # 4) Удаляем из БД (последним)
    await db.delete_feedback(fid)


# ---------- Lifecycle ----------
async def on_startup(app: Application):
    db = DB(os.environ["DATABASE_URL"])
    await db.connect()
    app.bot_data["db"] = db


async def on_shutdown(app: Application):
    db: DB = app.bot_data.get("db")
    if db:
        await db.close()


def main():
    app = (
        Application.builder()
        .token(os.environ["TELEGRAM_TOKEN"])
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    new_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("new", start),
            CallbackQueryHandler(start_from_callback, pattern=r"^new$"),
        ],
        states={
            DISH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_dish)],
            DISH_CONFIRM_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, dish_confirm_new)],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_comment)],
            REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_reply),
                CommandHandler("skip", skip_reply),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_edit_button, pattern=r"^edit:\d+$")],
        states={EDIT_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_reply)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    bulk_conv = ConversationHandler(
        entry_points=[CommandHandler("dbulk", dbulk)],
        states={BULK_DISHES: [MessageHandler(filters.TEXT & ~filters.COMMAND, dbulk_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(new_conv)
    app.add_handler(edit_conv)
    app.add_handler(bulk_conv)

    app.add_handler(CallbackQueryHandler(on_delete_ask, pattern=r"^delask:\d+$"))
    app.add_handler(CallbackQueryHandler(on_delete_confirm, pattern=r"^del:\d+$"))
    app.add_handler(CallbackQueryHandler(on_delete_cancel, pattern=r"^delcancel:\d+$"))

    app.add_handler(CallbackQueryHandler(help_from_button, pattern=r"^help$"))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("dadd", dadd))
    app.add_handler(CommandHandler("ddel", ddel))
    app.add_handler(CommandHandler("dlist", dlist))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

