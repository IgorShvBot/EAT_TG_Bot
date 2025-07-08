"""Автотест на целостность фильтров"""
"""Запуск: pytest tests/test_filters.py"""

import pytest
from handlers.filters import get_default_filters

REQUIRED_EXPORT_KEYS = [
    'start_date', 'end_date',
    'category', 'transaction_type', 'cash_source',
    'counterparty', 'check_num', 'transaction_class',
    'description', 'pdf_type', 'import_id',
    'id'
]

def test_export_filters_have_all_required_keys():
    filters = get_default_filters()
    missing_keys = [key for key in REQUIRED_EXPORT_KEYS if key not in filters]
    assert not missing_keys, f"Отсутствуют ключи в export_filters: {missing_keys}"
