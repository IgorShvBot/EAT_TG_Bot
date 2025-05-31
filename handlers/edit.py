# handlers/edit.py

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from db.base import DBConnection
from db.transactions import check_existing_ids, get_transactions, update_transactions
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def build_edit_keyboard() -> InlineKeyboardMarkup:
    """
    Возвращает клавиатуру для выбора редактируемого поля.
    """
    keyboard = [
        [InlineKeyboardButton("🏷 Категория", callback_data='edit_field_category')],
        [InlineKeyboardButton("📝 Описание", callback_data='edit_field_description')],
        [InlineKeyboardButton("👥 Контрагент", callback_data='edit_field_counterparty')],
        [InlineKeyboardButton("🧾 Чек #", callback_data='edit_field_check_num')],
        [InlineKeyboardButton("💳 Наличность", callback_data='edit_field_cash_source')],
        [InlineKeyboardButton("📄 Тип PDF", callback_data='edit_field_pdf_type')],
        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_edit_choice')],
        [InlineKeyboardButton("✖️ Отмена", callback_data='cancel_edit')]
    ]
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
