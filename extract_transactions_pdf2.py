import pandas as pd
import re
import os
from typing import List, Optional

def process_tinkoff_platinum(input_csv_path: str) -> pd.DataFrame:
    """Обрабатывает CSV для Tinkoff Platinum"""
    df = pd.read_csv(input_csv_path, header=None, skiprows=1)
    result = []

    i = 0
    while i < len(df):
        current_row = df.iloc[i]

        if isinstance(current_row[0], str) and re.match(r"\d{2}\.\d{2}\.\d{4}", current_row[0]):
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
        df = pd.read_csv(input_csv_path)
    except Exception as e:
        raise ValueError(f"Ошибка чтения CSV файла: {str(e)}")

    # Создаем новый DataFrame с нужными колонками
    # new_df = pd.DataFrame(columns=['Дата и время', 'Дата списания', 'Код авторизации', # 'Категория',
    #                                 'Описание операции', 'Сумма', 'Остаток средств'])

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

    return new_df # pd.DataFrame(result)




def save_processed_data(df: pd.DataFrame, input_csv_path: str, suffix: str = "") -> str:
    """Сохраняет обработанные данные в CSV"""
    output_dir = os.path.dirname(input_csv_path)
    output_csv_path = os.path.join(output_dir, f"transactions_processed_{suffix}.csv")
    df.to_csv(output_csv_path, index=False, encoding='utf-8-sig')
    return output_csv_path

def process_csv(input_csv_path: str, pdf_type: Optional[str] = None) -> str:
    """Основная функция обработки CSV"""
    if pdf_type == "Tinkoff_Platinum":
        df = process_tinkoff_platinum(input_csv_path)
        return save_processed_data(df, input_csv_path, "tinkoff_platinum")
    elif pdf_type == "Visa_Gold_Aeroflot":
        df = process_visa_gold_aeroflot(input_csv_path)
        return save_processed_data(df, input_csv_path, "visa_gold_aeroflot")
    else:
        raise ValueError(f"Неизвестный тип PDF: {pdf_type}")

if __name__ == "__main__":
    # Пример использования
    # input_csv = "/Users/IgorShvyrkin/Downloads/transactions_Visa_Gold_Aeroflot_temp.csv"
    input_csv = "/Users/IgorShvyrkin/Downloads/transactions_Tinkoff_Platinum_temp.csv"
    if not os.path.exists(input_csv):
        raise FileNotFoundError(f"Input CSV file not found: {input_csv}")
    
    try:
        # Определите тип PDF (можно получить из detect_pdf_type из extract_transactions_pdf1.py)
        pdf_type = "Tinkoff_Platinum"
        # pdf_type = "Visa_Gold_Aeroflot"
        output_csv = process_csv(input_csv, pdf_type)
        print(f"Обработанный CSV сохранен: {output_csv}")
    except Exception as e:
        print(f"Ошибка при обработке CSV: {str(e)}")



    # output_csv_path = os.path.join(os.path.dirname(input_csv_path), "transactions_pdf2_combined.csv")
    # df_result.to_csv(output_csv_path, index=False, encoding='utf-8-sig')

    # return output_csv_path