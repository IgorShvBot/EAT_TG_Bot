# handlers/config_handlers.py

import os
import yaml
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, MessageHandler, CallbackQueryHandler, filters, CommandHandler
from handlers.utils import ADMIN_FILTER

logger = logging.getLogger(__name__)

CONFIG_FILES = {
    'view_categories': 'categories.yaml',
    'view_special': 'special_conditions.yaml',
    'view_pdf_patterns': 'pdf_patterns.yaml',
    'view_timeouts': 'timeouts.yaml',
    'view_all': None
}


def register_config_menu_handlers(application):
    """
    Регистрирует хендлеры, управляющие YAML-конфигами:
    просмотр, редактирование, загрузка, удаление.
    """
    application.add_handler(CallbackQueryHandler(config_selection_callback, pattern=r'^(view_categories|view_special|view_pdf_patterns|view_timeouts|view_all|back_to_main)$'))
    application.add_handler(CallbackQueryHandler(main_menu_callback, pattern='^(view_config|edit_config|restart|view_logs)$'))
    application.add_handler(CallbackQueryHandler(edit_menu_callback, pattern='^(edit_categories|edit_special|edit_pdf_patterns|edit_timeouts|cancel)$'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ADMIN_FILTER, handle_config_edit), group=2)
    application.add_handler(MessageHandler(filters.Document.ALL & ADMIN_FILTER, handle_config_upload), group=2)
    application.add_handler(CommandHandler("config", handle_config_command, filters=ADMIN_FILTER))

async def handle_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /config — открывает главное меню управления конфигами.
    """
    await show_config_menu(update)

async def config_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор одного или всех конфигов для просмотра."""
    query = update.callback_query
    await query.answer()

    if query.data == 'back_to_main':
        await show_config_menu(query.message)
        return
    elif query.data == 'view_all':
        await send_all_config_files(query)
        return

    filename = CONFIG_FILES.get(query.data)
    await send_single_config_file(query, filename)


async def show_config_menu(message_or_update):
    """Показывает главное меню конфигурации."""
    if isinstance(message_or_update, Update):
        message = message_or_update.message or message_or_update.callback_query.message
    else:
        message = message_or_update

    keyboard = [
        [InlineKeyboardButton("🔎 Просмотреть конфиг", callback_data='view_config')],
        [InlineKeyboardButton("✏️ Редактировать конфиг", callback_data='edit_config')],
        [InlineKeyboardButton("📝 Добавить Категорию - Паттерн", callback_data='add_pattern_interactive')],
        [InlineKeyboardButton("👁️ Просмотреть логи", callback_data='view_logs')],
        [InlineKeyboardButton("🔄 Перезагрузить бота", callback_data='restart')]
    ]
    await message.reply_text("Управление конфигурацией:", reply_markup=InlineKeyboardMarkup(keyboard))


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает переходы из главного меню управления конфигом."""
    query = update.callback_query
    await query.answer()

    match query.data:
        case 'view_config': await show_config_selection(update)
        case 'edit_config': await show_edit_menu(update)
        case 'view_logs': await context.bot.send_message(chat_id=query.message.chat.id, text="⏳ Загрузка логов...")
        case 'restart': await context.bot.send_message(chat_id=query.message.chat.id, text="🔄 Перезапуск...")


async def show_config_selection(update: Update):
    """Меню выбора конфигурационного файла для просмотра."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("Категории", callback_data='view_categories')],
        [InlineKeyboardButton("Спец. условия", callback_data='view_special')],
        [InlineKeyboardButton("PDF паттерны", callback_data='view_pdf_patterns')],
        [InlineKeyboardButton("Таймауты", callback_data='view_timeouts')],
        [InlineKeyboardButton("Все файлы", callback_data='view_all')],
        [InlineKeyboardButton("↩️ Назад", callback_data='back_to_main')]
    ]
    await query.edit_message_text("Выберите конфигурационный файл:", reply_markup=InlineKeyboardMarkup(keyboard))

async def send_single_config_file(query, filename):
    """Отправляет пользователю YAML-файл."""
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    filepath = os.path.join(config_dir, filename)
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        if len(content) > 4000:
            with open(filepath, 'rb') as f_bin:
                await query.message.reply_document(document=f_bin)
        else:
            await query.message.reply_text(
                f"*{filename}*:\n```yaml\n{content}\n```",
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Ошибка при отправке файла {filename}: {e}")
        await query.message.reply_text(f"Ошибка при отправке файла {filename}")


async def send_all_config_files(query):
    """Отправляет все YAML-файлы конфигурации."""
    for key, filename in CONFIG_FILES.items():
        if filename:
            await send_single_config_file(query, filename)
            await asyncio.sleep(0.5)


async def edit_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор файла для редактирования."""
    query = update.callback_query
    await query.answer()

    filename = CONFIG_FILES.get(query.data)
    if not filename:
        await query.edit_message_text("Ошибка: Неизвестный файл.")
        return

    context.user_data['editing_file'] = filename
    await query.edit_message_text(
        text=f"Отправьте YAML содержимое файла {filename} как текст или документ. Используйте /cancel для отмены."
    )


async def show_edit_menu(update: Update):
    """Показывает меню выбора файла для редактирования."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("Категории", callback_data='edit_categories')],
        [InlineKeyboardButton("Спец. условия", callback_data='edit_special')],
        [InlineKeyboardButton("PDF паттерны", callback_data='edit_pdf_patterns')],
        [InlineKeyboardButton("Таймауты", callback_data='edit_timeouts')],
        [InlineKeyboardButton("↩️ Отмена", callback_data='cancel')]
    ]
    await query.edit_message_text("Выберите файл для редактирования:", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_config_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовое сообщение с новым содержимым YAML."""
    filename = context.user_data.get('editing_file')
    if not filename:
        return

    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    filepath = os.path.join(config_dir, filename)

    try:
        parsed_data = yaml.safe_load(update.message.text)
        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.dump(parsed_data, f, allow_unicode=True, sort_keys=False)
        await update.message.reply_text(f"✅ Файл {filename} успешно обновлён")
    except Exception as e:
        logger.error(f"Ошибка записи YAML: {e}")
        await update.message.reply_text(f"❌ Ошибка в YAML: {e}")
    finally:
        context.user_data.pop('editing_file', None)


async def handle_config_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает загрузку YAML-файла конфигурации как документа."""
    filename = context.user_data.get('editing_file')
    if not filename:
        return

    document = update.message.document
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    filepath = os.path.join(config_dir, filename)

    try:
        new_file = await document.get_file()
        downloaded_path = await new_file.download_to_drive()
        with open(downloaded_path, 'r', encoding='utf-8') as f:
            yaml.safe_load(f.read())
        os.replace(downloaded_path, filepath)
        await update.message.reply_text(f"✅ Файл {filename} успешно обновлён")
    except Exception as e:
        logger.error(f"Ошибка загрузки YAML: {e}")
        await update.message.reply_text(f"❌ Ошибка загрузки файла: {e}")
    finally:
        context.user_data.pop('editing_file', None)
