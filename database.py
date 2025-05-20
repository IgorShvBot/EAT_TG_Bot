import psycopg2
from psycopg2.extras import execute_batch
from psycopg2 import sql  # Для безопасной работы с SQL-идентификаторами
import subprocess
from datetime import datetime, timedelta
import pandas as pd
import os
import logging
from contextlib import contextmanager
from dotenv import load_dotenv
import sys # Импортируем sys для StreamHandler
import pytz

# Загрузите переменные окружения из .env
load_dotenv()

# Настройка логгера для бэкапов (можно оставить INFO)
backup_logger = logging.getLogger('backup.database')
# backup_logger.setLevel(logging.INFO) # Можно оставить фиксированный уровень или тоже сделать через ENV

# Основной логгер для этого файла
logger = logging.getLogger(__name__)

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Настройка логирования для основного логгера
def setup_database_logging():
    # Удаляем все существующие обработчики (чтобы избежать дублирования)
    logger.handlers.clear()

    # Читаем уровень логирования из переменной окружения, по умолчанию INFO
    log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
    log_level_mapping = {
        'CRITICAL': logging.CRITICAL,
        'ERROR': logging.ERROR,
        'WARNING': logging.WARNING,
        'INFO': logging.INFO,
        'DEBUG': logging.DEBUG,
        'NOTSET': logging.NOTSET
    }
    # Устанавливаем уровень для основного логгера, INFO по умолчанию для database.py если ENV нет
    # Или можно использовать INFO как дефолт для database.py, если ENV_VAR не установлен
    # Давайте использовать ENV_VAR как основной, с дефолтом INFO если ENV_VAR отсутствует
    log_level = log_level_mapping.get(log_level_str, logging.INFO)
    logger.setLevel(log_level)
    # >>> КОНЕЦ ИЗМЕНЕНИЯ <<<

    # Единый формат даты (совпадает с bot.py)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%d-%m-%Y %H:%M:%S'  # Формат: "08-05-2025 11:21:06"
    )

    # Один обработчик с унифицированным форматом
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Убедимся, что обработчик добавляется только один раз
    if not logger.handlers:
        logger.addHandler(handler)
    
    # Логируем установленный уровень
    logger.info(f"Уровень логирования для DATABASE установлен в: {logging.getLevelName(logger.level)}")

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
            logger.error(f"Отсутствуют обязательные переменные окружения: {missing_vars}")
            raise EnvironmentError(f"Отсутствуют обязательные переменные окружения: {missing_vars}")

        logger.debug(f"Попытка подключения к БД: host={os.getenv('DB_HOST')}, dbname={os.getenv('DB_NAME')}")
        try:
            self.conn = psycopg2.connect(
                dbname=os.getenv('DB_NAME'),
                user=os.getenv('DB_USER'),
                password=os.getenv('DB_PASSWORD'),
                host=os.getenv('DB_HOST'),
                port=os.getenv('DB_PORT', '5432'),
                connect_timeout=int(os.getenv('DB_CONNECT_TIMEOUT', '5'))
            )
            logger.debug("Подключение к БД успешно")
        except Exception as e:
            logger.error(f"Ошибка подключения к БД: {e}", exc_info=True)
            raise

    @contextmanager
    def get_cursor(self, dict_cursor: bool = False):
        """Контекстный менеджер для безопасной работы с курсором"""
        cursor_factory = psycopg2.extras.DictCursor if dict_cursor else None
        cursor = self.conn.cursor(cursor_factory=cursor_factory)
        # cursor = self.conn.cursor()
        try:
            yield cursor
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Ошибка при работе с БД: {e}", exc_info=True)
            raise
        finally:
            cursor.close()

    def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        with self.get_cursor(dict_cursor=True) as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def get_unique_values(self, column: str, user_id: int) -> list[str]:
        logger.debug(f"Вызов get_unique_values для column={column}, user_id={user_id}")
        logger.debug(f"Проверка наличия fetchall: {hasattr(self, 'fetchall')}")
        
        # Проверяем существование столбца
        with self.get_cursor() as cur:
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'transactions' AND column_name = %s
            """, (column,))
            if not cur.fetchone():
                logger.error(f"Столбец {column} не существует в таблице transactions")
                raise ValueError(f"Столбец {column} не существует в таблице transactions")        

        query = sql.SQL("""
            SELECT DISTINCT {column}
            FROM transactions
            WHERE user_id = %s AND {column} IS NOT NULL
            ORDER BY {column}
        """).format(column=sql.Identifier(column))
        logger.debug(f"Выполняется запрос: {query.as_string(self.conn)} с параметром user_id={user_id}")
        
        try:
            rows = self.fetchall(query, (user_id,))
            result = [row[column] for row in rows]
            logger.debug(f"Получено {len(result)} уникальных значений для столбца {column}")
            return result
        except Exception as e:
            logger.error(f"Ошибка при получении уникальных значений для {column}: {e}", exc_info=True)
            raise

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
            logger.debug(f"Выполнен SQL-скрипт: {filepath}")
        except Exception as e:
            logger.error(f"Ошибка выполнения SQL-скрипта {filepath}: {e}", exc_info=True)
            raise

    def save_transactions(self, df, user_id, pdf_type):
        """Массовая вставка транзакций с возвратом статистики"""
        stats = {'new': 0, 'duplicates': 0, 'updated': 0, 'duplicates_list': []}
        
        # Приведение названий столбцов к нижнему регистру
        df.columns = df.columns.str.lower()
        
        try:
            with self.get_cursor() as cur:
                cur.execute("SELECT nextval('import_id_seq')")
                import_id = cur.fetchone()[0]

                df['дата'] = pd.to_datetime(df['дата'], format='%d.%m.%Y %H:%M', errors='coerce')
                df['сумма'] = pd.to_numeric(df['сумма'].astype(str).str.replace(',', '.'), errors='coerce')
                if 'сумма (куда)' in df.columns:
                    df['сумма (куда)'] = pd.to_numeric(df['сумма (куда)'].astype(str).str.replace(',', '.'), errors='coerce')

                df.dropna(subset=['дата', 'сумма'], inplace=True)

                if df.empty:
                    logger.warning("DataFrame пуст после преобразования дат и сумм")
                    return stats

                logger.debug(f"Сортировка {len(df)} записей по дате (от старых к новым)...")
                df.sort_values(by='дата', ascending=True, inplace=True)

                new_data = []
                disable_duplicates = os.getenv('DISABLE_DUPLICATE_CHECK', 'false').lower() == 'true'
                if disable_duplicates:
                    logger.warning("⚠ Проверка дубликатов отключена (DISABLE_DUPLICATE_CHECK=true)")

                current_time_msk = datetime.now(MOSCOW_TZ)

                for _, row in df.iterrows():
                    is_duplicate = False
                    if not disable_duplicates:
                        is_duplicate = self.check_duplicate(row['дата'], row['наличность'], row['сумма'])

                    if not is_duplicate:
                        new_data.append((
                            import_id, user_id, row['дата'], row['сумма'],
                            row.get('наличность'), row.get('категория'), 
                            row.get('описание'), row.get('контрагент'),
                            row.get('чек #'), row.get('тип транзакции'),
                            row.get('класс'), row.get('сумма (куда)'), 
                            row.get('наличность (куда)'), pdf_type, current_time_msk
                        ))
                        stats['new'] += 1
                    else:
                        stats['duplicates'] += 1
                        stats['duplicates_list'].append({
                            'дата': row['дата'],
                            'сумма': row['сумма'],
                            'наличность': row.get('наличность')
                        })

                if new_data:
                    insert_query = """INSERT INTO transactions (
                            import_id, user_id, transaction_date, amount,
                            cash_source, category, description,
                            counterparty, check_num, transaction_type,
                            transaction_class, target_amount, target_cash_source,
                            pdf_type, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
                        
                    execute_batch(
                        cur,
                        insert_query,
                        new_data,
                        page_size=100
                    )
                    logger.info("✅ Вставлено %d новых транзакций", stats['new'])

                if stats['duplicates']:
                    logger.info("ℹ️ Пропущено как дубликаты: %d записей", stats['duplicates'])
                    for dup in stats['duplicates_list'][:5]:
                        logger.debug(f"Дубликат: Дата={dup['дата']}, Сумма={dup['сумма']}, Наличность={dup['наличность']}")
                    if len(stats['duplicates_list']) > 5:
                        logger.debug(f"...и еще {len(stats['duplicates_list']) - 5} дубликатов")

                return stats
        except Exception as e:
            logger.error(f"Ошибка при сохранении транзакций: {e}", exc_info=True)
            raise
            
    def check_duplicate(self, transaction_date, cash_source, amount):
        """Проверяет наличие дублирующейся транзакции, если не отключено в настройках"""
        if os.getenv('DISABLE_DUPLICATE_CHECK', 'false').lower() == 'true':
            return False
        with self.get_cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM transactions 
                WHERE transaction_date = %s 
                AND cash_source = %s 
                AND amount = %s
            """, (transaction_date, cash_source, amount))
            return cur.fetchone()[0] > 0

    def get_min_max_dates_by_pdf_type(self, user_id: int) -> list[dict]:
        """
        Получает минимальную и максимальную дату транзакции для каждого
        уникального pdf_type для данного пользователя.

        Args:
            user_id: ID пользователя.

        Returns:
            Список словарей, где каждый словарь содержит 'pdf_type',
            'min_date' и 'max_date'.
            Например: [{'pdf_type': 'Tinkoff', 'min_date': datetime(...), 'max_date': datetime(...)}, ...]
            Если данных нет, возвращает пустой список.
        """
        logger.debug(f"Вызов get_max_date_by_pdf_type для user_id={user_id}")
        query = sql.SQL("""
            SELECT 
                pdf_type,
                MIN(transaction_date) AS min_date,
                MAX(transaction_date) AS max_date
            FROM transactions
            WHERE user_id = %s AND pdf_type IS NOT NULL
            GROUP BY pdf_type
            ORDER BY pdf_type;
        """) # Добавлено order by для предсказуемого порядка

        try:
            # Используем fetchall с dict_cursor=True для получения словарей
            results = self.fetchall(query, (user_id,))
            logger.debug(f"Получено {len(results)} записей о максимальных датах.")
            return results
        except Exception as e:
            logger.error(f"Ошибка при получении максимальных дат по pdf_type: {e}", exc_info=True)
            # В случае ошибки лучше вернуть пустой список, чтобы бот не упал
            return []

    def get_transactions(self, user_id, start_date, end_date, filters=None):
        """Получает транзакции с фильтрацией и сортировкой по дате (новые сначала),
           включая начальную и конечную даты полностью."""

        # --- КОРРЕКТИРОВКА КОНЕЧНОЙ ДАТЫ ДЛЯ ПОЛНОГО ВКЛЮЧЕНИЯ ---
        try:
            start_date = start_date if isinstance(start_date, datetime) else datetime.strptime(start_date, '%d.%m.%Y')
            # Преобразуем строку конечной даты в объект date
            end_date_obj = end_date.date() if isinstance(end_date, datetime) else datetime.strptime(end_date, '%d.%m.%Y').date()
            # Вычисляем начало следующего дня
            end_date_exclusive = end_date_obj + timedelta(days=1)
            logger.debug(f"Диапазон дат для запроса: >= {start_date} и < {end_date_exclusive}")
        except ValueError:
            # Обработка ошибки, если формат даты неправильный
            logger.error(f"Неверный формат конечной даты: {end_date}. Запрос может вернуть некорректные данные.")
            # В случае ошибки используем оригинальную дату + 1 день, но это рискованно
            # Лучше убедиться, что даты валидируются раньше в bot.py
            # Для примера, оставляем как есть, но это нужно улучшить
            # end_date_exclusive = end_date # Это вернет старое поведение при ошибке
            # Правильнее было бы здесь либо выбросить исключение, либо использовать +1 день к строке,
            # но это не гарантирует корректность. Примем, что дата валидна.
            # Если формат гарантированно YYYY-MM-DD, эта ошибка не должна происходить.
            # Если все же произошла, используем дату "как есть" + 1 день, что может быть неверно:
            try:
                 # Попытка добавить день к строке (не рекомендуется, но как запасной вариант)
                 year, month, day = map(int, end_date.split('-'))
                 temp_date = datetime(year, month, day) + timedelta(days=1)
                #  end_date_exclusive = temp_date.strftime('%Y-%m-%d')
                 end_date_exclusive = temp_date.date()
            except:
                 end_date_exclusive = end_date # Если все сломалось, используем как есть

        # Используем список для безопасного формирования запроса частями
        query_parts = [
            # Заменяем BETWEEN на >= start_date AND < end_date_exclusive
            sql.SQL("""
            SELECT
                id, transaction_date, amount, cash_source,
                target_amount, target_cash_source, 
                category, description, transaction_type,
                counterparty, check_num, transaction_class                            
            FROM transactions
            WHERE user_id = %s AND transaction_date >= %s AND transaction_date < %s
            """)
        ]
        # Обновляем список параметров с скорректированной конечной датой
        params = [user_id, start_date, end_date_exclusive] # Используем вычисленный следующий день

        if filters:
            # Используем ОТДЕЛЬНЫЙ список для SQL объектов фильтров
            filter_conditions = []
            for key, value in filters.items():
                # Используем безопасные идентификаторы и параметры
                # Игнорируем фильтр, если значение 'Все' или пустое (кроме контрагента/чека, где 'Все' обрабатывается логикой вызова)
                if value is None or value == 'Все' or (isinstance(value, str) and not value.strip() and key not in ['counterparty', 'check_num', 'description']):
                    continue # Пропускаем пустые или "Все" фильтры, кроме текстовых, где пустота может быть введена пользователем
                if key in ['category', 'transaction_type', 'cash_source', 'transaction_class', 'target_cash_source', 'pdf_type']: # Добавлен pdf_type
                    # Для этих полей ищем точное совпадение
                    col = sql.Identifier(key)
                    filter_conditions.append(sql.SQL("{0} = %s").format(col))
                    params.append(value)
                elif key == 'description' and isinstance(value, str) and value.strip():
                    # Частичное совпадение для description без учета регистра
                    filter_conditions.append(sql.SQL("description ILIKE %s"))
                    params.append(f"%{value.strip()}%") # Добавляем % и очищаем пробелы
 
                elif key == 'check_num' and isinstance(value, str) and value.strip(): # Добавлена проверка на непустую строку
                    # Частичное совпадение для check_num без учета регистра
                    filter_conditions.append(sql.SQL("check_num ILIKE %s"))
                    params.append(f"%{value.strip()}%") # Добавляем % и очищаем пробелы
                elif key == 'counterparty' and isinstance(value, str) and value.strip(): # Добавлена проверка на непустую строку
                     # Частичное совпадение для counterparty без учета регистра
                    filter_conditions.append(sql.SQL("counterparty ILIKE %s"))
                    params.append(f"%{value.strip()}%") # Добавляем % и очищаем пробелы
                elif key == 'import_id' and value != 'Все' and value is not None: # Добавлена проверка на None
                    filter_conditions.append(sql.SQL("import_id = %s"))
                    params.append(value)

            # --- КОРРЕКТНОЕ ДОБАВЛЕНИЕ ФИЛЬТРОВ к ОСНОВНОМУ ЗАПРОСУ ---
            if filter_conditions:
                # Добавляем ключевое слово AND как sql.SQL объект
                query_parts.append(sql.SQL("AND"))
                # Добавляем все условия фильтрации, безопасно объединенные через ' AND '
                query_parts.append(sql.SQL(' AND ').join(filter_conditions))
            # ---------------------------------------------------------

        # --- ORDER BY ДОБАВЛЯЕТСЯ ПОСЛЕ ВСЕХ УСЛОВИЙ WHERE ---
        query_parts.append(sql.SQL("ORDER BY transaction_date DESC"))
        # ------------------------------------------------------

        # Собираем финальный запрос (эта строка у вас уже правильная)
        final_query = sql.SQL(' ').join(query_parts)

        # Логирование (у вас уже правильное)
        logger.debug("Выполняется запрос: %s с параметрами %s", final_query.as_string(self.conn), params)

        # Выполнение запроса (у вас уже правильное)
        try:
            with self.get_cursor(dict_cursor=True) as cur:
                cur.execute(final_query, params)
                columns = [desc[0] for desc in cur.description]
                return pd.DataFrame(cur.fetchall(), columns=columns)
        except Exception as e:
            logger.error("Ошибка выполнения запроса: %s", e, exc_info=True) # Добавьте exc_info для полного traceback
            raise

    def get_last_import_ids(self, user_id: int, limit: int = 10) -> list[tuple[int, datetime]]:
            """
            Получает последние N уникальных import_id для пользователя с датой их создания.

            Args:
                user_id: ID пользователя.
                limit: Максимальное количество import_id для возврата.

            Returns:
                Список кортежей (import_id, created_at) отсортированный по дате создания (самые новые первыми).
            """
            logger.debug(f"Вызов get_last_import_ids для user_id={user_id}, limit={limit}")
            query = """
                SELECT DISTINCT ON (t.import_id) t.import_id, t.created_at, t.pdf_type
                FROM transactions t
                WHERE t.user_id = %s
                ORDER BY t.import_id DESC, t.created_at DESC
                LIMIT %s
            """
            try:
                with self.get_cursor() as cur:
                    cur.execute(query, (user_id, limit))
                    results = cur.fetchall()
                    logger.debug(f"Получено {len(results)} последних import_id.")
                    return results
            except Exception as e:
                logger.error(f"Ошибка при получении последних import_id: {e}", exc_info=True)
                raise

    def update_transactions(self, user_id: int, ids: list[int], updates: dict) -> list[int]:
        """Обновляет транзакции с логированием изменений
        
        Args:
            user_id: ID пользователя, выполняющего редактирование
            ids: Список ID записей для обновления
            updates: Словарь вида {'field_name': ('new_value', 'mode')}
                где mode может быть 'replace' или 'append'
        
        Returns:
            Список фактически обновленных ID
        """
        if not ids:
            return []
        
        with self.get_cursor() as cur:
            # Формируем SET часть запроса
            set_parts = []
            params_for_set = []

            for field, (value, mode) in updates.items():
                # Используем безопасное форматирование идентификаторов столбцов
                safe_field = sql.Identifier(field)
                if mode == 'replace':
                    set_parts.append(sql.SQL("{} = %s").format(safe_field))
                elif mode == 'append':
                    set_parts.append(sql.SQL("{} = CONCAT({}, ', ', %s)").format(safe_field, safe_field))
                params_for_set.append(value)
            
            set_parts.append(sql.SQL("edited_by = %s"))
            params_for_set.append(user_id)
            
            set_parts.append(sql.SQL("edited_at = %s")) # <--- ИЗМЕНЕНО с NOW()
            params_for_set.append(datetime.now(MOSCOW_TZ)) # <--- ДОБАВЛЕНО время в MSK
            
            set_parts.append(sql.SQL("edited_ids = %s"))
            params_for_set.append(ids)
            
            set_clause = sql.SQL(', ').join(set_parts)
            query = sql.SQL("""
                UPDATE transactions
                SET {set_clause}
                WHERE id = ANY(%s)
                RETURNING id
            """).format(set_clause=set_clause)
            
            final_query_params = params_for_set + [ids] 
            
            cur.execute(query, final_query_params)
            return [row[0] for row in cur.fetchall()]

    def check_existing_ids(self, ids: list[int]) -> list[int]:
        """Проверяет существование ID в базе"""
        with self.get_cursor() as cur:
            cur.execute("SELECT id FROM transactions WHERE id = ANY(%s)", (ids,))
            return [row[0] for row in cur.fetchall()]

    def close(self):
        """Закрывает соединение с БД"""
        if hasattr(self, 'conn') and self.conn and not self.conn.closed:
            self.conn.close()
            logger.debug("Соединение с БД закрыто")
 
    """ Резервное копирование БД """
    def create_backup(self, backup_dir: str = None):
        """Создает резервную копию базы данных с именем в формате YYYY-MM-DD.backup"""
        if backup_dir is None:
            backup_dir = os.getenv('BACKUP_DIR', os.path.join(os.path.dirname(__file__), 'backups'))
        
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        
        backup_file = os.path.join(backup_dir, f"{datetime.now().strftime('%Y-%m-%d')}.backup")
        db_params = {
            'dbname': os.getenv('DB_NAME'),
            'user': os.getenv('DB_USER'),
            'password': os.getenv('DB_PASSWORD'),
            'host': os.getenv('DB_HOST'),
            'port': os.getenv('DB_PORT', '5432')
        }
        
        try:
            cmd = [
                'pg_dump',
                f"--dbname=postgresql://{db_params['user']}:{db_params['password']}@{db_params['host']}:{db_params['port']}/{db_params['dbname']}",
                '-Fc',  # Формат custom для pg_restore
                '-f', backup_file
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            logger.info(f"Резервная копия создана: {backup_file}")
            self.cleanup_old_backups(backup_dir)
            return backup_file
        except subprocess.CalledProcessError as e:
            logger.error(f"Ошибка создания резервной копии: {e.stderr}")
            raise

    def cleanup_old_backups(self, backup_dir: str):
        """Удаляет резервные копии старше 30 дней"""
        cutoff_date = datetime.now() - timedelta(days=30)
        for file in os.listdir(backup_dir):
            file_path = os.path.join(backup_dir, file)
            if file.endswith('.backup'):
                try:
                    file_date = datetime.strptime(file.split('.')[0], '%Y-%m-%d')
                    if file_date < cutoff_date:
                        os.unlink(file_path)
                        logger.info(f"Удалена старая резервная копия: {file_path}")
                except ValueError:
                    continue