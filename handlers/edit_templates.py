from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from db.base import DBConnection
from db.templates import (
    save_edit_template,
    get_edit_templates,
    get_edit_template,
    delete_edit_template,
)
from db.transactions import get_transaction_fields
from handlers.edit import build_edit_keyboard
from handlers.utils import ADMIN_FILTER

EDIT_FIELDS = [
    ("category", "🏷 Категория"),
    ("description", "📝 Описание"),
    ("counterparty", "👥 Контрагент"),
    ("check_num", "🧾 Чек #"),
    ("cash_source", "💳 Наличность"),
    ("target_cash_source", "💸 Наличность (куда)"),
    ("transaction_type", "🔀 Тип"),
    ("transaction_class", "📊 Класс"),
    ("pdf_type", "📄 Тип PDF"),
]


def build_new_template_keyboard(fields: dict, copied_from_id: int | None = None) -> InlineKeyboardMarkup:
    def button_text(label: str, field: str) -> str:
        if field == "copy_from_id" and copied_from_id is not None:
            return f"{label}: {copied_from_id}"
        value = fields.get(field)
        if value is not None:
            short = value if len(str(value)) <= 20 else str(value)[:17] + "..."
            return f"{label}: {short}"
        return label

    keyboard = [
        [InlineKeyboardButton(button_text("📋 Из ID", "copy_from_id"), callback_data="etpl_copy_from_id")]
    ]
    for field, label in EDIT_FIELDS:
        keyboard.append([
            InlineKeyboardButton(button_text(label, field), callback_data=f"etpl_field_{field}")
        ])
    keyboard.append([InlineKeyboardButton("💾 Сохранить", callback_data="etpl_save_new")])
    keyboard.append([InlineKeyboardButton("✖️ Отмена", callback_data="etpl_cancel_new")])
    return InlineKeyboardMarkup(keyboard)


async def create_edit_template_from_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_edit_tpl_id"] = True
    await query.edit_message_text("Введите ID записи для создания шаблона:")


async def list_edit_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with DBConnection() as db:
        templates = get_edit_templates(update.effective_user.id, db=db)
    # if not templates:
    #     await update.message.reply_text("⚠️ Шаблоны не найдены")
    #     return

    # keyboard = []
    keyboard: list[list[InlineKeyboardButton]] = []
    for tpl in templates:
        # keyboard.append([
        #     InlineKeyboardButton(tpl["name"], callback_data=f"etpl_apply_{tpl['id']}") ,
        #     InlineKeyboardButton("🗑", callback_data=f"etpl_del_{tpl['id']}") ,
        # ])
        keyboard.append(
            [
                InlineKeyboardButton(
                    tpl["name"], callback_data=f"etpl_apply_{tpl['id']}"
                ),
                InlineKeyboardButton("🗑", callback_data=f"etpl_del_{tpl['id']}") ,
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("➕ Создать из ID", callback_data="etpl_create_from_id")]
    )

    text = "📑 Шаблоны редактирования:" if templates else "⚠️ Шаблоны не найдены"

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
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


async def start_new_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_new_tpl_id'] = True
    context.user_data.pop('new_tpl_fields', None)
    context.user_data.pop('new_tpl_source_id', None)
    await query.edit_message_text("Введите ID записи, из которой скопировать данные:")


async def handle_new_template_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_new_tpl_id'):
        try:
            tx_id = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("Введите числовой ID")
            return

        with DBConnection() as db:
            fields = get_transaction_fields(tx_id, db=db)

        if not fields:
            await update.message.reply_text("Запись с таким ID не найдена")
            return

        context.user_data['new_tpl_source_id'] = tx_id
        context.user_data['new_tpl_fields'] = {k: v for k, v in fields.items() if v is not None}
        context.user_data['awaiting_new_tpl_id'] = False

        await update.message.reply_text(
            "Отредактируйте поля или сохраните шаблон:",
            reply_markup=build_new_template_keyboard(context.user_data['new_tpl_fields'], tx_id),
        )
        return

    if context.user_data.get('editing_tpl_field'):
        field = context.user_data.pop('editing_tpl_field')
        context.user_data.setdefault('new_tpl_fields', {})[field] = update.message.text.strip()
        source_id = context.user_data.get('new_tpl_source_id')
        await update.message.reply_text(
            "Поле обновлено. Выберите следующее поле или сохраните шаблон:",
            reply_markup=build_new_template_keyboard(context.user_data['new_tpl_fields'], source_id),
        )
        return


async def handle_new_template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "etpl_copy_from_id":
        context.user_data['awaiting_new_tpl_id'] = True
        await query.edit_message_text("Введите ID записи, из которой скопировать данные:")
    elif data.startswith("etpl_field_"):
        field = data.split("_", 2)[2]
        context.user_data['editing_tpl_field'] = field
        await query.edit_message_text(f"Введите значение для поля '{field}':")
    elif data == "etpl_save_new":
        fields = context.user_data.get('new_tpl_fields')
        if not fields:
            await query.edit_message_text("⚠️ Нет данных для сохранения")
            return
        context.user_data['save_edit_template_fields'] = fields
        context.user_data['awaiting_edit_template_name'] = True
        await query.edit_message_text("Введите название шаблона:")
    elif data == "etpl_cancel_new":
        context.user_data.pop('awaiting_new_tpl_id', None)
        context.user_data.pop('editing_tpl_field', None)
        context.user_data.pop('new_tpl_fields', None)
        context.user_data.pop('new_tpl_source_id', None)
        await query.edit_message_text("Создание шаблона отменено")


def register_edit_template_handlers(app):
    app.add_handler(CommandHandler("edit_templates", list_edit_templates, filters=ADMIN_FILTER))
    app.add_handler(CallbackQueryHandler(start_save_edit_template, pattern="^edit_save_template$"))
    app.add_handler(CallbackQueryHandler(create_edit_template_from_id, pattern="^etpl_create_from_id$"))
    app.add_handler(MessageHandler(filters.TEXT & ADMIN_FILTER, handle_new_template_text), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ADMIN_FILTER, save_edit_template_name), group=1)
    app.add_handler(CallbackQueryHandler(apply_edit_template, pattern=r"^etpl_apply_\d+$"))
    app.add_handler(CallbackQueryHandler(remove_edit_template, pattern=r"^etpl_del_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_new_template_callback, pattern=r"^etpl_(?:copy_from_id|field_.*|save_new|cancel_new)$"))