import pdfplumber
import re
import pandas as pd

# Открываем PDF файл
file_path = "/Users/IgorShvyrkin/Downloads/Выписка_по_счёту_кредитной_карты.pdf"

with pdfplumber.open(file_path) as pdf:
    line_index = 0  # Счётчик для нумерации строк
    matching_indices = []  # Список для хранения индексов подходящих строк

    for page in pdf.pages:
        text = page.extract_text()
        if text:  # Проверяем, что текст успешно извлечен
            for line in text.split("\n"):
                line_index += 1  # Увеличиваем счётчик для каждой строки
                # Проверяем, соответствует ли строка регулярному выражению
                if re.match(r"\d{2}\.\d{2}\.\d{4}\s+(\d{2}:\d{2})", line):
                    print(f"Индекс строки: {line_index}, Содержимое: {line}")
                    matching_indices.append(line_index)  # Сохраняем индекс строки

    # Выводим список всех индексов строк, удовлетворяющих условию
    print("\nИндексы строк, соответствующих условию:")
    print(matching_indices)

# Пример использования
# pdf_path = "/Users/IgorShvyrkin/Downloads/Выписка_по_счёту_кредитной_карты.pdf"
# df = extract_with_camelot(pdf_path)
# print(df.to_csv(sep=';', index=False, encoding='utf-8'))