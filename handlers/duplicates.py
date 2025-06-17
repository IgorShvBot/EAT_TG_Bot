import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes
from handlers.utils import ADMIN_FILTER
from db.base import DBConnection
from handlers.edit import apply_edits

logger = logging.getLogger(__name__)


def register_duplicate_handlers(application, bot_instance):
    """Регистрирует хендлер обработки дубликатов."""
    application.add_handler(CallbackQueryHandler(
        bot_instance.handle_duplicates_decision,
        pattern='^(update_duplicates|skip_duplicates|view_duplicates)$'
    ))


def update_transaction(date, amount, new_category):
    """Обновляет категорию транзакции по дате и сумме (используется при обработке дубликатов)."""
    with DBConnection() as db:
        with db.cursor() as cur:
            cur.execute("""
                UPDATE transactions
                SET category = %s
                WHERE transaction_date = %s AND amount = %s
            """, (new_category, date, amount))
