import psycopg2
import logging
from contextlib import contextmanager
from db.config import DB_CONFIG

logger = logging.getLogger(__name__)

class DBConnection:
    def __init__(self):
        self.conn = None
        self.connect()

    def connect(self):
        self.conn = psycopg2.connect(**DB_CONFIG)

    @contextmanager
    def cursor(self, dict_cursor: bool = False):
        cursor_factory = psycopg2.extras.DictCursor if dict_cursor else None
        cur = self.conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cur
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error("Ошибка транзакции: %s", e, exc_info=True)
            raise
        finally:
            cur.close()

    def close(self):
        if self.conn and not self.conn.closed:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()