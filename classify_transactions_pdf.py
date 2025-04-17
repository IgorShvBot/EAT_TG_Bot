import pandas as pd
import re
import yaml
import os
from typing import Dict, List, Any

def load_config(config_path: str = None) -> Dict[str, Any]:
    """Загружает конфигурацию из YAML-файла"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'categories.yaml')
    with open(config_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)

def classify_transaction(description: str, categories: List[Dict[str, Any]]) -> str:
    """Классификация операции по описанию"""
    description = description.upper()

    for category in categories:
        for pattern in category['patterns']:
            if re.search(pattern, description, re.IGNORECASE):
                return category['name']
    return 'Другое'

def apply_special_conditions(row: pd.Series, conditions: List[Dict[str, Any]], result: pd.DataFrame) -> None:
    description = row['Описание операции'].upper()

    for condition in conditions:
        if re.search(condition['condition'], description, re.IGNORECASE):
            for action in condition['actions']:
                field = action['field']
                value = action['value']

                if value == "$SELF":
                    value = result.at[row.name, 'Сумма']
                elif value == "-$CURRENT":
                    # Получаем текущее значение суммы и добавляем минус
                    current_value = result.at[row.name, 'Сумма']
                    # Удаляем возможные пробелы и символы валюты
                    clean_value = current_value.replace(' ', '').replace('₽', '').replace('P', '')
                    value = f"-{clean_value} ₽"  # Добавляем минус и возвращаем валюту
                elif value == "$CURRENT":
                    current_value = result.at[row.name, field]
                    comment = action.get('comment', '')
                    value = f"{current_value}, {comment}" if comment else current_value

                result.at[row.name, field] = value

def classify_transactions(input_csv_path: str) -> str:
    """Классифицирует транзакции и возвращает путь к итоговому CSV"""
    # Загрузка конфигураций
    categories_config = load_config()
    special_conditions = load_config(os.path.join(os.path.dirname(__file__), 'config', 'special_conditions.yaml'))['special_conditions']
    # Чтение исходных данных
    df = pd.read_csv(input_csv_path, sep=',', encoding='utf-8-sig')
    # Создание результирующего DataFrame
    result = pd.DataFrame()
    # 1. Дата
    result['Дата'] = df['Дата и время операции']
    # 2. Сумма
    result['Сумма'] = df['Сумма операции в валюте карты'].str.replace(' ₽', '', regex=True).str.replace('-', '', regex=True).str.replace('+', '', regex=True).str.strip()
    # 3. Наличность
    result['Наличность'] = 'Тинькофф. Платинум'
    # 4. Сумма (куда) и Наличность (куда)
    result['Сумма (куда)'] = ''
    result['Наличность (куда)'] = ''
    # 5. Категория
    result['Категория'] = df['Описание операции'].apply(
        lambda x: classify_transaction(x, categories_config['categories'])
    )
    # 6. Описание
    result['Описание'] = df['Описание операции']
    # 7. Тип транзакции (по умолчанию "Расход")
    result['Тип транзакции'] = 'Расход'
    # 8. Контрагент
    result['Контрагент'] = ''
    result.loc[df['Номер карты'] == 2578, 'Контрагент'] = '! Наташа'
    result.loc[result['Категория'] == 'Ком. платежи. Вернадского 54', 'Контрагент'] = 'Квартира_Ипотека'
    # 9. Чек #
    result['Чек #'] = df['Номер карты'].astype(str)
    # 10. Класс
    result['Класс'] = '01 Личное'
    # Применение специальных условий
    for _, row in df.iterrows():
        apply_special_conditions(row, special_conditions, result)
    # Сохранение результата
    output_csv_path = os.path.join(os.path.dirname(input_csv_path), "result.csv")
    result.to_csv(output_csv_path, sep=';', index=False, encoding='utf-8')
    return output_csv_path