import pandas as pd
import re
import os

def process_csv(input_csv_path: str) -> str:
    """Обрабатывает временный CSV и возвращает путь к очищенному CSV"""
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

    df_result = pd.DataFrame(result, columns=[
        "Дата и время операции",
        "Сумма операции в валюте карты",
        "Описание операции",
        "Номер карты"
    ])

    output_csv_path = os.path.join(os.path.dirname(input_csv_path), "transactions_pdf2_combined.csv")
    df_result.to_csv(output_csv_path, index=False, encoding='utf-8-sig')

    return output_csv_path