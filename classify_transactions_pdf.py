import pandas as pd
import re
import yaml
import os
from typing import Dict, List, Any
import logging
import csv

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%d-%m-%Y %H:%M:%S %z',
    # level=logging.DEBUG
    level=logging.INFO
)

logger = logging.getLogger(__name__)

def load_config(config_path: str = None) -> Dict[str, Any]:
    """Загружает конфигурацию из YAML-файла"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'categories.yaml')
    with open(config_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)

def load_class_contractor_config(config_path: str = None) -> Dict[str, Any]:
    """Загружает конфигурацию class_contractor из YAML-файла"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'class_contractor.yaml')
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

def apply_special_conditions(row: pd.Series, conditions: List[Dict[str, Any]], result: pd.DataFrame, user_settings: dict = None) -> None:
    description = row['Описание операции'].upper()
    
    # Инициализация user_settings, если не переданы
    user_settings = user_settings or {}

    for condition in conditions:
        if re.search(condition['condition'], description, re.IGNORECASE):
            for action in condition['actions']:
                field = action['field']
                value = action['value']

                if value == "$SELF":
                    value = result.at[row.name, 'Сумма']
                elif value == "-$CURRENT":
                    current_value = result.at[row.name, 'Сумма']
                    clean_value = current_value.replace(' ', '').replace('₽', '').replace('P', '')
                    value = f"-{clean_value} ₽"
                elif value == "$CURRENT":
                    current_value = result.at[row.name, field]
                    comment = action.get('comment', '')
                    value = f"{current_value}, {comment}" if comment else current_value

                # Применение пользовательских настроек только для поля 'Чек #'
                if field == 'Чек #' and 'Чек #' in user_settings:
                    if user_settings['Чек #']['operator'] == '+':
                        value = f"{value} {user_settings['Чек #']['value']}"
                    else:
                        value = user_settings['Чек #']['value']

                result.at[row.name, field] = value

def add_pattern_to_category(category_name: str, pattern: str, config_path: str = None) -> None:
    """Добавляет новый паттерн в указанную категорию конфига"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'categories.yaml')
    
    try:
        # Загружаем текущий конфиг
        with open(config_path, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file) or {'categories': []}
        
        # Ищем категорию
        category_found = False
        for category in config.get('categories', []):
            if category['name'] == category_name:
                if pattern not in category['patterns']:
                    category['patterns'].append(pattern)
                category_found = True
                break
        
        # Если категория не найдена, создаем новую
        if not category_found:
            config['categories'].append({'name': category_name, 'patterns': [pattern]})
        
        # Сохраняем изменения
        with open(config_path, 'w', encoding='utf-8') as file:
            yaml.dump(config, file, allow_unicode=True, default_flow_style=False, sort_keys=False)
        
        logger.info(f"Паттерн '{pattern}' успешно добавлен в категорию '{category_name}'")
    except Exception as e:
        logger.error(f"Ошибка при добавлении паттерна: {str(e)}")
        raise

def classify_transactions(input_csv_path: str, pdf_type: str = 'default', user_settings: dict = None) -> str:
    """Классифицирует транзакции и возвращает путь к итоговому CSV"""
    try:
        if user_settings is None:
            user_settings = {}
        # df = pd.read_csv(input_csv_path)
        df = pd.read_csv(input_csv_path, sep=',', quotechar='"', engine='python')
        
        # Проверка обязательных столбцов
        required_columns = ['Дата и время операции', 'Сумма операции в валюте карты']
        missing_columns = [col for col in required_columns if col not in df.columns]
        
        if missing_columns:
            raise ValueError(f"Отсутствуют обязательные столбцы: {', '.join(missing_columns)}")  
        
        # Загрузка конфигураций
        categories_config = load_config()
        type_settings = load_class_contractor_config().get('type_settings', {})
        settings = type_settings.get(pdf_type, type_settings.get('default', {}))
        special_conditions = load_config(os.path.join(os.path.dirname(__file__), 'config', 'special_conditions.yaml'))['special_conditions']
        # Чтение исходных данных
        df = pd.read_csv(input_csv_path, sep=',', encoding='utf-8-sig')
        # Создание результирующего DataFrame
        result = pd.DataFrame()
        # 1. Дата
        result['Дата'] = df['Дата и время операции']
        # 2. Сумма
        result['Сумма'] = df['Сумма операции в валюте карты'].str.replace(r'[₽+-]|\s', '', regex=True).str.strip()
        # 3. Наличность
        result['Наличность'] = user_settings.get('Наличность', {}).get('value', settings.get('cash', ''))
        # 4. Сумма (куда) и Наличность (куда)
        result['Сумма (куда)'] = ''
        result['Наличность (куда)'] = ''
        # 5. Категория
        result['Категория'] = df['Описание операции'].apply(
            lambda x: classify_transaction(x, categories_config['categories'])
        )
        # 6. Описание
        if 'Описание' in user_settings:
            setting = user_settings['Описание']
            if setting['operator'] == '+':
                result['Описание'] = df['Описание операции'] + ', ' + setting['value']
            else:
                result['Описание'] = setting['value']
        else:
            result['Описание'] = df['Описание операции']

        # 7. Тип транзакции (по умолчанию "Расход")
        result['Тип транзакции'] = 'Расход'
        # 8. Контрагент
        result['Контрагент'] = user_settings.get('Контрагент', {}).get('value', settings.get('contractor', ''))
        # result.loc[df['Номер карты'] == "2578", 'Контрагент'] = '! Наташа'
        result.loc[result['Категория'] == 'Ком. платежи. Вернадского 54', 'Контрагент'] = 'Квартира_Ипотека'
        # 9. Чек #
        result['Чек #'] = df['Номер карты'].astype(str)
        result.loc[df['Номер карты'] == "2578", 'Контрагент'] = '! Наташа'
        # 10. Класс
        result['Класс'] = user_settings.get('Класс', {}).get('value', settings.get('class', '01 Личное'))
        # Применение специальных условий
        for _, row in df.iterrows():
            apply_special_conditions(row, special_conditions, result, user_settings)

        # Применение пользовательских настроек (дополнительно)
        if user_settings:
            for setting_key, setting_value in user_settings.items():
                # Нормализуем название поля
                normalized_key = setting_key.lower().replace(' ', '').replace('#', '').replace('№', '')
                
                # Определяем соответствующее название колонки в DataFrame
                if normalized_key in ['контрагент', 'контрагента']:
                    column_name = 'Контрагент'
                elif normalized_key in ['чек', 'чек#', 'чек№']:
                    column_name = 'Чек #'
                elif normalized_key in ['описание', 'описании']:
                    column_name = 'Описание'
                elif normalized_key in ['наличность', 'нал', 'наличка']:
                    column_name = 'Наличность'
                elif normalized_key in ['класс']:
                    column_name = 'Класс'
                else:
                    continue  # Пропускаем неизвестные настройки
                    
                # Применяем настройку, если колонка существует
                if column_name in result.columns:
                    if setting_value['operator'] == '+':
                        result[column_name] = result[column_name].astype(str) + ', ' + setting_value['value']
                    else:
                        result[column_name] = setting_value['value']

        # Сохранение результата
        output_csv_path = os.path.join(os.path.dirname(input_csv_path), "result.csv")
        # result.to_csv(output_csv_path, sep=';', index=False, encoding='utf-8')
        result.to_csv(output_csv_path, sep=';', index=False, encoding='utf-8', quoting=csv.QUOTE_ALL)

        # Формирование файла с неподходящими транзакциями
        unclassified_csv_path = None
        unclassified_df = result[result['Категория'] == 'Другое']
        if not unclassified_df.empty:
            unclassified_csv_path = os.path.join(os.path.dirname(input_csv_path), "unclassified.csv")
            # Сохраняем исходные данные для удобства пользователя
            # unclassified_df = df[df.index.isin(unclassified_df.index)]
            unclassified_df.to_csv(unclassified_csv_path, sep=';', index=False, encoding='utf-8')

    except Exception as e:
        logger.error(f"Ошибка классификации транзакций: {str(e)}")
        raise

    return output_csv_path, unclassified_csv_path