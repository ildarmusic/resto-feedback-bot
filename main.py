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


# ---------- Admin helpers ----------
def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "").strip()
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def _is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in _admin_ids())


# ---------- Conversation states ----------
DISH, COMMENT, REPLY, EDIT_REPLY, BULK_DISHES = range(5)


# ---------- Cleanup helpers (remove intermediate messages; keep only final card) ----------
def _cleanup_list(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[int, int]]:
    # list of (chat_id, message_id)
    return context.user_data.setdefault("cleanup_ids", [])


def _track_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    _cleanup_list(context).append((chat_id, message_id))


async def _send_tracked(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    **kwargs,
):
    """
    Send bot message and track it for later deletion.
    Works for both message-based and callback-based updates.
    """
    msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, **kwargs)
    _track_message(context, msg.chat_id, msg.message_id)
    return msg


async def _track_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        _track_message(context, update.message.chat_id, update.message.message_id)


async def _cleanup_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    items = context.user_data.get("cleanup_ids", [])
    # delete from the end
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
            ]
        ]
    )


# ---------- Admin commands ----------
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш user_id: {update.effective_user.id}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Помощь по боту*\n\n"
        "📝 *Запись обратной связи*\n"
        "• `/start` или `/new` — начать новую запись\n"
        "• шаги: блюдо → комментарий → ответ кухни (или /skip)\n"
        "• ✏️ *Ответ кухни* можно добавить позже кнопкой на карточке\n\n"
        "🍽 *Блюда* (доступно админам)\n"
        "• `/dbulk` — загрузить список блюд (по одному в строке)\n"
        "• `/dadd Название` — добавить одно блюдо\n"
        "• `/ddel Название` — удалить блюдо\n"
        "• `/dlist` — сколько блюд в базе\n\n"
        "⚙️ *Сервис*\n"
        "• `/cancel` — отменить текущий шаг\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def dadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")

    name = " ".join(context.args).strip()
    if not name:
        return await update.message.reply_text("Использование: /dadd Название блюда")

    db: DB = context.application.bot_data["db"]
    await db.upsert_dish(name)
    await update.message.reply_text(f"✅ Добавил: {name}")


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

    await update.message.reply_text(f"✅ Импортировал блюд: {added}")
    return ConversationHandler.END


async def dlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")
    db: DB = context.application.bot_data["db"]
    row = await db.pool.fetchrow("SELECT COUNT(*) AS c FROM dishes")  # type: ignore
    await update.message.reply_text(f"🍽 Блюд в базе: {row['c']}")


async def ddel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Недостаточно прав.")

    name = " ".join(context.args).strip()
    if not name:
        return await update.message.reply_text("Использование: /ddel Название блюда")

    db: DB = context.application.bot_data["db"]
    await db.pool.execute("DELETE FROM dishes WHERE name=$1", name)  # type: ignore
    await update.message.reply_text(f"🗑 Удалил (если было): {name}")


# ---------- Main flow ----------
def _set_auto_date(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now().astimezone()
    context.user_data["date_obj"] = now.date()
    context.user_data["date_str"] = now.strftime("%d/%m/%y")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # дата = сейчас (в личке это отлично)
    _set_auto_date(context)

    # трекаем команду/сообщение пользователя и ответ бота
    await _track_user_message(update, context)
    await _send_tracked(
        update,
        context,
        "Записываем ОС.\n\n1) Введи слово или буквы из названия блюда (появятся кнопки с вариантами):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DISH


async def start_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # запуск “Новая запись” с inline-кнопки
    q = update.callback_query
    await q.answer()
    _set_auto_date(context)

    # тут нет user message, трекаем только наш промпт
    await _send_tracked(
        update,
        context,
        "Записываем ОС.\n\n1) Введи слово или буквы из названия блюда (появятся кнопки с вариантами):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DISH


async def get_dish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: DB = context.application.bot_data["db"]
    text = (update.message.text or "").strip()

    await _track_user_message(update, context)

    if len(text) < 2:
        await _send_tracked(update, context, "Нужно минимум 2 буквы. Повторите:")
        return DISH

    options = await db.search_dishes(text, limit=10)

    if options:
        for o in options:
            if o.lower() == text.lower():
                context.user_data["dish"] = o
                await _send_tracked(update, context, "2) Комментарий гостя:", reply_markup=ReplyKeyboardRemove())
                return COMMENT

        await _send_tracked(
            update,
            context,
            "Выберите блюдо кнопкой (или допишите точнее):",
            reply_markup=dish_keyboard(options),
        )
        return DISH

    # если не нашли — принимаем как новое блюдо
    context.user_data["dish"] = text
    await _send_tracked(update, context, "2) Комментарий гостя:", reply_markup=ReplyKeyboardRemove())
    return COMMENT


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

    # 1) Сначала отправляем итоговую карточку (НЕ трекаем — она должна остаться)
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=card_text(fid, date_str, dish, comment, kitchen_reply),
        reply_markup=card_keyboard(fid),
    )
    await db.set_message_refs(fid, msg.chat_id, msg.message_id)

    # 2) Запись в Google Sheets
    await asyncio.to_thread(sheets.append_feedback_row, fid, date_str, dish, comment, kitchen_reply)

    # 3) Удаляем все промежуточные сообщения
    await _cleanup_messages(context)

    context.user_data.clear()
    return ConversationHandler.END


# ---------- Edit flow (kitchen reply) ----------
async def on_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    fid = int((q.data or "").split(":", 1)[1])
    context.user_data["edit_fid"] = fid

    # Просим ввести ответ кухни (это сообщение трекаем и потом удалим)
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
        # чистим “мусор” и просто выходим
        await _cleanup_messages(context)
        context.user_data.clear()
        return ConversationHandler.END

    date_str = row["feedback_date"].strftime("%d/%m/%y")
    dish = row["dish_name"]
    comment = row["guest_comment"]
    reply = row["kitchen_reply"]
    chat_id = row["telegram_chat_id"]
    message_id = row["telegram_message_id"]

    # Обновляем карточку
    await context.application.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=card_text(fid, date_str, dish, comment, reply),
        reply_markup=card_keyboard(fid),
    )

    # Обновляем строку в Google Sheets
    await asyncio.to_thread(sheets.update_feedback_row, fid, date_str, dish, comment, reply)

    # Удаляем промежуточные сообщения (вопрос “Введите ответ...”, ваш ответ и т.п.)
    await _cleanup_messages(context)

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # удалим всё, что накопили в этом “сеансе”
    await _track_user_message(update, context)
    await _cleanup_messages(context)
    context.user_data.clear()
    return ConversationHandler.END


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

    # Основной сценарий (создание ОС)
    new_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("new", start),
            CallbackQueryHandler(start_from_callback, pattern=r"^new$"),
        ],
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

    # Редактирование ответа кухни
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_edit_button, pattern=r"^edit:\d+$")],
        states={EDIT_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edited_reply)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        # per_message=True тут НЕ нужно (и мешает), т.к. у нас ввод текста
    )

    # Bulk-импорт блюд
    bulk_conv = ConversationHandler(
        entry_points=[CommandHandler("dbulk", dbulk)],
        states={BULK_DISHES: [MessageHandler(filters.TEXT & ~filters.COMMAND, dbulk_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(new_conv)
    app.add_handler(edit_conv)
    app.add_handler(bulk_conv)

    # Админ-команды
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("dadd", dadd))
    app.add_handler(CommandHandler("dlist", dlist))
    app.add_handler(CommandHandler("ddel", ddel))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

