import os
import time
import asyncio
import logging
import pandas as pd
from io import BytesIO
from tempfile import NamedTemporaryFile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, MessageHandler, CallbackQueryHandler, filters
from handlers.utils import ADMIN_FILTER
from db.base import DBConnection
from db.transactions import save_transactions
from handlers.edit import apply_edits  # используется в обработке дубликатов

logger = logging.getLogger(__name__)


def register_pdf_handlers(application, bot_instance):
    """
    Регистрирует хендлеры, связанные с загрузкой PDF и подтверждением сохранения.
    """
    application.add_handler(MessageHandler(filters.Document.PDF & ADMIN_FILTER, bot_instance.handle_document))
    application.add_handler(CallbackQueryHandler(bot_instance.handle_save_confirmation, pattern='^save_(yes|no)$'))
    application.add_handler(CallbackQueryHandler(bot_instance.handle_duplicates_decision, pattern='^(update_duplicates|skip_duplicates)$'))

async def cleanup_files(file_paths):
    """Удаляет временные файлы последовательно."""
    for path in file_paths:
        if path and os.path.exists(path) and os.path.isfile(path):
            try:
                await asyncio.to_thread(os.unlink, path)
                logger.debug(f"Удален временный файл: {path}")
            except Exception as e:
                logger.error(f"Ошибка удаления {path}: {e}")
