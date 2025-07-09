# handlers/edit.py

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from db.base import DBConnection
from db.transactions import (
    check_existing_ids,
    get_transactions,
    update_transactions,
    get_transaction_fields,
)
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def build_edit_keyboard(
    updates: dict | None = None,
    add_confirm: bool = False,
    copied_from_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру выбора полей с учётом введённых значений."""

    def button_text(label: str, field: str) -> str:
        if field == "copy_from_id" and copied_from_id is not None:
            return f"{label}: {copied_from_id}"
        if updates is not None:
            if field in updates:
                value = updates[field][0]
                short = value if len(value) <= 20 else value[:17] + "..."
                return f"{label}: {short}"
            else:
                return f"{label}: без изменений"
            
        return label

    keyboard = [
        [InlineKeyboardButton(button_text("📋 Из ID", "copy_from_id"), callback_data='edit_copy_from_id')],
        [InlineKeyboardButton("📑 Шаблоны", callback_data='edit_show_templates')],
        [InlineKeyboardButton(button_text("🏷 Категория", "category"), callback_data='edit_field_category')],
        [InlineKeyboardButton(button_text("📝 Описание", "description"), callback_data='edit_field_description')],
        [InlineKeyboardButton(button_text("👥 Контрагент", "counterparty"), callback_data='edit_field_counterparty')],
        [InlineKeyboardButton(button_text("🧾 Чек #", "check_num"), callback_data='edit_field_check_num')],
        [InlineKeyboardButton(button_text("💳 Наличность", "cash_source"), callback_data='edit_field_cash_source')],
        [InlineKeyboardButton(button_text("💸 Наличность (куда)", "target_cash_source"), callback_data='edit_field_target_cash_source')],
        [InlineKeyboardButton(button_text("🔀 Тип", "transaction_type"), callback_data='edit_field_transaction_type')],
        [InlineKeyboardButton(button_text("📊 Класс", "transaction_class"), callback_data='edit_field_transaction_class')],
        [InlineKeyboardButton(button_text("📄 Тип PDF", "pdf_type"), callback_data='edit_field_pdf_type')],
    ]

    if add_confirm:
        keyboard.append([InlineKeyboardButton("✅ Подтвердить", callback_data='confirm_edits')])

    keyboard.extend([
        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_edit_choice')],
        [InlineKeyboardButton("✖️ Отмена", callback_data='cancel_edit')]
    ])

    return InlineKeyboardMarkup(keyboard)


def parse_ids_input(text: str) -> list[int]:
    """
    Разбирает текстовый ввод в список ID.
    """
    if '-' in text:
        start, end = map(int, text.split('-'))
        return list(range(start, end + 1))
    return [int(id_str.strip()) for id_str in text.split(',')]


def get_valid_ids(text: str) -> list[int]:
    """
    Проверяет существование ID в БД, возвращает только существующие.
    """
    parsed_ids = parse_ids_input(text)
    with DBConnection() as db:
        existing = check_existing_ids(parsed_ids, db=db)
    if not existing:
        raise ValueError("❌ Ни один из указанных ID не найден в базе")
    return existing


async def apply_edits(context: ContextTypes.DEFAULT_TYPE, user_id: int, edit_mode: dict, new_value: str) -> tuple[int, str]:
    """
    Применяет изменения к базе и возвращает количество обновленных записей.
    """
    if edit_mode['type'] == 'edit_by_filter':
        ids = edit_mode.get('ids', [])
        if not ids:
            filters = edit_mode.get('edit_filters')
            if not filters:
                raise ValueError("⚠ Фильтры для редактирования не найдены.")

            with DBConnection() as db:
                df = get_transactions(
                    user_id=user_id,
                    start_date=datetime.strptime(filters['start_date'], '%d.%m.%Y'),
                    end_date=datetime.strptime(filters['end_date'], '%d.%m.%Y'),
                    filters={k: v for k, v in filters.items() if v != 'Все'},
                    db=db
                )
            ids = df['id'].tolist()
            if not ids:
                raise ValueError("⚠ По фильтрам не найдено записей.")

    else:
        ids = edit_mode.get('ids', [])

    updates = {
        edit_mode['field']: (new_value, edit_mode['mode'])
    }

    with DBConnection() as db:
        updated_ids = update_transactions(
            user_id=user_id,
            ids=ids,
            updates=updates,
            db=db
        )

    logger.info(f"Пользователь {user_id} обновил {len(updated_ids)} записей: {updated_ids}. Поле: {edit_mode['field']}")
    return len(updated_ids), edit_mode['field']
