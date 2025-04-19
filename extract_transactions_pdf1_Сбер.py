import fitz  # PyMuPDF
import pandas as pd
import yaml
import os
import re

def load_pdf_config(config_path: str = 'pdf_patterns.yaml') -> dict:
    """Загружает конфигурацию из YAML файла"""
    if config_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, 'config', 'pdf_patterns.yaml')
    
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Файл конфигурации не найден: {config_path}")
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        raise ValueError(f"Ошибка при загрузке конфигурации: {e}")

def transform_dataframe(df):
    # Создаем новый DataFrame с нужными колонками
    new_df = pd.DataFrame(columns=['Дата и время операции', 'Сумма операции в валюте карты',
                                    'Описание операции', 'Номер карты'])
    
    i = 0
    n = len(df)
    
    while i < n:
        # Проверяем, является ли текущая строка датой (ДД.ММ.ГГГГ)
        if re.match(r'\d{2}\.\d{2}\.\d{4}', df.iloc[i]['text'].strip()):
            date = df.iloc[i]['text'].strip()
            i += 1
            
            # Проверяем, является ли следующая строка временем (ЧЧ:ММ)
            if i < n and re.match(r'\d{2}:\d{2}', df.iloc[i]['text'].strip()):
                time = df.iloc[i]['text'].strip()
                i += 1
                
                # Собираем данные для новой строки
                new_row = {
                    'Дата и время операции': f"{date} {time}",
                    'Сумма операции в валюте карты': df.iloc[i+2]['text'].strip() if i+2 < n else '',
                    'Описание операции': "Остаток: " + df.iloc[i+3]['text'].strip() + ", "
                                                    + df.iloc[i+1]['text'].strip() + ":" if i+1 < n else '',
                    'Номер карты': "Дата списания: " + df.iloc[i+4]['text'].strip() + ", "
                                                    + "код авторизации:" + df.iloc[i]['text'].strip()
                }
                
                # Добавляем новую строку в DataFrame
                new_df = pd.concat([new_df, pd.DataFrame([new_row])], ignore_index=True)
                i += 5  # Переходим к следующей дате
                
                # Обрабатываем дополнительные строки описания (9,10,11 и т.д.)
                while i < n and not re.match(r'\d{2}\.\d{2}\.\d{4}', df.iloc[i]['text'].strip()):
                    # Добавляем текст к описанию операции с разделителем ": "
                    if 'Описание операции' in new_df.columns:
                        new_df.at[new_df.index[-1], 'Описание операции'] += " " + df.iloc[i]['text'].strip()
                    i += 1
            else:
                i += 1
        else:
            i += 1
    
    return new_df

def extract_text_from_pdf(pdf_path, yaml_path):
    # Загрузка данных из YAML-файла
    pdf_config = load_pdf_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'pdf_patterns.yaml'))
    pdf_type = "Visa_Gold_Aeroflot"
    print(f"Определен тип PDF: {pdf_type}")

    # Получаем конфигурацию для конкретного типа PDF
    config = pdf_config['pdf_types'][pdf_type]
    
    patterns = config['patterns']
    columns = config['columns']
    start_marker = config['start_marker']
    end_marker = config['end_marker']
    remove_rows_by_text = config['remove_rows_by_text']

    # Открываем PDF файл
    document = fitz.open(pdf_path)
    text = ""

    # Извлекаем текст из всех страниц
    for page_num in range(len(document)):
        page = document.load_page(page_num)
        text += page.get_text()

    # Разделяем текст на строки
    lines = text.split('\n')

    # Создание DataFrame
    df = pd.DataFrame(lines, columns=['text'])

    # Создание временного столбца для пометки строк к удалению
    df['to_delete'] = False

    # Пометка строк, содержащих указанные тексты
    for text_pattern in remove_rows_by_text:
        df.loc[df['text'].str.contains(text_pattern, regex=False), 'to_delete'] = True

    # Поиск строки с текстом start_marker
    index_start_marker = df[df['text'].str.contains(start_marker, regex=False)].index

    # Пометка строк от начала документа до найденной строки включительно
    if not index_start_marker.empty:
        df.loc[:index_start_marker[0], 'to_delete'] = True

    # Поиск строки с текстом end_marker
    index_end_marker = df[df['text'].str.contains(end_marker, regex=False)].index

    # Пометка строк от найденной строки до конца документа
    if not index_end_marker.empty:
        df.loc[index_end_marker[0]:, 'to_delete'] = True

    # Удаление строк, помеченных к удалению
    df = df[~df['to_delete']]

    # Удаление временного столбца
    df = df.drop(columns=['to_delete'])

    # После получения очищенного DataFrame
    df = transform_dataframe(df)

    # Сохранение обработанного CSV-файла
    df.to_csv('transactions_Sber.csv', index=False)

    # print("Извлеченный текст:")
    # print(df)

# Пример использования
pdf_path = "/Users/IgorShvyrkin/Downloads/Выписка_по_счёту_кредитной_карты.pdf"
yaml_path = "/Users/IgorShvyrkin/Documents/EAT_TG_Bot_Docker/config/pdf_patterns.yaml"
extract_text_from_pdf(pdf_path, yaml_path)