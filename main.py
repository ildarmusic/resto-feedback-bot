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

# Явно грузим .env (стабильнее на Python 3.13)
load_dotenv(dotenv_path=".env")

def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "").strip()
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}

def _is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in _admin_ids()

# Состояния диалога (дату больше не спрашиваем)
DISH, COMMENT, REPLY, EDIT_REPLY, BULK_DISHES = range(5)

# Постоянная кнопка внизу чата
MAIN_MENU = ReplyKeyboardMarkup([["➕ Новая запись"]], resize_keyboard=True)


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
        [[InlineKeyboardButton("✏️ Добавить/Редактировать ответ кухни", callback_data=f"edit:{fid}")]]
    )

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
    await update.message.reply_text(f"✅ Добавил: {name}", reply_markup=MAIN_MENU)

async def dbulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")
    await update.message.reply_text(
        "Отправьте одним сообщением список блюд (по одному в строке).",
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

    await update.message.reply_text(f"✅ Импортировал блюд: {added}", reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def dlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")
    db: DB = context.application.bot_data["db"]
    # простой подсчёт
    row = await db.pool.fetchrow("SELECT COUNT(*) AS c FROM dishes")  # type: ignore
    await update.message.reply_text(f"🍽 Блюд в базе: {row['c']}", reply_markup=MAIN_MENU)

async def ddel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")

    name = " ".join(context.args).strip()
    if not name:
        return await update.message.reply_text("Использование: /ddel Название блюда")

    db: DB = context.application.bot_data["db"]
    await db.pool.execute("DELETE FROM dishes WHERE name=$1", name)  # type: ignore
    await update.message.reply_text(f"🗑 Удалил (если было): {name}", reply_markup=MAIN_MENU)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Начало новой записи.
    Дату берём автоматически из времени сообщения Telegram.
    """
    msg_date = update.message.date.astimezone()  # локальная TZ системы
    context.user_data["date_obj"] = msg_date.date()
    context.user_data["date_str"] = msg_date.strftime("%d/%m/%y")

    await update.message.reply_text(
        "Записываем ОС.\n\n1) Введите 2+ буквы названия блюда (будут подсказки):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DISH


async def new_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Запуск сценария по кнопке "➕ Новая запись"
    return await start(update, context)


async def get_dish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: DB = context.application.bot_data["db"]
    text = (update.message.text or "").strip()

    if len(text) < 2:
        await update.message.reply_text("Нужно минимум 2 буквы. Повторите:")
        return DISH

    options = await db.search_dishes(text, limit=10)

    # Если нашли варианты — показываем кнопки или принимаем точное совпадение
    if options:
        for o in options:
            if o.lower() == text.lower():
                context.user_data["dish"] = o
                await update.message.reply_text("2) Комментарий гостя:", reply_markup=ReplyKeyboardRemove())
                return COMMENT

        await update.message.reply_text(
            "Выберите блюдо кнопкой (или допишите точнее):",
            reply_markup=dish_keyboard(options),
        )
        return DISH

    # Если вариантов нет — принимаем текст как новое блюдо
    context.user_data["dish"] = text
    await update.message.reply_text("2) Комментарий гостя:", reply_markup=ReplyKeyboardRemove())
    return COMMENT


async def get_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Комментарий не должен быть пустым. Повторите:")
        return COMMENT

    context.user_data["comment"] = text
    await update.message.reply_text("3) Ответ кухни (или /skip чтобы пропустить):")
    return REPLY


async def get_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Ответ пустой. Введите текст или /skip:")
        return REPLY
    return await finalize(update, context, kitchen_reply=text)


async def skip_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await finalize(update, context, kitchen_reply=None)


async def finalize(update: Update, context: ContextTypes.DEFAULT_TYPE, kitchen_reply: str | None):
    db: DB = context.application.bot_data["db"]

    date_str = context.user_data["date_str"]
    date_obj = context.user_data["date_obj"]
    dish = context.user_data["dish"]
    comment = context.user_data["comment"]

    await db.upsert_dish(dish)
    fid = await db.create_feedback(date_obj, dish, comment, kitchen_reply)

    # Карточка ОС (с кнопкой редактирования)
    msg = await update.message.reply_text(
        card_text(fid, date_str, dish, comment, kitchen_reply),
        reply_markup=card_keyboard(fid),
    )
    await db.set_message_refs(fid, msg.chat_id, msg.message_id)

    # Запись в Google Sheets
    await asyncio.to_thread(sheets.append_feedback_row, fid, date_str, dish, comment, kitchen_reply)

    # Меню для быстрого старта следующей записи
    await update.message.reply_text(
        "Готово ✅ Нажмите «➕ Новая запись», чтобы добавить следующую.",
        reply_markup=MAIN_MENU,
    )

    context.user_data.clear()
    return ConversationHandler.END


async def on_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    fid = int((q.data or "").split(":", 1)[1])
    context.user_data["edit_fid"] = fid
    await q.message.reply_text("Введите ответ кухни (сообщением):")
    return EDIT_REPLY


async def save_edited_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: DB = context.application.bot_data["db"]
    fid = int(context.user_data["edit_fid"])

    reply_text = (update.message.text or "").strip()
    if not reply_text:
        await update.message.reply_text("Ответ не должен быть пустым. Введите ещё раз:")
        return EDIT_REPLY

    await db.update_kitchen_reply(fid, reply_text)
    row = await db.get_feedback(fid)
    if not row:
        await update.message.reply_text("Запись не найдена.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    date_str = row["feedback_date"].strftime("%d/%m/%y")
    dish = row["dish_name"]
    comment = row["guest_comment"]
    reply = row["kitchen_reply"]
    chat_id = row["telegram_chat_id"]
    message_id = row["telegram_message_id"]

    # Обновляем сообщение-карточку
    await context.application.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=card_text(fid, date_str, dish, comment, reply),
        reply_markup=card_keyboard(fid),
    )

    # Обновляем строку в Google Sheets
    await asyncio.to_thread(sheets.update_feedback_row, fid, date_str, dish, comment, reply)

    await update.message.reply_text("Обновил ✅", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Ок, отменил.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


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

    # Сценарий создания новой ОС
    new_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("new", start)],
        states={
            DISH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_dish)],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_comment)],
            REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_reply),
                CommandHandler("skip", skip_reply),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # Сценарий редактирования ответа кухни (кнопка на карточке)
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_edit_button, pattern=r"^edit:\d+$")],
        states={EDIT_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_reply)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_message=True,
    )

bulk_conv = ConversationHandler(
    entry_points=[CommandHandler("dbulk", dbulk)],
    states={BULK_DISHES: [MessageHandler(filters.TEXT & ~filters.COMMAND, dbulk_receive)]},
    fallbacks=[CommandHandler("cancel", cancel)],
    allow_reentry=True,
)

    app.add_handler(bulk_conv)
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(new_conv)
    app.add_handler(edit_conv)
    app.add_handler(CommandHandler("dadd", dadd))
    app.add_handler(CommandHandler("dlist", dlist))
    app.add_handler(CommandHandler("ddel", ddel))

    # Кнопка меню "➕ Новая запись" (без команд)
    app.add_handler(MessageHandler(filters.Regex(r"^➕ Новая запись$"), new_from_button))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

