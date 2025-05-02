import psycopg2
from psycopg2.extras import execute_batch
from psycopg2 import sql  # Для безопасной работы с SQL-идентификаторами
from datetime import datetime
import pandas as pd
import os
import logging
from contextlib import contextmanager
from dotenv import load_dotenv

# Загрузите переменные окружения из .env
load_dotenv()

# Настройка логирования
logger = logging.getLogger(__name__)
def setup_database_logging():
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

setup_database_logging()

class Database:
    def __init__(self):
        """Инициализация подключения к БД с использованием переменных окружения"""
        self._connect()
        self._create_tables()     # Теперь создаём таблицы
        self._create_indexes()

    def _connect(self):
        REQUIRED_ENV_VARS = ['DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST']
        missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
        if missing_vars:
            raise EnvironmentError(f"Отсутствуют обязательные переменные окружения: {missing_vars}")

        """Устанавливает соединение с БД через переменные окружения"""
        logger.info(f"Попытка подключения к БД: host={os.getenv('DB_HOST')}, dbname={os.getenv('DB_NAME')}")
        try:
            self.conn = psycopg2.connect(
                dbname=os.getenv('DB_NAME'),
                user=os.getenv('DB_USER'),
                password=os.getenv('DB_PASSWORD'),
                host=os.getenv('DB_HOST'),
                port=os.getenv('DB_PORT', '5432'),  # По умолчанию 5432
                connect_timeout=int(os.getenv('DB_CONNECT_TIMEOUT', '5'))
            )
            logger.info("Подключение к БД успешно")
        except Exception as e:
            logger.error(f"Ошибка подключения к БД: {e}")
            raise

    @contextmanager
    def get_cursor(self):
        """Контекстный менеджер для безопасной работы с курсором"""
        cursor = self.conn.cursor()
        try:
            yield cursor
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Ошибка при работе с БД: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def _create_tables(self):
        """Создаёт таблицы из SQL-файлов"""
        tables_sql_path = os.path.join(os.path.dirname(__file__), 'sql', 'tables.sql')
        self._execute_sql_file(tables_sql_path)

    def _create_indexes(self):
        """Создаёт индексы из SQL-файлов"""
        indexes_sql_path = os.path.join(os.path.dirname(__file__), 'sql', 'indexes.sql')
        self._execute_sql_file(indexes_sql_path)

    def check_connection(self):
        """Проверяет статус соединения"""
        try:
            with self.get_cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except psycopg2.InterfaceError:
            return False

    def reconnect(self):
        """Переподключается к БД"""
        self.close()
        self._connect()
        self._create_indexes()

    def retry_on_disconnect(max_retries=2):
        def decorator(func):
            def wrapper(*args, **kwargs):
                retries = 0
                while retries <= max_retries:
                    try:
                        return func(*args, **kwargs)
                    except psycopg2.OperationalError as e:
                        logger.warning(f"Ошибка базы данных: {e}. Пытаюсь повторно подключиться...")
                        args[0].reconnect()
                        retries += 1
                return None  # Или подними исключение
            return wrapper
        return decorator

    @retry_on_disconnect(max_retries=2)
    def get_categories(self, user_id: int) -> list[str]:
        """Возвращает список уникальных категорий для пользователя."""
        """Получает список уникальных категорий пользователя"""
        with self.get_cursor() as cur:
            cur.execute("""
                SELECT DISTINCT category FROM transactions 
                WHERE user_id = %s
                ORDER BY category
            """, (user_id,))
            return [row[0] for row in cur.fetchall()]


    def _execute_sql_file(self, filepath):
        """Загружает и выполняет SQL-файл из указанного пути"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                sql_script = f.read()
            with self.get_cursor() as cur:
                cur.execute(sql_script)
            logger.info(f"Выполнен SQL-скрипт: {filepath}")
        except Exception as e:
            logger.error(f"Ошибка выполнения SQL-скрипта {filepath}: {e}", exc_info=True)
            raise

    def save_transactions(self, df, user_id):
        """Массовая вставка транзакций с обновлением дубликатов по полям"""
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.error("Передан пустой DataFrame или не DataFrame")
            raise ValueError("Передан пустой DataFrame")

        logger.info(f"Начало сохранения {len(df)} транзакций для user_id={user_id}")

        if not self.check_connection():
            logger.warning("Соединение разорвано, переподключаемся...")
            self.reconnect()

        try:
            with self.get_cursor() as cur:
                # Генерируем import_id
                cur.execute("SELECT nextval('import_id_seq')")
                import_id = cur.fetchone()[0]
                logger.debug(f"Сгенерирован import_id={import_id}")

                # Валидация данных
                df['Дата'] = pd.to_datetime(df['Дата'], errors='coerce')
                df['Сумма'] = pd.to_numeric(df['Сумма'], errors='coerce')
                df.dropna(subset=['Дата', 'Сумма'], inplace=True)

                if df.empty:
                    logger.warning("DataFrame стал пустым после очистки")
                    return False

                data = []
                for _, row in df.iterrows():
                    data.append((
                        import_id,
                        user_id,
                        row['Дата'],
                        row['Сумма'],
                        row['Наличность'],
                        row['Категория'],
                        row['Описание'],
                        row['Контрагент'],
                        row['Чек #'],
                        row['Тип транзакции']
                    ))

                execute_batch(
                    cur,
                    """
                    INSERT INTO transactions (
                        import_id, user_id, transaction_date, amount,
                        cash_source, category, description, counterparty,
                        check_num, transaction_type
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (transaction_date, cash_source, amount) 
                    DO UPDATE SET 
                        category = EXCLUDED.category,
                        description = EXCLUDED.description
                    """,
                    data,
                    page_size=100
                )
                logger.info(f"Сохранено {len(data)} транзакций (import_id={import_id})")
                return True
        except Exception as e:
            logger.error(f"Ошибка при сохранении транзакций: {e}", exc_info=True)
            raise

    def check_duplicate(self, transaction_date, cash_source, amount):
        """Проверяет наличие дублирующейся транзакции"""
        with self.get_cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM transactions 
                WHERE transaction_date = %s 
                AND cash_source = %s 
                AND amount = %s
            """, (transaction_date, cash_source, amount))
            return cur.fetchone()[0] > 0

    def get_transactions(self, user_id, start_date, end_date, filters=None):
        """Получает транзакции с фильтрацией"""
        query = """
            SELECT * FROM transactions 
            WHERE user_id = %s 
            AND transaction_date BETWEEN %s AND %s
        """
        params = [user_id, start_date, end_date]

        if filters:
            conditions = []
            for key, value in filters.items():
                if 'LIKE' in key:
                    conditions.append(key)
                    params.append(value)
                else:
                    col = sql.Identifier(key)
                    conditions.append(sql.SQL("{0} = %s").format(col))
                    params.append(value)
            query += " AND " + " AND ".join(conditions)

        with self.get_cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=columns)

    def close(self):
        """Закрывает соединение с БД"""
        if hasattr(self, 'conn') and self.conn and not self.conn.closed:
            self.conn.close()
            logger.info("Соединение с БД закрыто")