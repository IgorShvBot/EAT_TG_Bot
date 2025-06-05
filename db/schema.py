import os
import logging

logger = logging.getLogger(__name__)

def execute_sql_file(path: str, conn) -> None:
    """
    Выполняет SQL-скрипт из файла.

    Args:
        path: путь к SQL-файлу.
        conn: psycopg2 connection.
    """
    if not os.path.exists(path):
        logger.warning("Файл %s не найден, пропуск.", path)
        return

    try:
        with open(path, 'r', encoding='utf-8') as f:
            sql_script = f.read()
        with conn.cursor() as cur:
            cur.execute(sql_script)
            conn.commit()
        logger.info("✅ Выполнен SQL-файл: %s", path)
    except Exception as e:
        conn.rollback()
        logger.error("Ошибка выполнения SQL-файла %s: %s", path, e, exc_info=True)
        raise


def create_tables(conn) -> None:
    """
    Выполняет создание таблиц из /sql/tables.sql
    """
    path = os.path.join(os.path.dirname(__file__), "..", "sql", "tables.sql")
    execute_sql_file(os.path.abspath(path), conn)


def create_indexes(conn) -> None:
    """
    Выполняет создание индексов из /sql/indexes.sql
    """
    path = os.path.join(os.path.dirname(__file__), "..", "sql", "indexes.sql")
    execute_sql_file(os.path.abspath(path), conn)
