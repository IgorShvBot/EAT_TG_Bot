import os
import sys
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler, CommandHandler
from handlers.utils import ADMIN_FILTER

logger = logging.getLogger(__name__)


def register_restart_handlers(application, bot_instance):
    """Регистрирует хендлеры, связанные с перезапуском."""
    application.add_handler(CommandHandler("restart", bot_instance.restart_bot, filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(bot_instance.restart_bot, pattern='^restart$'))



async def safe_restart(bot_instance):
    """Безопасный перезапуск вне Docker (для использования внутри класса)."""
    if bot_instance._is_restarting:
        return

    bot_instance._is_restarting = True
    logger.info("Начало процесса перезагрузки...")

    if bot_instance._in_docker:
        logger.info("Перезагрузка в Docker не поддерживается. Используйте 'docker restart'.")
        return

    try:
        if bot_instance.application.running:
            await bot_instance.application.stop()
            await bot_instance.application.shutdown()

        logger.info("Запуск нового процесса...")
        os.execv(sys.executable, [sys.executable, __file__])
    except Exception as e:
        logger.error(f"Ошибка при перезапуске: {e}")
        os._exit(1)
    finally:
        bot_instance._is_restarting = False
