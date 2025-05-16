import pandas as pd
import re
import os
import logging
from typing import List, Optional, Dict
import sys

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def process_tinkoff_platinum(input_csv_path: str) -> pd.DataFrame:
    """Обрабатывает CSV для Tinkoff Platinum"""
    try:
        # df = pd.read_csv(input_csv_path, header=None, skiprows=1)
        df = pd.read_csv(input_csv_path, sep=',', quotechar='"', engine='python')
        logger.debug(f"Успешно загружен CSV для Tinkoff Platinum, строк: {len(df)}")
    except Exception as e:
        logger.error(f"Ошибка чтения CSV для Tinkoff Platinum: {str(e)}")
        raise

    result = []
    i = 0
    
    while i < len(df):
        current_row = df.iloc[i]

        if isinstance(current_row[0], str) and re.match(r"\d{2}\.\d{2}\.\d{4}", current_row[0]):
            try:
                date = current_row[0]
                amount = current_row[1]
                initial_description = str(current_row[2]) if pd.notna(current_row[2]) else ""
                card_number = current_row[3]

                time_row = df.iloc[i+1] if i+1 < len(df) else None
                time = time_row[0] if time_row is not None and pd.notna(time_row[0]) else ""

                description_parts = [initial_description]
                if time_row is not None and pd.notna(time_row[2]):
                    description_parts.append(str(time_row[2]))

                j = i + 2
                while j < len(df) and (pd.isna(df.iloc[j][0]) or df.iloc[j][0] == ''):
                    if pd.notna(df.iloc[j][2]):
                        description_parts.append(str(df.iloc[j][2]))
                    j += 1

                full_description = ' '.join(filter(None, description_parts)).strip()
                datetime = f"{date} {time}" if time else date

                result.append([
                    datetime,
                    amount,
                    full_description,
                    card_number
                ])

                i = j
            except Exception as e:
                logger.error(f"Ошибка обработки строки {i}: {str(e)}")
                i += 1
        else:
            i += 1

    return pd.DataFrame(result, columns=[
        "Дата и время операции",
        "Сумма операции в валюте карты",
        "Описание операции",
        "Номер карты"
    ])

def process_visa_gold_aeroflot(input_csv_path: str) -> pd.DataFrame:
    """Обрабатывает CSV для Visa Gold Aeroflot"""
    try:
        # df = pd.read_csv(input_csv_path)
        df = pd.read_csv(input_csv_path, sep=',', quotechar='"', engine='python')
        logger.info(f"Успешно загружен CSV для Visa Gold Aeroflot, строк: {len(df)}")
    except Exception as e:
        logger.error(f"Ошибка чтения CSV для Visa Gold Aeroflot: {str(e)}")
        raise

    new_df = pd.DataFrame(columns=[
        'Дата и время операции', 
        'Сумма операции в валюте карты',
        'Описание операции', 
        'Номер карты'
    ])

    i = 0
    n = len(df)
    
    while i < n:
        try:
            if re.match(r'\d{2}\.\d{2}\.\d{4}', df.iloc[i]['text'].strip()):
                date = df.iloc[i]['text'].strip()
                i += 1
                
                if i < n and re.match(r'\d{2}:\d{2}', df.iloc[i]['text'].strip()):
                    time = df.iloc[i]['text'].strip()
                    i += 1
                    
                    new_row = {
                        'Дата и время операции': f"{date} {time}",
                        # 'Сумма операции в валюте карты': df.iloc[i+2]['text'].strip() if i+2 < n else '',
                        'Сумма операции в валюте карты': df.iloc[i+1]['text'].strip() if i+2 < n else '',
                        'Описание операции': " ".join(filter(None, [
                            df.iloc[i]['text'].strip() + ":" if i < n else ''
                        ])),
                        'Номер карты': "".join(filter(None, [
                            "Остаток: " + df.iloc[i+2]['text'].strip() if i+2 < n else '',
                            " ₽, дата списания: " + df.iloc[i+3]['text'].strip() if i+3 < n else '',
                            ", код авторизации: " + df.iloc[i+4]['text'].strip() if i+4 < n else ''
                        ]))
                    }
                    
                    new_df = pd.concat([new_df, pd.DataFrame([new_row])], ignore_index=True)
                    i += 5
                    
                    while i < n and not re.match(r'\d{2}\.\d{2}\.\d{4}', df.iloc[i]['text'].strip()):
                        new_df.at[new_df.index[-1], 'Описание операции'] += " " + df.iloc[i]['text'].strip()
                        i += 1
                else:
                    i += 1
            else:
                i += 1
        except Exception as e:
            logger.error(f"Ошибка обработки строки {i}: {str(e)}")
            i += 1

    return new_df

def process_Yandex(input_csv_path: str) -> pd.DataFrame:
    """Обрабатывает CSV для Yandex"""
    try:
        # df = pd.read_csv(input_csv_path)
        df = pd.read_csv(input_csv_path, sep=',', quotechar='"', engine='python')
        logger.info(f"Успешно загружен CSV для Yandex, строк: {len(df)}")
    except Exception as e:
        logger.error(f"Ошибка чтения CSV для Yandex: {str(e)}")
        raise

    new_df = pd.DataFrame(columns=[
        'Дата и время операции', 
        'Сумма операции в валюте карты',
        'Описание операции', 
        'Номер карты'
    ])

    i = 0
    n = len(df)
    
    while i < n:
        try:
            # Сбор описания операции (может занимать несколько строк перед датой)
            description_parts = []
            while i < n and not re.match(r'\d{2}\.\d{2}\.\d{4}', df.iloc[i]['text'].strip()):
                description_parts.append(df.iloc[i]['text'].strip())
                i += 1

            if i >= n:
                break

            # Обработка даты и времени
            date = df.iloc[i]['text'].strip()
            i += 1
            
            if i < n and re.match(r'в \d{2}:\d{2}', df.iloc[i]['text'].strip()):
                time = re.match(r'в (\d{2}:\d{2})', df.iloc[i]['text'].strip()).group(1)
                i += 1
            else:
                time = "00:00"

            # Пропуск даты обработки и суммы в валюте операции
            i += 2  # дата обработки и сумма в валюте операции

            # Получение суммы в валюте карты
            amount = df.iloc[i]['text'].strip() if i < n else ''
            i += 1

            # Формирование строки
            new_row = {
                'Дата и время операции': f"{date} {time}",
                'Сумма операции в валюте карты': amount,
                'Описание операции': ' '.join(description_parts).strip(),
                'Номер карты': "Карта Пэй"
            }
            
            new_df = pd.concat([new_df, pd.DataFrame([new_row])], ignore_index=True)

        except Exception as e:
            logger.error(f"Ошибка обработки строки {i}: {str(e)}")
            i += 1

    return new_df

def process_default(input_csv_path: str) -> pd.DataFrame:
    """Обработка по умолчанию для неизвестных типов PDF"""
    try:
        # df = pd.read_csv(input_csv_path)
        df = pd.read_csv(input_csv_path, sep=',', quotechar='"', engine='python')
        logger.info(f"Применена обработка по умолчанию, строк: {len(df)}")
        
        # Стандартизация столбцов для совместимости
        column_mapping = {
            'date': 'Дата и время операции',
            'amount': 'Сумма операции в валюте карты',
            'description': 'Описание операции',
            'card': 'Номер карты'
        }
        
        # Переименовываем столбцы, если они существуют
        for old_name, new_name in column_mapping.items():
            if old_name in df.columns:
                df.rename(columns={old_name: new_name}, inplace=True)
        
        return df
    except Exception as e:
        logger.error(f"Ошибка обработки по умолчанию: {str(e)}")
        raise

def save_processed_data(df: pd.DataFrame, input_csv_path: str, suffix: str = "") -> str:
    """Сохраняет обработанные данные в CSV"""
    try:
        output_dir = os.path.dirname(input_csv_path)
        os.makedirs(output_dir, exist_ok=True)
        output_csv_path = os.path.join(output_dir, f"transactions_processed_{suffix}.csv")
        df.to_csv(output_csv_path, index=False, encoding='utf-8-sig')
        logger.debug(f"Данные сохранены в {output_csv_path}")
        return output_csv_path
    except Exception as e:
        logger.error(f"Ошибка сохранения данных: {str(e)}")
        raise

def process_csv(input_csv_path: str, pdf_type: Optional[str] = None) -> str:
    """Основная функция обработки CSV"""
    if not os.path.exists(input_csv_path):
        raise FileNotFoundError(f"Файл не найден: {input_csv_path}")

    processors: Dict[str, callable] = {
        "Tinkoff_Platinum": process_tinkoff_platinum,
        "Tinkoff": process_tinkoff_platinum,        
        "Visa_Gold_Aeroflot": process_visa_gold_aeroflot,
        "Yandex": process_Yandex,
        "default": process_default
    }

    processor = processors.get(pdf_type, processors["default"])
    logger.info(f"Обработка CSV как {pdf_type or 'default'}")

    try:
        df = processor(input_csv_path)
        return save_processed_data(df, input_csv_path, pdf_type.lower() if pdf_type else "default")
    except Exception as e:
        logger.error(f"Ошибка обработки CSV: {str(e)}")
        raise

if __name__ == "__main__":
    # input_csv = "/Users/IgorShvyrkin/Downloads/transactions_Yandex_temp.csv"
    input_csv = "/Users/IgorShvyrkin/Downloads/transactions_Visa_Gold_Aeroflot_temp.csv"
    # input_csv = "/Users/IgorShvyrkin/Downloads/transactions_Tinkoff_Platinum_temp.csv"
    if not os.path.exists(input_csv):
        raise FileNotFoundError(f"Файл не найден: {input_csv}")
    
    try:
        # Определите тип PDF (можно получить из detect_pdf_type из extract_transactions_pdf1.py)
        # pdf_type = "Tinkoff_Platinum"
        pdf_type = "Visa_Gold_Aeroflot"
        # pdf_type = "Yandex"
        # output_csv = process_csv(input_csv, "Tinkoff_Platinum")
        output_csv = process_csv(input_csv, "Visa_Gold_Aeroflot")
        # output_csv = process_csv(input_csv, "Yandex")
        # print(f"Обработанный CSV сохранен: {output_csv}")
        logger.info(f"Обработанный CSV сохранен: {output_csv}")
        
        # Проверка результата
        # result_df = pd.read_csv(output_csv)
        # print("\nРезультат обработки:")
        # print(result_df.head())
    except Exception as e:
        logger.error(f"Ошибка при обработке: {str(e)}")
        sys.exit(1)