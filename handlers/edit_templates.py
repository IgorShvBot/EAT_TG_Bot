from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from db.base import DBConnection
from db.templates import (
    save_edit_template,
    get_edit_templates,
    get_edit_template,
    delete_edit_template,
)
from handlers.edit import build_edit_keyboard
from handlers.utils import ADMIN_FILTER


async def list_edit_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with DBConnection() as db:
        templates = get_edit_templates(update.effective_user.id, db=db)
    if not templates:
        await update.message.reply_text("⚠️ Шаблоны не найдены")
        return

    keyboard = []
    for tpl in templates:
        keyboard.append([
            InlineKeyboardButton(tpl["name"], callback_data=f"etpl_apply_{tpl['id']}") ,
            InlineKeyboardButton("🗑", callback_data=f"etpl_del_{tpl['id']}") ,
        ])
    await update.message.reply_text(
        "📑 Шаблоны редактирования:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def start_save_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    updates = context.user_data.get("edit_mode", {}).get("updates")
    if not updates:
        # На случай если edit_mode очистился после подтверждения
        updates = context.user_data.get("last_edit_updates")
    if not updates:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("⚠️ Нет данных для сохранения")
        return
    fields = {k: v[0] for k, v in updates.items()}
    context.user_data["save_edit_template_fields"] = fields
    context.user_data["awaiting_edit_template_name"] = True
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите название шаблона:")


async def save_edit_template_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_edit_template_name"):
        return
    name = update.message.text.strip()
    fields = context.user_data.pop("save_edit_template_fields", {})
    context.user_data.pop("awaiting_edit_template_name", None)
    with DBConnection() as db:
        save_edit_template(update.effective_user.id, name, fields, db=db)
    # После сохранения очищаем данные редактирования
    context.user_data.pop("edit_mode", None)
    context.user_data.pop("last_edit_updates", None)
    await update.message.reply_text("✅ Шаблон сохранен")


async def apply_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tpl_id = int(query.data.split("_")[2])
    with DBConnection() as db:
        fields = get_edit_template(query.from_user.id, tpl_id, db=db)
    if not fields:
        await query.edit_message_text("⚠️ Шаблон не найден")
        return
    updates = context.user_data.setdefault("edit_mode", {}).setdefault("updates", {})
    for key, value in fields.items():
        updates[key] = (value, "replace")
    await query.edit_message_text(
        "Шаблон применен. Подтвердите изменения или выберите другие поля:",
        reply_markup=build_edit_keyboard(updates, add_confirm=True),
    )


async def remove_edit_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tpl_id = int(query.data.split("_")[2])
    with DBConnection() as db:
        delete_edit_template(query.from_user.id, tpl_id, db=db)
    await query.edit_message_text("🗑 Шаблон удален")


def register_edit_template_handlers(app):
    app.add_handler(CommandHandler("edit_templates", list_edit_templates, filters=ADMIN_FILTER))
    app.add_handler(CallbackQueryHandler(start_save_edit_template, pattern="^edit_save_template$"))
    app.add_handler(MessageHandler(filters.TEXT & ADMIN_FILTER, save_edit_template_name), group=1)
    app.add_handler(CallbackQueryHandler(apply_edit_template, pattern=r"^etpl_apply_\d+$"))
    app.add_handler(CallbackQueryHandler(remove_edit_template, pattern=r"^etpl_del_\d+$"))