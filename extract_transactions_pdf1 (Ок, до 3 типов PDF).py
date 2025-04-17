import camelot
import pandas as pd
import os
import re

def process_pdf(pdf_path: str) -> str:
    """Обрабатывает PDF файл и возвращает путь к временному CSV"""
    # Чтение PDF
    tables = camelot.read_pdf(
        pdf_path,
        flavor="stream",
        pages="all",
        strip_text="\n",
        edge_tol=500,
    )

    if not tables:
        raise ValueError("Не удалось извлечь таблицы из PDF")

    df = pd.concat([table.df for table in tables])

    # Обработка данных
    df = df.drop(df.columns[[1, 2]], axis=1)
    df.columns = range(len(df.columns))
    df = df.reset_index(drop=True)

    # Исправленное условие для маски
    mask = ~(
            df.iloc[:, 0].str.contains("Дата и время|операции", na=False) &
            df.duplicated(subset=df.columns[0], keep='first')
    )
    df = df[mask]

    # Находим начало и конец данных
    start_rows = df[df.iloc[:, 0].str.contains("Дата и время", na=False)]
    if start_rows.empty:
        raise ValueError("Строка с текстом 'Дата и время' не найдена.")
    start_index = start_rows.index[0]

    end_rows = df[df.iloc[:, 0].str.contains("Пополнения:", na=False)]
    if end_rows.empty:
        raise ValueError("Строка с текстом 'Пополнения:' не найдена.")
    end_index = end_rows.index[0]

    # Выбираем нужный диапазон строк
    df = df.loc[start_index:end_index-1]

    # Объединение строк
    if len(df) > 1:
        combined_row = df.iloc[0] + " " + df.iloc[1]
        df.iloc[0] = combined_row
        df = df.reset_index(drop=True)
        df = df.drop(1)

    # Сохранение во временный файл
    temp_csv_path = os.path.join(os.path.dirname(pdf_path), "transactions_pdf1_temp.csv")
    df.to_csv(temp_csv_path, index=False)

    return temp_csv_path