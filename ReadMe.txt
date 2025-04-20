20 апреля 2025: обработка разных PDF (Сбер, Т), настройки через YAML файлы

3 скрипта:
1. extract_transactions_pdf1 - извлекает данные из файла pdf и формирует временный файл transactions_pdf1_temp.csv для анализа следующим скриптом 
2. extract_transactions_pdf2 - формирует очищенный и структурированный файл transactions_pdf2_combined.csv для анализа следующим скриптом
3. classify_transactions_pdf - классифицирует данные и формирует итоговый структурированный файл result.csv
