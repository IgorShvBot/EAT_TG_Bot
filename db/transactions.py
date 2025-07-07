import pandas as pd
from datetime import datetime, timedelta
from psycopg2.extras import execute_batch
from pytz import timezone
from psycopg2 import sql
import logging

logger = logging.getLogger(__name__)
MOSCOW_TZ = timezone("Europe/Moscow")


def save_transactions(df: pd.DataFrame, user_id: int, pdf_type: str, db) -> dict:
    stats = {'new': 0, 'duplicates': 0, 'duplicates_list': []}

    df.columns = df.columns.str.lower()
    df['дата'] = pd.to_datetime(df['дата'], format='%d.%m.%Y %H:%M', errors='coerce')
    df['сумма'] = pd.to_numeric(df['сумма'].astype(str).str.replace(',', '.'), errors='coerce')
    if 'сумма (куда)' in df.columns:
        df['сумма (куда)'] = pd.to_numeric(df['сумма (куда)'].astype(str).str.replace(',', '.'), errors='coerce')

    df.dropna(subset=['дата', 'сумма'], inplace=True)

    if df.empty:
        return stats

    # Сортировка по возрастанию даты для правильного порядка вставки в БД
    df = df.sort_values(by='дата', ascending=True)

    with db.cursor() as cur:
        cur.execute("SELECT nextval('import_id_seq')")
        import_id = cur.fetchone()[0]

        new_data = []
        for _, row in df.iterrows():
            cur.execute("""
                SELECT COUNT(*) FROM transactions
                WHERE user_id = %s AND date_trunc('minute', transaction_date) = date_trunc('minute', %s)
                AND cash_source = %s AND amount = %s
            """, (user_id, row['дата'], row.get('наличность'), row['сумма']))
            if cur.fetchone()[0] == 0:
                new_data.append((
                    import_id, user_id, row['дата'], row['сумма'],
                    row.get('наличность'), row.get('категория'), row.get('описание'),
                    row.get('контрагент'), row.get('чек #'), row.get('тип транзакции'),
                    row.get('класс'), row.get('сумма (куда)'), row.get('наличность (куда)'),
                    pdf_type, datetime.now(MOSCOW_TZ)
                ))
                stats['new'] += 1
            else:
                stats['duplicates'] += 1
                stats['duplicates_list'].append({
                    'дата': row['дата'], 'сумма': row['сумма'], 'наличность': row.get('наличность')
                })

        if new_data:
            insert_query = """INSERT INTO transactions (
                import_id, user_id, transaction_date, amount, cash_source, category,
                description, counterparty, check_num, transaction_type, transaction_class,
                target_amount, target_cash_source, pdf_type, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
            execute_batch(cur, insert_query, new_data, page_size=100)

    return stats


def get_transactions(user_id: int, start_date, end_date, db, filters: dict = None) -> pd.DataFrame:
    """
    Получает транзакции по пользователю, диапазону дат и фильтрам.

    Args:
        user_id: Telegram ID пользователя.
        start_date: datetime или строка "%d.%m.%Y"
        end_date: datetime или строка "%d.%m.%Y"
        db: экземпляр DBConnection
        filters: dict — фильтры по полям

    Returns:
        DataFrame с транзакциями
    """
    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, "%d.%m.%Y")
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, "%d.%m.%Y")

    # Включаем весь день end_date до 23:59
    end_date_exclusive = (end_date + timedelta(days=1)).date()

    query_parts = [
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
    params = [user_id, start_date, end_date_exclusive]

    if filters:
        filter_conditions = []
        for key, value in filters.items():
            if value in [None, "Все", ""]:
                continue

            if key in ['category', 'transaction_type', 'cash_source', 'transaction_class', 'pdf_type']:
                filter_conditions.append(sql.SQL("{} = %s").format(sql.Identifier(key)))
                params.append(value)
            elif key in ['description', 'counterparty', 'check_num']:
                filter_conditions.append(sql.SQL("{} ILIKE %s").format(sql.Identifier(key)))
                params.append(f"%{value.strip()}%")
            elif key == 'import_id':
                filter_conditions.append(sql.SQL("import_id = %s"))
                params.append(value)

        if filter_conditions:
            query_parts.append(sql.SQL("AND"))
            query_parts.append(sql.SQL(" AND ").join(filter_conditions))

    query_parts.append(sql.SQL("ORDER BY transaction_date DESC"))
    final_query = sql.SQL(" ").join(query_parts)

    with db.cursor(dict_cursor=True) as cur:
        cur.execute(final_query, params)
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
        return pd.DataFrame(rows, columns=columns)
    

def update_transactions(user_id: int, ids: list[int], updates: dict, db) -> list[int]:
    """
    Обновляет транзакции.

    Args:
        user_id: ID пользователя, который делает обновление.
        ids: Список ID транзакций.
        updates: dict формата {'field_name': (new_value, mode)} где mode ∈ {'replace', 'append'}
        db: Экземпляр DBConnection.

    Returns:
        Список обновлённых ID.
    """
    if not ids or not updates:
        logger.warning("Обновление не выполнено: пустой список ID или обновлений")
        return []

    set_parts = []
    params = []

    for field, (value, mode) in updates.items():
        identifier = sql.Identifier(field)
        if mode == 'replace':
            set_parts.append(sql.SQL("{} = %s").format(identifier))
        elif mode == 'append':
            set_parts.append(sql.SQL("{} = CONCAT({}, ', ', %s)").format(identifier, identifier))
        else:
            logger.warning(f"Неизвестный режим обновления: {mode} для поля {field}")
            continue
        params.append(value)

    # Технические поля
    set_parts.append(sql.SQL("edited_by = %s"))
    params.append(user_id)

    set_parts.append(sql.SQL("edited_at = %s"))
    params.append(datetime.now(MOSCOW_TZ))

    query = sql.SQL("""
        UPDATE transactions
        SET {set_clause}
        WHERE id = ANY(%s)
        RETURNING id
    """).format(set_clause=sql.SQL(', ').join(set_parts))

    params.append(ids)

    with db.cursor() as cur:
        cur.execute(query, params)
        updated_ids = [row[0] for row in cur.fetchall()]
        logger.info("Обновлены транзакции: %s", updated_ids)
        return updated_ids


def get_last_import_ids(user_id: int, limit: int, db) -> list[tuple[int, str, str]]:
    """
    Получает последние import_id пользователя.

    Args:
        user_id: Telegram ID пользователя.
        limit: Сколько записей вернуть.
        db: Экземпляр DBConnection.

    Returns:
        Список кортежей: (import_id, created_at, pdf_type)
    """
    query = """
        SELECT DISTINCT ON (import_id) import_id, created_at, pdf_type
        FROM transactions
        WHERE user_id = %s
        ORDER BY import_id DESC, created_at DESC
        LIMIT %s
    """

    with db.cursor() as cur:
        cur.execute(query, (user_id, limit))
        rows = cur.fetchall()
        logger.debug("Получено %d import_id для user_id=%s", len(rows), user_id)
        return [(row[0], row[1].astimezone(MOSCOW_TZ), row[2]) for row in rows]


def get_unique_values(column: str, user_id: int, db) -> list[str]:
    """
    Возвращает уникальные значения указанного столбца в таблице transactions для пользователя.

    Args:
        column: название поля (например, 'category').
        user_id: Telegram ID пользователя.
        db: экземпляр DBConnection.

    Returns:
        Список уникальных значений.
    """
    # Сначала проверим, существует ли такой столбец в таблице
    with db.cursor() as cur:
        cur.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'transactions' AND column_name = %s
        """, (column,))
        if not cur.fetchone():
            logger.warning("Попытка обращения к несуществующему столбцу: %s", column)
            raise ValueError(f"Столбец {column} не существует в таблице transactions")

    # Подготовим безопасный SQL-запрос
    query = sql.SQL("""
        SELECT DISTINCT {column}
        FROM transactions
        WHERE user_id = %s AND {column} IS NOT NULL
        ORDER BY {column}
    """).format(column=sql.Identifier(column))

    with db.cursor() as cur:
        cur.execute(query, (user_id,))
        return [row[0] for row in cur.fetchall()]


def get_min_max_dates_by_pdf_type(user_id: int, db) -> list[dict]:
    """
    Возвращает min/max даты транзакций для каждого pdf_type пользователя.

    Args:
        user_id: Telegram ID пользователя.
        db: экземпляр DBConnection.

    Returns:
        Список словарей: [{'pdf_type': ..., 'min_date': ..., 'max_date': ...}, ...]
    """
    query = sql.SQL("""
        SELECT 
            pdf_type,
            MIN(transaction_date) AS min_date,
            MAX(transaction_date) AS max_date
        FROM transactions
        WHERE user_id = %s AND pdf_type IS NOT NULL
        GROUP BY pdf_type
        ORDER BY pdf_type
    """)

    try:
        with db.cursor(dict_cursor=True) as cur:
            cur.execute(query, (user_id,))
            rows = cur.fetchall()
            logger.debug("Получено %d групп pdf_type по датам.", len(rows))
            return rows
    except Exception as e:
        logger.error("Ошибка при получении диапазонов дат по pdf_type: %s", e, exc_info=True)
        return []


def check_existing_ids(ids: list[int], db) -> list[int]:
    """
    Проверяет, какие из указанных ID реально существуют в таблице transactions.

    Args:
        ids: список ID для проверки.
        db: экземпляр DBConnection.

    Returns:
        Список ID, которые существуют в БД.
    """
    if not ids:
        return []

    query = "SELECT id FROM transactions WHERE id = ANY(%s)"
    with db.cursor() as cur:
        cur.execute(query, (ids,))
        return [row[0] for row in cur.fetchall()]


def get_transaction_fields(tx_id: int, db) -> dict | None:
    """Возвращает значения основных полей для указанной транзакции."""
    query = """
        SELECT cash_source, target_cash_source, category, description,
               transaction_type, counterparty, check_num, transaction_class
        FROM transactions
        WHERE id = %s
    """
    with db.cursor(dict_cursor=True) as cur:
        cur.execute(query, (tx_id,))
        row = cur.fetchone()
        return dict(row) if row else None