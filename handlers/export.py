import os
import logging
import pandas as pd
from datetime import datetime
from tempfile import NamedTemporaryFile

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)

from db.base import DBConnection
from db.transactions import get_transactions

logger = logging.getLogger(__name__)
EXPORT_FILTER_KEYS = ["category", "transaction_type", "cash_source", "transaction_class", "pdf_type", "import_id"]


def build_filters_keyboard(filters: dict) -> InlineKeyboardMarkup:
    """
    Генерирует InlineKeyboard с текущими значениями фильтров.
    """
    buttons = []

    mapping = {
        "category": "Категория",
        "transaction_type": "Тип",
        "cash_source": "Наличность",
        "transaction_class": "Класс",
        "pdf_type": "Тип PDF",
        "import_id": "ID импорта"
    }

    for key in EXPORT_FILTER_KEYS:
        value = filters.get(key)
        title = mapping.get(key, key)
        display = value if value not in (None, "", "Все") else "Все"
        buttons.append([
            InlineKeyboardButton(f"{title}: {display}", callback_data=f"set_{key}")
        ])

    buttons.append([
        InlineKeyboardButton("📤 Сформировать отчёт", callback_data="generate_report")
    ])
    buttons.append([
        InlineKeyboardButton("↩️ Назад", callback_data="back_to_main")
    ])

    return InlineKeyboardMarkup(buttons)


async def show_filters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_mode: bool = False):
    """
    Показывает меню фильтров для экспорта или редактирования.
    """
    filters = context.user_data.setdefault("export_filters", {})

    if edit_mode:
        text = "⚙️ Настройте параметры редактирования:"
    else:
        text = "⚙️ Настройте параметры отчета:"

    reply_markup = build_filters_keyboard(filters)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def handle_export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /export — запускает настройку фильтров.
    """
    context.user_data["edit_mode"] = False  # Явно выключаем режим редактирования
    context.user_data.setdefault("export_filters", {})  # Готовим фильтры
    await show_filters_menu(update, context)


async def generate_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Формирует CSV-отчет на основе установленных фильтров.
    """
    query = update.callback_query
    await query.answer("Формирую отчет...")

    filters = context.user_data.get("export_filters")
    if not filters:
        await query.edit_message_text("❌ Ошибка: фильтры экспорта не найдены.")
        return

    try:
        # Преобразуем даты
        filters['start_date'] = datetime.strptime(filters['start_date'], "%d.%m.%Y")
        filters['end_date'] = datetime.strptime(filters['end_date'], "%d.%m.%Y")

        # Отфильтровываем значения
        db_filters = {
            k: v for k, v in filters.items()
            if v not in ("Все", None, "") or k in ["description", "counterparty", "check_num"]
        }

        with DBConnection() as db:
            df = get_transactions(
                user_id=query.from_user.id,
                start_date=filters['start_date'],
                end_date=filters['end_date'],
                filters=db_filters,
                db=db
            )

        if df.empty:
            await query.edit_message_text("⚠ По вашему запросу ничего не найдено.")
            return

        df = df.fillna('').replace('NaN', '').astype(str)
        df['transaction_date'] = pd.to_datetime(df['transaction_date']).dt.strftime('%d.%m.%Y %H:%M')

        column_mapping = {
            'id': 'ID', 'transaction_date': 'Дата', 'amount': 'Сумма',
            'cash_source': 'Наличность', 'target_amount': 'Сумма (куда)',
            'target_cash_source': 'Наличность (куда)', 'category': 'Категория',
            'description': 'Описание', 'transaction_type': 'Тип транзакции',
            'counterparty': 'Контрагент', 'check_num': 'Чек #', 'transaction_class': 'Класс'
        }
        df.rename(columns=column_mapping, inplace=True)

        with NamedTemporaryFile(suffix='.csv', delete=False, mode='w', encoding='utf-8') as tmp:
            df.to_csv(tmp.name, index=False, sep=',')
            tmp_path = tmp.name

        applied_filters = format_filters(filters)

        await context.bot.send_document(
            chat_id=query.from_user.id,
            document=open(tmp_path, 'rb'),
            filename='report.csv',
            caption=f"Отчет за {filters['start_date'].strftime('%d.%m.%Y')} – {filters['end_date'].strftime('%d.%m.%Y')}\n"
                    f"📌 Записей: {len(df)}"
        )

        os.unlink(tmp_path)

        await query.edit_message_text(
            f"✅ Отчет успешно сформирован\n\n"
            f"⚙️ Примененные фильтры:\n{applied_filters}"
        )

    except Exception as e:
        logger.error("Ошибка генерации отчета: %s", e, exc_info=True)
        await query.edit_message_text("❌ Ошибка при формировании отчета. Попробуйте позже")


def format_filters(filters: dict) -> str:
    """
    Форматирует словарь фильтров в текст для отображения пользователю.
    """
    lines = []
    if 'start_date' in filters and 'end_date' in filters:
        lines.append(f"📅 Период: {filters['start_date'].strftime('%d.%m.%Y')} – {filters['end_date'].strftime('%d.%m.%Y')}")
    for key, label in [
        ('cash_source', '💳 Наличность'),
        ('target_cash_source', '💸 Куда'),
        ('category', '📂 Категория'),
        ('transaction_type', '🔄 Тип'),
        ('transaction_class', '🏷 Класс'),
        ('description', '📝 Описание'),
        ('counterparty', '👤 Контрагент'),
        ('check_num', '🔢 Чек #'),
        ('pdf_type', '📎 Тип PDF'),
        ('import_id', '🆔 ID импорта')
    ]:
        if filters.get(key) not in [None, '', 'Все']:
            lines.append(f"{label}: {filters[key]}")
    return "\n".join(lines)


def register_export_handlers(application):
    """
    Регистрирует все хендлеры, связанные с экспортом отчёта.
    """
    application.add_handler(CommandHandler("export", handle_export_command))
    application.add_handler(CallbackQueryHandler(generate_report, pattern="^generate_report$"))
    application.add_handler(CallbackQueryHandler(show_filters_menu, pattern="^apply_export_filters$"))