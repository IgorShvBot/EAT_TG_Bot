import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def save_template(user_id: int, name: str, filters: dict, db) -> int:
    query = (
        "INSERT INTO filter_templates (user_id, name, filters_json, created_at) "
        "VALUES (%s, %s, %s, %s) RETURNING id"
    )
    # Преобразуем datetime-значения к строкам, чтобы корректно сериализовать их в JSON
    serializable = {
        k: (v.strftime('%d.%m.%Y') if isinstance(v, datetime) else v)
        for k, v in filters.items()
    }
    with db.cursor() as cur:
        cur.execute(query, (user_id, name, json.dumps(serializable), datetime.now()))
        return cur.fetchone()[0]


def get_templates(user_id: int, db) -> list[dict]:
    query = "SELECT id, name, filters_json FROM filter_templates WHERE user_id = %s ORDER BY created_at DESC"
    with db.cursor(dict_cursor=True) as cur:
        cur.execute(query, (user_id,))
        return cur.fetchall()


def get_template(user_id: int, template_id: int, db) -> dict | None:
    query = "SELECT filters_json FROM filter_templates WHERE id = %s AND user_id = %s"
    with db.cursor() as cur:
        cur.execute(query, (template_id, user_id))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None


def delete_template(user_id: int, template_id: int, db) -> bool:
    query = "DELETE FROM filter_templates WHERE id = %s AND user_id = %s"
    with db.cursor() as cur:
        cur.execute(query, (template_id, user_id))
        return cur.rowcount > 0