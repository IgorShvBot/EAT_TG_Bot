import os
import asyncio
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes
from handlers.utils import ADMIN_FILTER

logger = logging.getLogger(__name__)


def register_log_handlers(application, bot_instance):
    """Регистрирует хендлеры, связанные с логами."""
    application.add_handler(CallbackQueryHandler(bot_instance.view_logs_callback, pattern='^view_logs$', filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(bot_instance.handle_logfile_selection, pattern='^logfile_', filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(bot_instance.handle_log_view_option, pattern='^logview_', filters=ADMIN_FILTER))


def sanitize_log_content(content: str) -> str:
    """Очищает текст лога от символов, мешающих Markdown/HTML."""
    replacements = {
        '<': '&lt;', '>': '&gt;', '&': '&amp;', '`': "'", '*': '',
        '_': '', '[': '(', ']': ')', '~': '-'
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    return content