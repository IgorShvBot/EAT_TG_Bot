import camelot
import pandas as pd
import os
import re
from pypdf import PdfReader
import yaml
import fitz  # PyMuPDF

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

def detect_pdf_type(pdf_path: str, pdf_config: dict) -> str:
    """Определяет тип PDF файла на основе конфигурации"""
    try:
        reader = PdfReader(pdf_path)
        first_page = reader.pages[0].extract_text()
        # print("Текст первой страницы:", first_page)
    except Exception as e:
        raise ValueError(f"Ошибка при чтении PDF: {e}")

    found_types = []

    for pdf_type, config in pdf_config['pdf_types'].items():
        for pattern in config['patterns']:
            try:
                if re.search(pattern, first_page, re.IGNORECASE):
                    print(f"Для типа {pdf_type} найдено совпадение с паттерном: {pattern}")
                    found_types.append(pdf_type)
                    break  # Достаточно одного совпадения для этого типа
            except re.error:
                print(f"Ошибка в регулярном выражении: {pattern}")

    if not found_types:
        raise ValueError("Не удалось определить тип PDF файла. Ни один паттерн не совпал.")
    
    if len(found_types) > 1:
        print(f"Найдено несколько возможных типов: {found_types}. Будет использован первый: {found_types[0]}")

    return found_types[0]

def process_Tinkoff_Platinum(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Обработка для Tinkoff Platinum
    
    Параметры:
        df: DataFrame с сырыми данными из PDF
        config: Словарь с конфигурацией обработки, содержащий:
            - 'columns': список индексов колонок для сохранения
            - 'start_marker': маркер начала данных
            - 'end_marker': маркер конца данных
    
    Возвращает:
        Обработанный DataFrame с транзакциями
    """
    # 1. Выбор нужных колонок из исходного DataFrame
    # Используем индексы колонок из конфига (например, [0, 3, 4, 5])
    df = df.iloc[:, config['columns']]
    
    # 2. Сброс названий колонок на числовые индексы (0, 1, 2...)
    # Это упрощает дальнейшую обработку
    df.columns = range(len(df.columns))
    
    # 3. Сброс индекса строк (начинаем с 0)
    df = df.reset_index(drop=True)

    # 4. Создание маски для фильтрации строк:
    # - Исключаем строки, содержащие start_marker или "операции"
    # - Исключаем дубликаты по первой колонке (кроме первого вхождения)
    mask = ~(
        # df.iloc[:, 0].str.contains(f"{config['start_marker']}|операции", na=False) &
        df.iloc[:, 0].str.contains(f"{config['start_marker']}", na=False) &
        df.duplicated(subset=df.columns[0], keep='first')
    )
    df = df[mask]

    # 5. Поиск начальной строки данных по start_marker
    start_rows = df[df.iloc[:, 0].str.contains(config['start_marker'], na=False)]
    if start_rows.empty:
        raise ValueError(f"Строка с текстом '{config['start_marker']}' не найдена.")
    start_index = start_rows.index[0]

    # 6. Поиск конечной строки данных по end_marker
    end_rows = df[df.iloc[:, 0].str.contains(config['end_marker'], na=False)]
    if end_rows.empty:
        raise ValueError(f"Строка с текстом '{config['end_marker']}' не найдена.")
    end_index = end_rows.index[0]

    # 7. Выбор только нужного диапазона строк (от start_index до end_index-1)
    df = df.loc[start_index:end_index-1]

    # 8. Обработка случая, когда заголовок таблицы разбит на две строки
    if len(df) > 1:
        # Объединяем первую и вторую строку через пробел
        combined_row = df.iloc[0] + " " + df.iloc[1]
        # Заменяем первую строку объединенной
        df.iloc[0] = combined_row
        # Сбрасываем индексы
        df = df.reset_index(drop=True)
        # Удаляем вторую строку (она теперь избыточна)
        df = df.drop(1)
    
    return df

def process_Visa_Gold_Aeroflot(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Обработка для Visa Gold Aeroflot
    
    Параметры:
        df: DataFrame с сырыми данными из PDF (должен содержать столбец 'text')
        config: Словарь с конфигурацией обработки из YAML
    
    Возвращает:
        Обработанный DataFrame с транзакциями
    """
    # Проверка и подготовка конфигурации
    required_keys = ['remove_rows_by_text', 'start_marker', 'end_marker']
    for key in required_keys:
        if key not in config:
            raise ValueError(f"В конфигурации отсутствует обязательный ключ: {key}")

    remove_texts = config['remove_rows_by_text']
    start_marker = config['start_marker']
    end_marker = config['end_marker']

    # Создаем временный столбец для пометки строк к удалению
    df['to_delete'] = False

    # 1. Удаление строк по текстовым паттернам из конфига
    for pattern in remove_texts:
        df.loc[df['text'].str.contains(pattern, regex=False, na=False), 'to_delete'] = True

    # 2. Удаление строк до start_marker (включительно)
    start_mask = df['text'].str.contains(start_marker, regex=False, na=False)
    if start_mask.any():
        start_idx = start_mask.idxmax()
        df.loc[:start_idx, 'to_delete'] = True

    # 3. Удаление строк после end_marker (включительно)
    end_mask = df['text'].str.contains(end_marker, regex=False, na=False)
    if end_mask.any():
        end_idx = end_mask.idxmax()
        df.loc[end_idx:, 'to_delete'] = True

    # 4. Применение удаления и очистка
    df = df[~df['to_delete']].copy()
    df = df.drop(columns=['to_delete'])
    df = df.reset_index(drop=True)

    return df

def process_Tinkoff(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Обработка для Tinkoff"""
    df = df.iloc[:, config.get('columns', slice(None))]
    df.columns = range(len(df.columns))
    df = df.dropna(how='all')
    return df

def process_default(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Обработка по умолчанию"""
    df = df.iloc[:, config.get('columns', slice(None))]
    df.columns = range(len(df.columns))
    df = df.dropna(how='all')
    return df

def remove_rows_by_position(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """
    Удаляет строки по позициям (первые/последние N строк или по конкретным индексам)
    
    Параметры:
        df: DataFrame для обработки
        config: Словарь с конфигурацией, может содержать:
            - 'first': количество первых строк для удаления
            - 'last': количество последних строк для удаления
            - 'indices': список конкретных индексов для удаления
    
    Возвращает:
        Обработанный DataFrame
    """
    if 'remove_rows_by_position' not in config:
        return df
    
    position_config = config['remove_rows_by_position']
    n_rows = len(df)
    
    # Удаление первых N строк
    if 'first' in position_config:
        first_n = min(position_config['first'], n_rows)
        print(f"Удаление первых {first_n} строк")
        df = df.iloc[first_n:]
    
    # Удаление последних N строк
    if 'last' in position_config:
        last_n = min(position_config['last'], n_rows)
        print(f"Удаление последних {last_n} строк")
        df = df.iloc[:-last_n] if last_n > 0 else df
    
    # Удаление по конкретным индексам
    if 'indices' in position_config:
        print(f"Удаление строк с индексами: {position_config['indices']}")
        df = df.drop(position_config['indices'], errors='ignore')
    
    return df

# Словарь обработчиков для разных типов PDF
PDF_PROCESSORS = {
    'Tinkoff_Platinum': process_Tinkoff_Platinum,
    'Visa_Gold_Aeroflot': process_Visa_Gold_Aeroflot,
    'Tinkoff': process_Tinkoff_Platinum, # process_Tinkoff
    "default": process_default
}

def sub_process_pdf_Sber(pdf_path: str) -> pd.DataFrame:
    # Открываем PDF файл
    document = fitz.open(pdf_path)
    text = ""

    # Извлекаем текст из всех страниц
    for page_num in range(len(document)):
        page = document.load_page(page_num)
        text += page.get_text()
    return pd.DataFrame(text.split('\n'), columns=['text'])
    
    # Разделяем текст на строки
    # lines = text.split('\n')

    # Создание DataFrame
    # df = pd.DataFrame(lines, columns=['text'])

    # Создание временного столбца для пометки строк к удалению
    df['to_delete'] = False

def sub_process_pdf_Not_Sber(pdf_path: str) -> pd.DataFrame:
    # Чтение PDF
    tables = camelot.read_pdf(
        pdf_path,
        flavor="stream",
        pages="all",
        strip_text=None, # "\n",
        edge_tol=100,
    )

    if not tables:
        raise ValueError("Не удалось извлечь таблицы из PDF")
    return pd.concat([table.df for table in tables])

def process_pdf(pdf_path: str) -> str:
    """Обрабатывает PDF файл и возвращает путь к временному CSV"""
    pdf_config = load_pdf_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'pdf_patterns.yaml'))
    pdf_type = detect_pdf_type(pdf_path, pdf_config)
    print(f"Определен тип PDF: {pdf_type}")
    
    config = pdf_config['pdf_types'][pdf_type]

    # Выбираем подпроцесс в зависимости от типа PDF
    if pdf_type == "Visa_Gold_Aeroflot":
        df = sub_process_pdf_Sber(pdf_path)
    else:
        df = sub_process_pdf_Not_Sber(pdf_path)

    # Выбираем обработчик
    processor = PDF_PROCESSORS.get(pdf_type, process_default)
    df = processor(df, config)
    
    # Сохранение во временный файл
    output_dir = os.path.dirname(pdf_path)
    os.makedirs(output_dir, exist_ok=True)
        
    temp_csv_path = os.path.join(output_dir, f"transactions_{pdf_type}_temp.csv")
    df.to_csv(temp_csv_path, index=False)

    return temp_csv_path, pdf_type

if __name__ == "__main__":
    pdf_path = "/Users/IgorShvyrkin/Downloads/Выписка_по_счёту_кредитной_карты.pdf"
    # pdf_path = '/Users/IgorShvyrkin/Downloads/Справка_о_движении_денежных_средств (Д).pdf'
    try:
        csv_path = process_pdf(pdf_path)
        print(f"CSV файл сохранен по пути: {csv_path}")
    except Exception as e:
        print(f"Ошибка при обработке PDF: {str(e)}")