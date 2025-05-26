import logging
from dotenv import load_dotenv
import pytz
import asyncpg
from db.config import DB_CONFIG

# Загружаем переменные окружения из файла .env
load_dotenv()

# Настройка логгера для бэкапов (можно оставить INFO)
backup_logger = logging.getLogger('backup.database')
# backup_logger.setLevel(logging.INFO) # Можно оставить фиксированный уровень или тоже сделать через ENV

# Основной логгер для этого файла
logger = logging.getLogger(__name__)

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

async def get_pdf_types(user_id: int = None) -> list[str]:
    """
    Возвращает список уникальных pdf_type из таблицы transactions.
    Если user_id указан, фильтрует по конкретному пользователю.
    """
    # Устанавливаем соединение с БД
    conn = await asyncpg.connect(**DB_CONFIG)

    if user_id:
        query = 'SELECT DISTINCT pdf_type FROM transactions WHERE user_id = $1 AND pdf_type IS NOT NULL'
        rows = await conn.fetch(query, user_id)
    else:
        query = 'SELECT DISTINCT pdf_type FROM transactions WHERE pdf_type IS NOT NULL'
        rows = await conn.fetch(query)

    await conn.close()
    return [r['pdf_type'] for r in rows]