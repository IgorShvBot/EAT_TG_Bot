from datetime import datetime
import calendar
from telegram import InlineKeyboardButton


def get_default_filters() -> dict:
    """
    Возвращает словарь фильтров с предустановленными значениями по умолчанию
    """
    today = datetime.now()
    start_of_month = today.replace(day=1)
    last_day = calendar.monthrange(today.year, today.month)[1]
    end_of_month = today.replace(day=last_day)

    return {
        'start_date': start_of_month.strftime('%d.%m.%Y'),
        'end_date': end_of_month.strftime('%d.%m.%Y'),
        'category': 'Все',
        'transaction_type': 'Все',
        'cash_source': 'Все',
        'counterparty': 'Все',
        'check_num': 'Все',
        'transaction_class': 'Все',
        'description': 'Все',
        'pdf_type': 'Все',
        'import_id': 'Все'
    }


def get_keyboard_for_filters(filters: dict) -> list:
    """
    Формирует клавиатуру на основе текущих значений фильтров.
    
    :param filters: словарь с фильтрами
    :return: список списков кнопок InlineKeyboardButton
    """
    keyboard = []
    for key, value in filters.items():
        if key in ['start_date', 'end_date']:
            continue  # Даты обрабатываются отдельно
        label = f"{key}: {value}"
        button = InlineKeyboardButton(label, callback_data=f"filter_{key}")
        keyboard.append([button])
    return keyboard
