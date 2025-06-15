from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from db.base import DBConnection
from db.templates import save_template, get_templates, get_template, delete_template
from handlers.export import show_filters_menu
from handlers.filters import get_default_filters
from handlers.utils import ADMIN_FILTER


async def list_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with DBConnection() as db:
        templates = get_templates(update.effective_user.id, db=db)

    if not templates:
        await update.message.reply_text("⚠️ Шаблоны не найдены")
        return

    keyboard = []
    for tpl in templates:
        keyboard.append([
            InlineKeyboardButton(
                tpl["name"], callback_data=f"tpl_apply_{tpl['id']}"
            ),
            InlineKeyboardButton(
                "🗑", callback_data=f"tpl_del_{tpl['id']}"
            ),
        ])
    await update.message.reply_text(
        "📑 Ваши шаблоны:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def start_save_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filters = context.user_data.get("export_filters")
    if not filters:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "⚠️ Фильтры не найдены"
        )
        return
    context.user_data["save_template_filters"] = filters
    context.user_data["awaiting_template_name"] = True
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "Введите название шаблона:" )


async def save_template_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_template_name"):
        return
    name = update.message.text.strip()
    filters = context.user_data.pop("save_template_filters", get_default_filters())
    context.user_data.pop("awaiting_template_name", None)
    with DBConnection() as db:
        save_template(update.effective_user.id, name, filters, db=db)
    await update.message.reply_text("✅ Шаблон сохранен")


async def apply_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    template_id = int(query.data.split("_")[2])
    with DBConnection() as db:
        filters = get_template(query.from_user.id, template_id, db=db)
    if not filters:
        await query.edit_message_text("⚠️ Шаблон не найден")
        return
    context.user_data["export_filters"] = filters
    await show_filters_menu(update, context)


async def remove_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    template_id = int(query.data.split("_")[2])
    with DBConnection() as db:
        delete_template(query.from_user.id, template_id, db=db)
    await query.edit_message_text("🗑 Шаблон удален")


def register_template_handlers(app):
    app.add_handler(CommandHandler("templates", list_templates, filters=ADMIN_FILTER))
    app.add_handler(CallbackQueryHandler(start_save_template, pattern="^save_template$"))
    app.add_handler(MessageHandler(filters.TEXT & ADMIN_FILTER, save_template_name), group=1)
    app.add_handler(CallbackQueryHandler(apply_template, pattern=r"^tpl_apply_\d+$"))
    app.add_handler(CallbackQueryHandler(remove_template, pattern=r"^tpl_del_\d+$"))