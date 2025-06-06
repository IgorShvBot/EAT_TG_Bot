"""
Основной модуль Telegram-бота для обработки банковских выписок.

Бот принимает PDF-файлы, классифицирует транзакции и сохраняет их в базу данных,
а также предоставляет административные команды.
"""

__version__ = "3.7.3"

# === Standard library imports ===
import os
import sys
import socket
import logging
from io import BytesIO
from tempfile import NamedTemporaryFile
import asyncio
import time
import yaml
import re
from datetime import datetime
import inspect

# === Third-party imports ===
import pandas as pd
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)
from telegram_bot_calendar import DetailedTelegramCalendar, LSTEP

# === Local imports ===
from handlers.pdf_type_filter import register_pdf_type_handler
from handlers.export import register_export_handlers, show_filters_menu, generate_report
from handlers.edit import build_edit_keyboard, get_valid_ids, apply_edits
from handlers.filters import get_default_filters
# from handlers.config import register_config_handlers
from handlers.pdf_processing import register_pdf_handlers, cleanup_files
from handlers.logs import register_log_handlers, sanitize_log_content
from handlers.restart import register_restart_handlers
from handlers.duplicates import register_duplicate_handlers
from handlers.config_handlers import register_config_menu_handlers
# from handlers.config_handlers import show_config_menu

from db.base import DBConnection
from db.transactions import (
    save_transactions,
    get_transactions,
    get_last_import_ids,
    get_unique_values,
    get_min_max_dates_by_pdf_type,
)
from config.env import TELEGRAM_BOT_TOKEN, ADMINS, DOCKER_MODE
from config.logging import setup_logging
from config.general import load_general_settings
from config.timeouts import load_timeouts

from utils.parser import parse_settings_from_text


print(">>> setup_logging() должен сейчас вызваться <<<")

# Настройка логирования
setup_logging()
logger = logging.getLogger(__name__)

ALLOWED_USERS = ADMINS
    
# Декоратор для проверки доступа
def admin_only(func):
    """Проверяет, что пользователь является администратором."""
    async def wrapper(*args, **kwargs):  # Сам wrapper должен быть async
        _update_ = None 

        # --- Начало: Логика поиска объекта update и user_id ---
        if len(args) >= 2 and isinstance(args[1], Update):
            _update_ = args[1]
        elif len(args) >= 1 and isinstance(args[0], Update):
            _update_ = args[0]
        elif 'update' in kwargs and isinstance(kwargs['update'], Update):
            _update_ = kwargs['update']
        else:
            _found_update_ = next((arg for arg in args if isinstance(arg, Update)), None)
            if not _found_update_:
                _found_update_ = next((val for val in kwargs.values() if isinstance(val, Update)), None)

            if _found_update_:
                _update_ = _found_update_
            else:
                logger.error(f"admin_only ({func.__name__}): Не удалось найти объект Update в аргументах.")
                # Если update не найден, все равно вызываем оригинальную функцию корректно
                if inspect.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    return func(*args, **kwargs) # Вызов синхронной функции

        if not _update_ or not hasattr(_update_, 'effective_user') or not _update_.effective_user:
            logger.error(f"admin_only ({func.__name__}): Объект Update или effective_user не найден или некорректен.")
            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            else:
                return func(*args, **kwargs)

        user_id = _update_.effective_user.id
        logger.debug(f"admin_only ({func.__name__}): Проверка доступа для user_id: {user_id}. Входит в ALLOWED_USERS: {user_id in ALLOWED_USERS}")

        if user_id not in ALLOWED_USERS:
            logger.warning(f"Попытка доступа от неавторизованного пользователя: {user_id} к функции {func.__name__}")
            if hasattr(_update_, 'message') and _update_.message:
                await _update_.message.reply_text("🚫 Доступ запрещен. Вы не авторизованы для использования этого бота.")
            elif hasattr(_update_, 'callback_query') and _update_.callback_query:
                await _update_.callback_query.answer("Доступ запрещен", show_alert=True)
                logger.debug(f"admin_only ({func.__name__}): Отправлен ответ 'Доступ запрещен' пользователю {user_id}")
            return
        # --- Конец: Логика поиска ---

        # Если проверка на администратора пройдена, вызываем оригинальную функцию корректно
        if inspect.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        else:
            return func(*args, **kwargs)
    return wrapper

# Проверка на дублирующийся запуск
try:
    lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    lock_socket.bind('\0' + 'transaction_bot_lock')  # Уникальное имя для вашего бота
except socket.error:
    print("Бот уже запущен! Завершаю работу")
    sys.exit(1)

# Импорт ваших скриптов
from extract_transactions_pdf1 import process_pdf as extract_pdf1
from extract_transactions_pdf2 import process_csv as extract_pdf2
from classify_transactions_pdf import (classify_transactions, add_pattern_to_category)


class TransactionProcessorBot:
    def __init__(self, token: str):
        """Инициализирует бота, загружая настройки и регистрируя обработчики."""
        self._active_tasks = 0
        self._max_active_tasks = 3  # Максимум 3 одновременно обрабатываемых файла

        self._is_running = False
        self._is_restarting = False  # Флаг перезагрузки  
        self._in_docker = os.getenv('DOCKER_MODE') is not None

        # Логируем ID созданных хендлеров для отладки
        # for i, handler_obj in enumerate(self.config_handlers):
        #     handler_name = "handle_config_edit" if i == 0 else "handle_config_upload"
        #     logger.debug(f"__init__: Создан config_handler ({handler_name}) с ID: {id(handler_obj)}")

        if not self._in_docker:
            # Проверка на дублирующийся запуск только вне Docker
            try:
                lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                lock_socket.bind('\0' + 'transaction_bot_lock')
            except socket.error:
                print("Бот уже запущен! Завершаю работу.")
                sys.exit(1)

        # Загрузка таймаутов
        timeouts = load_timeouts()
        self.download_timeout = timeouts['download_timeout']
        self.processing_timeout = timeouts['processing_timeout']
        self.request_timeout = timeouts['request_timeout']
        self.delay_between_operations = timeouts['delay_between_operations']

        # Загрузка общих настроек
        general_settings = load_general_settings()

        self.log_lines_to_show = general_settings.get('log_lines_to_show', 50) # Значение по умолчанию 50, если в файле нет
        logger.debug(f"Количество строк лога для отображения установлено в: {self.log_lines_to_show}")
        # Загрузка настройки для export_last_import_ids_count
        self.export_last_import_ids_count = general_settings.get('export_last_import_ids_count', 10)
        logger.debug(f"Количество последних import_id для фильтра экспорта установлено в: {self.export_last_import_ids_count}")

        # Настройка Application
        self.application = Application.builder() \
            .token(token) \
            .read_timeout(self.request_timeout) \
            .write_timeout(self.request_timeout) \
            .build()

        # Регистрация обработчиков
        self.setup_handlers()

    def setup_handlers(self):
        """Регистрирует обработчики команд и сообщений."""
        # Основные команды (только для админов)
        self.application.add_handler(CommandHandler("start", self.start))
        # self.application.add_handler(CommandHandler("config", show_config_menu))
        self.application.add_handler(CommandHandler("restart", self.restart_bot))
        self.application.add_handler(CommandHandler("add_pattern", self.add_pattern))
        self.application.add_handler(CommandHandler("add_settings", self.add_settings))
        self.application.add_handler(CommandHandler("settings", self.show_settings))
        self.application.add_handler(CommandHandler("edit", self.start_edit))
        self.application.add_handler(CommandHandler("reset", self.reset_settings))
        self.application.add_handler(CommandHandler("date_ranges", self.get_min_max_dates))

        # автоматически создаем вложенный ConversationHandler
        register_pdf_type_handler(self.application, show_filters_menu)
        register_export_handlers(self.application)
        # register_config_handlers(self.application, self)
        register_pdf_handlers(self.application, self)
        register_log_handlers(self.application, self)
        register_restart_handlers(self.application, self)
        register_duplicate_handlers(self.application, self)
        register_config_menu_handlers(self.application)

        
        self.application.add_handler(CallbackQueryHandler(self.handle_calendar_callback, pattern=r"^cbcal_"),group=0)
        self.application.add_handler(CallbackQueryHandler(self.handle_import_id_callback, pattern='^import_id_'))
        # self.application.add_handler(CallbackQueryHandler(self.debug_callback, pattern='.*'),group=0)

        # Редактирование записей
        self.application.add_handler(CallbackQueryHandler(
            self.handle_edit_choice,
            pattern='^(edit_by_id|edit_by_filter|cancel_edit)$'
        ))
        self.application.add_handler(CallbackQueryHandler(
            self.select_edit_mode,
            pattern='^edit_field_[a-z_]+$'
        ))
        self.application.add_handler(CallbackQueryHandler(
            self.get_new_value,
            pattern='^edit_mode_(replace|append)$'
        ))

        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'^(\d+[\s,-]*)+\d+$'),self.process_ids_input)) #, group=1)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input)) # Добавить перед apply_edits
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,self.apply_edits))
        
        # Обработчики callback-запросов (только для админов)        
        self.application.add_handler(
            CallbackQueryHandler(
                self.handle_pattern_callback,
                pattern='^addpat_'
            )
        )
        
        self.application.add_handler(
            CallbackQueryHandler(
                self.add_pattern_interactive,
                pattern='^add_pattern_interactive$'
            )
        )
        
        self.application.add_handler(CallbackQueryHandler(self.handle_edit_filter_proceed, pattern='^edit_filter_proceed_to_fields$'))

        self.application.add_handler(CommandHandler("cancel", self.cancel_operation))

        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)

    @admin_only
    async def get_min_max_dates(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Обработчик команды /max_dates (или переименованной команды).
        Получает и отправляет пользователю информацию о минимальной и
        максимальной дате операции для каждого pdf_type.
        """
        try:
            with DBConnection() as db:
                date_ranges = get_min_max_dates_by_pdf_type(user_id=update.effective_user.id, db=db)

            if not date_ranges:
                await update.message.reply_text("ℹ️ Данные о датах по типам PDF не найдены. Возможно, база данных пуста или не содержит записей с указанным типом PDF.")
                return

            response_lines = ["⚙️ **Диапазоны дат по типам PDF:**\n"]
            for item in date_ranges:
                pdf_type = item.get('pdf_type', 'Неизвестный тип')
                min_date = item.get('min_date')
                max_date = item.get('max_date')
                min_date_full_str = min_date.strftime('%d.%m.%Y %H:%M') if min_date else 'н/д'
                max_date_full_str = max_date.strftime('%d.%m.%Y %H:%M') if max_date else 'н/д'
                response_lines.append(f"▪️ *{pdf_type}*:\n           min: `{min_date_full_str}`\n           max: `{max_date_full_str}`")
            response_text = "\n".join(response_lines)
            await update.message.reply_text(response_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Ошибка при выполнении команды /date_ranges: {e}", exc_info=True)
            await update.message.reply_text("❌ Произошла ошибка при получении данных. Пожалуйста, попробуйте позже.")

    async def handle_edit_filter_proceed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Применяет фильтры и получает список ID для редактирования."""
        query = update.callback_query
        await query.answer()

        if not context.user_data.get('edit_mode') or \
        context.user_data['edit_mode'].get('type') != 'edit_by_filter' or \
        not context.user_data['edit_mode'].get('edit_filters'):
            await query.edit_message_text("Ошибка: Не найдены фильтры для редактирования.")
            context.user_data.pop('edit_mode', None)
            return

        # Фильтры уже должны быть в context.user_data['edit_mode']['edit_filters']
        # Здесь можно (если еще не сделано) получить ID транзакций по этим фильтрам
        # и сохранить их в context.user_data['edit_mode']['ids']
        try:
            filters_for_db = context.user_data['edit_mode']['edit_filters']
            db_parsed_filters = {}
            filter_keys_to_transfer = [
                'category', 'transaction_type', 'cash_source', 'description',
                'counterparty', 'check_num', 'transaction_class'
            ]
            for key in filter_keys_to_transfer:
                if key in filters_for_db and filters_for_db[key] != 'Все':
                    if key in ['counterparty', 'check_num', 'description']:
                        if isinstance(filters_for_db[key], str) and filters_for_db[key].strip():
                            db_parsed_filters[key] = filters_for_db[key].strip()
                    else:
                        db_parsed_filters[key] = filters_for_db[key]
            if filters_for_db.get('import_id') is not None and filters_for_db['import_id'] != 'Все':
                db_parsed_filters['import_id'] = filters_for_db['import_id']
            logger.debug(f"db_parsed_filters для handle_edit_filter_proceed: {db_parsed_filters}")
            start_date_dt = datetime.strptime(filters_for_db['start_date'], '%d.%m.%Y')
            end_date_dt = datetime.strptime(filters_for_db['end_date'], '%d.%m.%Y')
            with DBConnection() as db:
                df_transactions = get_transactions(
                    user_id=update.effective_user.id,
                    start_date=start_date_dt,
                    end_date=end_date_dt,
                    filters=db_parsed_filters if db_parsed_filters else None,
                    db=db
                )
            ids_from_filter = df_transactions['id'].tolist()
            if not ids_from_filter:
                await query.edit_message_text("⚠ По выбранным фильтрам не найдено записей для редактирования.")
                return
            context.user_data['edit_mode']['ids'] = ids_from_filter
            logger.info(f"Редактирование по фильтру: найдено {len(ids_from_filter)} ID. IDs: {ids_from_filter[:10]}...")
        except Exception as e:
            logger.error(f"Ошибка получения ID по фильтрам: {e}", exc_info=True)
            await query.edit_message_text("⚠️ Ошибка при применении фильтров")
            context.user_data.pop('edit_mode', None)
            return
        await query.edit_message_text(f"ℹ️ Найдено {len(ids_from_filter)} записей для редактирования.")
        await build_edit_keyboard(update, context)


    @admin_only
    async def start_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /edit"""
        context.user_data['edit_mode'] = {}  # Сброс предыдущего состояния
        keyboard = [
            [InlineKeyboardButton("🆔 По ID записи", callback_data='edit_by_id')],
            [InlineKeyboardButton("🔍 По фильтру", callback_data='edit_by_filter')],
            [InlineKeyboardButton("↩️ Отмена", callback_data='cancel_edit')]
        ]
        await update.message.reply_text(
            "📝 Выберите способ редактирования:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_edit_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор способа редактирования"""
        query = update.callback_query
        await query.answer()
        
        if query.data == 'back_to_edit_choice':
            await self.start_edit(update, context)
            return
        elif query.data == 'cancel_edit':
            await query.edit_message_text("ℹ️ Редактирование отменено")
            context.user_data.pop('edit_mode', None)
            return     
        elif query.data == 'edit_by_filter': # было `else:`
            if 'edit_mode' not in context.user_data:
                context.user_data['edit_mode'] = {}
            if 'edit_filters' not in context.user_data['edit_mode']:
                # Получаем default_filters асинхронно
                default_filters = get_default_filters()
                context.user_data['edit_mode']['edit_filters'] = default_filters.copy()
            context.user_data['edit_mode']['type'] = 'edit_by_filter'
            await show_filters_menu(update, context, edit_mode=True)

        if query.data == 'edit_by_id':
            context.user_data['edit_mode'] = {'type': 'edit_by_id', 'awaiting_ids': True} # Устанавливаем флаг
            await query.edit_message_text(
                "📝 Введите ID записей через запятую (например: 15, 28, 42):\n"
                "Или диапазон через дефис (15-28)"
            )
        else:  # edit_by_filter
            await show_filters_menu(update, context, edit_mode=True)


    async def process_ids_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Обрабатывает ввод ID записей от пользователя, проверяет существование и предлагает поля для редактирования.
        """
        try:
            ids = get_valid_ids(update.message.text.strip())
        except ValueError as e:
            await update.message.reply_text(str(e))
            return

        context.user_data['edit_mode'] = {
            'type': 'edit_by_id',
            'ids': ids
        }

        await update.message.reply_text(
            "✏️ Выберите поле для редактирования:",
            reply_markup=build_edit_keyboard()
        )


    async def _select_fields_to_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню выбора полей для редактирования"""
        logger.debug(f"Вызов _select_fields_to_edit для user_id: {update.effective_user.id}")

        keyboard = [
            [InlineKeyboardButton("🏷 Категория", callback_data='edit_field_category')],
            [InlineKeyboardButton("📝 Описание", callback_data='edit_field_description')],
            [InlineKeyboardButton("👥 Контрагент", callback_data='edit_field_counterparty')],
            [InlineKeyboardButton("🧾 Чек #", callback_data='edit_field_check_num')],
            [InlineKeyboardButton("💳 Наличность", callback_data='edit_field_cash_source')],
            [InlineKeyboardButton("📄 Тип PDF", callback_data='edit_field_pdf_type')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_edit_choice')],
            [InlineKeyboardButton("✖️ Отмена", callback_data='cancel_edit')]
        ]
        
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.edit_message_text(
                "✏️ Выберите поле для редактирования:",
                reply_markup=build_edit_keyboard()
            )
        else:
            await update.message.reply_text(
                "✏️ Выберите поле для редактирования:",
                reply_markup=build_edit_keyboard()
            )

    async def select_edit_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбор режима редактирования (замена/добавление)"""
        query = update.callback_query
        await query.answer()
        
        field = query.data.replace('edit_field_', '')
        context.user_data['edit_mode']['field'] = field
        
        keyboard = [
            [InlineKeyboardButton("🔄 Заменить полностью", callback_data='edit_mode_replace')],
            [InlineKeyboardButton("➕ Добавить текст", callback_data='edit_mode_append')],
            [InlineKeyboardButton("↩️ Отмена", callback_data='cancel_edit')]
        ]
        
        await query.edit_message_text(
            f"Выберите способ редактирования поля '{field}':",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def get_new_value(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запрашивает новое значение для выбранного поля"""
        query = update.callback_query
        await query.answer()
        
        context.user_data['edit_mode']['mode'] = query.data.replace('edit_mode_', '')
        
        await query.edit_message_text(
            f"Введите новое значение для поля '{context.user_data['edit_mode']['field']}':\n"
            f"(Режим: {'замена' if context.user_data['edit_mode']['mode'] == 'replace' else 'добавление'})"
        )


    async def apply_edits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Применяет новое значение к выбранному полю для записей, хранящихся в context.user_data['edit_mode'].
        """
        try:
            user_id = update.effective_user.id
            edit_mode = context.user_data.get('edit_mode', {})
            new_value = update.message.text

            count, field = await apply_edits(context, user_id, edit_mode, new_value)

            await update.message.reply_text(
                f"✅ Успешно обновлено {count} записей!\n"
                f"Поле: {field}\n"
                f"Новое значение: {new_value}"
            )

        except Exception as e:
            logger.error(f"Ошибка при редактировании: {e}", exc_info=True)
            await update.message.reply_text("❌ Ошибка при обновлении. Проверьте подключение к базе данных или обратитесь к администратору.")

        finally:
            context.user_data.pop('edit_mode', None)


    async def handle_calendar_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор даты пользователем."""
        query = update.callback_query
        logger.debug(f"Получен callback от календаря: {query.data}")
        # await query.answer()
        
        result, key, step = DetailedTelegramCalendar(locale='ru').process(query.data)

        calendar_context = context.user_data.get("calendar_context")

        # Определяем, активен ли режим редактирования по фильтру
        is_editing_filters = (
            context.user_data.get('edit_mode') and
            context.user_data['edit_mode'].get('type') == 'edit_by_filter'
        )

        if not result and key:
            # ... (логика обновления отображения календаря) ...
            # Примерно так:
            if calendar_context == "start_date":
                context_text_ru = "дату начала"
            elif calendar_context == "end_date":
                context_text_ru = "дату окончания"
            else:
                context_text_ru = "дату"
            await query.edit_message_text(f"📅 Выберите {context_text_ru} ({LSTEP[step]}):", reply_markup=key)
        elif result:
            selected_date_str = result.strftime('%d.%m.%Y')

            # Определяем, какой словарь фильтров использовать
            if is_editing_filters:
                # Убедимся, что 'edit_filters' существует в 'edit_mode'
                if 'edit_filters' not in context.user_data.get('edit_mode', {}): # Проверка, что edit_mode существует
                    if 'edit_mode' not in context.user_data: # Инициализация edit_mode если его нет
                         context.user_data['edit_mode'] = {}
                    context.user_data['edit_mode']['edit_filters'] = get_default_filters().copy()
                
                target_filters_dict = context.user_data['edit_mode']['edit_filters']
                log_source_for_filters = "edit_mode['edit_filters']"
            else:
                if 'export_filters' not in context.user_data:
                    context.user_data['export_filters'] = get_default_filters().copy()
                target_filters_dict = context.user_data['export_filters']
                log_source_for_filters = "export_filters"
            
            logger.debug(f"handle_calendar_callback: Текущие фильтры ({log_source_for_filters}) ДО обновления: {target_filters_dict}")

            if calendar_context == "start_date":
                target_filters_dict['start_date'] = selected_date_str
                logger.debug(f"Установлена дата начала через календарь в {log_source_for_filters}: {selected_date_str}")
            elif calendar_context == "end_date":
                target_filters_dict['end_date'] = selected_date_str
                logger.debug(f"Установлена дата окончания через календарь в {log_source_for_filters}: {selected_date_str}")

            logger.debug(f"handle_calendar_callback: Фильтры ({log_source_for_filters}) ПОСЛЕ обновления: {target_filters_dict}")
            
            if "calendar_context" in context.user_data:
                del context.user_data["calendar_context"]

            await show_filters_menu(update, context, edit_mode=is_editing_filters)


    async def debug_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Логирует данные callback для отладки."""
        query = update.callback_query
        # logger.info("Получен callback: %s", query.data)
        logger.debug(f"DEBUG_CALLBACK: Получен callback_data: '{query.data}' от user_id: {query.from_user.id}") # Улучшенный лог
        await query.answer()

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает текстовые сообщения как фильтры или данные для редактирования."""
        user_id = update.message.from_user.id
        text = update.message.text.strip() # Используем strip() для удаления пробелов

        edit_mode_data = context.user_data.get('edit_mode') or {}
        is_in_edit_process = bool(edit_mode_data.get('field') and edit_mode_data.get('mode')) # Уточненная проверка, ждем ли мы значение для редактирования поля

        logger.debug(f"handle_text_input: Обработка текста '{text}' для user_id {user_id}. Режим: {'edit_mode' if is_in_edit_process else 'фильтры/awaiting_input'}")
        logger.debug(f"handle_text_input: awaiting_input = {context.user_data.get('awaiting_input')}, edit_mode = {edit_mode_data}")


        if not text:
            # Можно добавить проверку на ожидание ввода, если текст пустой, и попросить ввести еще раз
            if context.user_data.get('awaiting_input'):
                 await update.message.reply_text("Пожалуйста, введите непустое значение для фильтра.")
                 # Важно не очищать awaiting_input здесь, чтобы пользователь мог ввести текст заново
                 return # Останавливаем обработку
            else:
                # Если не ожидаем ввода и текст пустой, просто игнорируем или передаем дальше
                logger.debug("Получен пустой текстовый ввод без ожидающего await_input.")
                return # Останавливаем обработку, так как пустой текст не несет смысла

        # --- Проверка на ожидание значения для редактирования поля ---
        # Этот блок должен быть первым после базовой проверки текста
        if is_in_edit_process:
            # Если мы в процессе редактирования поля и ожидаем значение
            logger.debug(f"handle_text_input: Обнаружен активный процесс редактирования поля. Передача в apply_edits.")
            await self.apply_edits(update, context)
            return # Останавливаем обработку здесь
        # ----------------------------------------------------------

        # --- Проверка на ожидание ввода ID для редактирования по ID ---
        # Этот блок должен быть вторым
        # if context.user_data.get('edit_mode', {}).get('type') == 'edit_by_id' and context.user_data.get('edit_mode', {}).get('awaiting_ids'):

        edit_mode_data = context.user_data.get('edit_mode')
        if not isinstance(edit_mode_data, dict):
            edit_mode_data = {}
            context.user_data['edit_mode'] = edit_mode_data

        if edit_mode_data.get('type') == 'edit_by_id' and edit_mode_data.get('awaiting_ids'):

             logger.debug(f"handle_text_input: Обнаружен ожидающий ввод ID для edit_by_id. Передача в process_ids_input.")
             # process_ids_input должен сам сбросить awaiting_ids при успешной обработке
             await self.process_ids_input(update, context)
             # Важно: process_ids_input должен сам решать, продолжать ли обработку (например, вызывать _select_fields_to_edit) или завершить (если ID не найдены).
             # Возвращаемся, чтобы не попасть в логику фильтров ниже.
             return
        # ----------------------------------------------------------


        # --- Логика для фильтров (экспорт или edit_by_filter), когда вводится значение ---
        # Получаем default_filters асинхронно ОДИН РАЗ
        default_filters = get_default_filters() # Этот вызов уже синхронный, не нужно await

        edit_mode_active = edit_mode_data.get('type') == 'edit_by_filter'

        # Определяем, где хранятся фильтры
        if edit_mode_active:
            # Убедимся, что 'edit_filters' существует, инициализируя если нет
            if 'edit_filters' not in context.user_data.get('edit_mode', {}):
                context.user_data.setdefault('edit_mode', {})['edit_filters'] = default_filters.copy()
            filters_storage = context.user_data['edit_mode']['edit_filters']
        else: # Это для export_filters
            # Убедимся, что 'export_filters' существует, инициализируя если нет
            if 'export_filters' not in context.user_data:
                 context.user_data['export_filters'] = default_filters.copy()
            filters_storage = context.user_data['export_filters']

        # Теперь filters_storage точно является словарем
        if not isinstance(filters_storage, dict): # Дополнительная проверка на всякий случай
            logger.error(f"handle_text_input: filters_storage не является словарем: {type(filters_storage)}, значение: {filters_storage}")
            await update.message.reply_text("❌ Внутренняя ошибка при обработке фильтров. Обратитесь к администратору.")
            context.user_data.pop('awaiting_input', None) # Очищаем флаг при ошибке
            return

        # Обработка ввода для различных типов ожидаемого ввода
        awaiting_input_type = context.user_data.pop('awaiting_input', None) # Получаем и удаляем флаг *после* определения filters_storage
        
        if awaiting_input_type == 'counterparty':
            filters_storage['counterparty'] = text
            logger.debug(f"handle_text_input: Установлен фильтр 'counterparty' = '{text}'")
        elif awaiting_input_type == 'check_num':
            filters_storage['check_num'] = text
            logger.debug(f"handle_text_input: Установлен фильтр 'check_num' = '{text}'")
        # --- ДОБАВЬТЕ ЭТОТ БЛОК ---
        elif awaiting_input_type == 'description':
            # Сохраняем введенный текст для фильтра описания
            filters_storage['description'] = text
            logger.debug(f"handle_text_input: Установлен фильтр 'description' = '{text}'")
        # -------------------------
        elif awaiting_input_type:
            # Если был установлен awaiting_input, но для неизвестного типа
             logger.warning(f"handle_text_input: Получен ввод для неизвестного awaiting_input_type: '{awaiting_input_type}' с текстом '{text}'")
             # Не отправляем сообщение пользователю, чтобы не мешать
             return # Важно остановиться

        else:
            # Если мы не ожидали специфического ввода (контрагент/чек/описание)
            # и это не ввод значения для редактирования поля (обработано выше),
            # и это не ввод ID (обработано выше),
            # то это может быть неожиданный текстовый ввод.
            logger.warning(f"handle_text_input: Получен неожиданный текстовый ввод: '{text}' от user_id {user_id} при отсутствии awaiting_input.")
            # Пока не будем ничего отвечать, чтобы не мешать другим потокам.
            return # Важно, чтобы не вызывался show_filters_menu без надобности

        # После установки значения для фильтра (контрагент, чек #, или описание),
        # показываем обновленное меню фильтров
        await show_filters_menu(update, context, edit_mode=edit_mode_active)


    async def handle_import_id_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Обрабатывает выбор import_id из меню"""
            query = update.callback_query
            await query.answer()

            callback_data = query.data
            logger.debug(f"handle_import_id_callback: Получен исходный callback_data: '{callback_data}'")

            selected_import_id = 'Все' # Инициализируем значение по умолчанию

            if callback_data == 'import_id_Все':
                selected_import_id = 'Все'
            elif callback_data.startswith('import_id_'):
                id_str = callback_data[len('import_id_'):]
                logger.debug(f"handle_import_id_callback: Извлечен id_str = '{id_str}', тип = {type(id_str)}")

                try:
                    selected_import_id = int(id_str)
                except ValueError:
                    logger.warning(f"Не удалось преобразовать извлеченную строку '{id_str}' в число. Устанавливаю 'Все'.")
                    selected_import_id = 'Все'
            else:
                logger.warning(f"Получен неожиданный callback_data для import_id: '{callback_data}'. Устанавливаю 'Все'.")
                selected_import_id = 'Все'

            # Определяем, какой словарь фильтров использовать
            edit_mode_active = context.user_data.get('edit_mode') and context.user_data['edit_mode'].get('type') == 'edit_by_filter'
            if edit_mode_active:
                filters_storage = context.user_data['edit_mode'].setdefault('edit_filters', get_default_filters())
            else:
                filters_storage = context.user_data.setdefault('export_filters', get_default_filters())

            # Сохраняем определенное значение import_id
            filters_storage['import_id'] = selected_import_id
            logger.debug(f"handle_import_id_callback: Установлен import_id в фильтрах: {filters_storage['import_id']}")

            # --- ДОБАВЛЕНО: Автоматическая установка даты начала при выборе ID импорта ---
            # Если выбран конкретный ID (не "Все"), устанавливаем давнюю дату начала
            if filters_storage['import_id'] != 'Все':
                past_start_date = datetime(2000, 1, 1) # Желаемая дата начала (например, 1 января 2000)
                filters_storage['start_date'] = past_start_date.strftime('%d.%m.%Y')
                logger.debug(f"handle_import_id_callback: Дата начала автоматически установлена в {filters_storage['start_date']} после выбора ID импорта.")
            # --- КОНЕЦ ДОБАВЛЕНО ---

            # Возвращаемся к основному меню фильтров
            try:
                await show_filters_menu(update, context, edit_mode=edit_mode_active)
            except Exception as e:
                logger.error(f"Ошибка при вызове show_filters_menu: {e}", exc_info=True)
                await update.callback_query.message.reply_text("⚠️ Не удалось обновить меню. Фильтр ID импорта установлен.")
                    
                    
    @admin_only
    async def add_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /add_settings для настройки параметров обработки"""
        args = context.args
        if not args:
            await update.message.reply_text(
                "Использование:\n"
                "/add_settings Контрагент: ОАЭ 2025\n"
                "или отправьте несколько настроек текстом после команды, например:\n\n"
                "/add_settings\n"
                "Контрагент: Калининград 2025\n"
                "Чек: + свой текст"
            )
            return

        # Получаем весь текст сообщения с учётом переносов строк
        full_text = update.message.text[len('/add_settings'):].strip()

        # Парсим настройки из текста
        settings = parse_settings_from_text(full_text)

        if not settings:
            await update.message.reply_text("Не удалось распознать настройки. Проверьте формат.")
            return

        # Сохраняем настройки в контексте пользователя
        context.user_data['processing_settings'] = settings

        # Формируем ответ с подтверждением
        response = "⚙ Настройки сохранены:\n"
        for key, value in settings.items():
            response += f"{key}: {value['value']}\n"

        await update.message.reply_text(response)

    @admin_only
    async def show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает текущие сохраненные настройки"""
        settings = context.user_data.get('processing_settings', {})
        
        if not settings:
            await update.message.reply_text("Настройки не заданы. Используются значения по умолчанию.")
            return
        
        response = "⚙ Текущие настройки:\n"
        for key, value in settings.items():
            response += f"{key}: {value['value']}\n"
        
        await update.message.reply_text(response)

    @admin_only
    async def reset_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сбрасывает сохраненные настройки"""
        context.user_data.pop('processing_settings', None)
        await update.message.reply_text("⚙ Все настройки сброшены к значениям по умолчанию.")


    async def handle_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений с настройками"""
        user_data = context.user_data
        message_text = update.message.text
        
        # Парсим настройки
        settings = parse_settings_from_text(message_text)
        
        # Сохраняем настройки в контексте пользователя
        user_data['processing_settings'] = settings
        
        # Формируем ответ с подтверждением
        response = "⚙ Настройки сохранены:\n"
        for key, value in settings.items():
            response += f"{key}: {value['value']}\n"
        
        response += "\nТеперь отправьте PDF-файл для обработки."
        
        await update.message.reply_text(response)


    async def view_logs_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню выбора логов"""
        query = update.callback_query
        await query.answer()
        
        try:
            log_dir = os.path.join(os.path.dirname(__file__), 'logs')
            if not os.path.exists(log_dir):
                await query.edit_message_text("Папка с логами не найдена")
                return
            
            # Получаем список файлов логов
            log_files = sorted([
                f for f in os.listdir(log_dir) 
                if f.endswith('.log') and os.path.isfile(os.path.join(log_dir, f))
            ], reverse=True)
            
            if not log_files:
                await query.edit_message_text("Файлы логов не найдены")
                return
            
            # Ограничиваем количество файлов в меню (не более 20)
            log_files = log_files[:20]
            
            # Создаем клавиатуру с файлами логов
            keyboard = [
                [InlineKeyboardButton(f, callback_data=f'logfile_{f}')]
                for f in log_files
            ]
            keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data='back_to_main')])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.edit_message_text(
                    text="Выберите файл логов для просмотра:",
                    reply_markup=reply_markup
                )
            except telegram.error.BadRequest as e:
                if "not modified" in str(e):
                    logger.debug("Сообщение не изменилось, пропускаем ошибку")
                else:
                    raise
                    
        except Exception as e:
            logger.error(f"Ошибка при показе меню логов: {e}")
            await query.edit_message_text("Произошла ошибка при загрузке списка логов")

    @admin_only
    async def cancel_operation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отменяет текущую операцию"""
        if 'adding_pattern' in context.user_data:
            del context.user_data['adding_pattern']
            self.application.remove_handler(self.pattern_handler)
            await update.message.reply_text("Добавление паттерна отменено")
        elif 'editing_file' in context.user_data:
            self.remove_config_handlers()
            del context.user_data['editing_file']
            await update.message.reply_text("Редактирование конфига отменено")
        else:
            await update.message.reply_text("Нет активных операций для отмены")


    async def handle_pattern_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор категории для добавления паттерна"""
        query = update.callback_query
        await query.answer()
        
        # Получаем оригинальное название категории из безопасного callback_data
        safe_category = query.data.replace('addpat_', '')
        
        # Находим полное название категории в конфиге
        from classify_transactions_pdf import load_config
        config = load_config()

        full_category = None
        for cat in config['categories']:
            if cat['name'].replace(" ", "_")[:30] == safe_category:
                full_category = cat['name']
                break
        
        if not full_category:
            await query.edit_message_text("Категория не найдена")
            return

        context.user_data['adding_pattern'] = {
            'category': full_category,
            'message': await query.edit_message_text(
            f"Вы выбрали категорию: {full_category}\n"
            "Теперь отправьте мне паттерн для добавления (текст или регулярное выражение).\n"
                "Используйте /cancel для отмены"
        )
        }
        
        # Добавляем обработчик следующего сообщения
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.handle_pattern_input
        ))


    def safe_calendar_pattern_wrapper(self, original_pattern_callable):
        """Безопасно обрабатывает проверки паттернов календаря, перехватывая AttributeErrors."""
        def wrapper(data: str) -> bool:
            try:
                # Вызываем оригинальную функцию проверки паттерна календаря
                return original_pattern_callable(data)
            except AttributeError as e:
                # Перехватываем конкретную ошибку, указывающую на строку без .data
                if "'str' object has no attribute 'data'" in str(e):
                    return False # Считаем, что это не паттерн календаря
                # Перевызываем другие AttributeErrors
                raise
            # Не перехватываем TypeError и другие исключения
        return wrapper


    async def handle_pattern_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает ввод паттерна"""
        if 'adding_pattern' not in context.user_data:
            await update.message.reply_text("Сессия добавления паттерна устарела")
            return
        
        pattern = update.message.text
        category = context.user_data['adding_pattern']['category']
        
        try:
            from classify_transactions_pdf import add_pattern_to_category
            add_pattern_to_category(category, pattern)
            
            await update.message.reply_text(
                f"✅ Паттерн '{pattern}' успешно добавлен в категорию '{category}'"
            )
        except Exception as e:
            logger.error(f"Ошибка добавления паттерна: {e}")
            await update.message.reply_text(
                f"Ошибка при добавлении паттерна: {str(e)}"
            )
        finally:
            # Удаляем временные данные и обработчик
            if 'adding_pattern' in context.user_data:
                del context.user_data['adding_pattern']
            self.application.remove_handler(self.pattern_handler)

    @admin_only
    async def add_pattern(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды добавления нового паттерна"""
        try:
            # Разделяем команду на три части: /add_pattern, категория, паттерн
            args = update.message.text.split(maxsplit=2)
            # Проверяем количество аргументов (команда + 2 аргумента)
            if len(args) != 3:
                await update.message.reply_text(
                    "Использование: /add_pattern \"Категория\" \"Паттерн\"\n\n"
                    "Пример: /add_pattern \"Еда\" \"VKUSVILL\""
                )
                return
                
            category = args[1]  # "Домашние животные"
            pattern = args[2]   # "VET UNION"

            # Объединяем аргументы в случае, если они содержат пробелы
            # try:
            #     category = ' '.join(args[:-1]).strip('"\'')
            #     pattern = args[-1].strip('"\'')

            # Вызываем функцию добавления паттерна
            from classify_transactions_pdf import add_pattern_to_category
            add_pattern_to_category(category, pattern)
            
            await update.message.reply_text(f"Паттерн '{pattern}' успешно добавлен в категорию '{category}'")
        except Exception as e:
            logger.error(f"Ошибка добавления паттерна: {str(e)}")
            await update.message.reply_text(f"Ошибка: {str(e)}")

        except Exception:
            await update.message.reply_text("Неверный формат команды")
            return


    async def add_pattern_interactive(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Интерактивное добавление паттерна"""
        query = update.callback_query
        await query.answer()
        
        # Загружаем список категорий
        from classify_transactions_pdf import load_config
        config = load_config()
        
        # Создаем безопасные callback_data
        categories = []
        for cat in config['categories']:
            name = cat['name']
            # Заменяем пробелы и спецсимволы, обрезаем длину
            safe_name = name.replace(" ", "_")[:30]  # Максимум 30 символов
            categories.append((name, f'addpat_{safe_name}'))
        
        if not categories:
            await query.edit_message_text("Нет доступных категорий")
            return
        
        # Создаем клавиатуру с категориями
        keyboard = [
            [InlineKeyboardButton(name, callback_data=callback_data)]
            for name, callback_data in categories
        ]
        keyboard.append([InlineKeyboardButton("Отмена", callback_data='back_to_main')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_text(
                text="Выберите категорию для добавления паттерна:",
                reply_markup=reply_markup
            )
        except telegram.error.BadRequest as e:
            logger.error(f"Ошибка при создании клавиатуры: {e}")
            await query.edit_message_text(
                text="Произошла ошибка при создании меню. Попробуйте снова."
            )
        
        # Устанавливаем следующий шаг
        context.user_data['next_step'] = 'await_pattern'

         
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Логирует ошибки и уведомляет пользователя"""
        error = context.error
        logger.error("Ошибка: %s, update: %s", error, update, exc_info=True)

        if isinstance(error, telegram.error.Forbidden):
            logger.error("Бот заблокирован пользователем")
            return
        elif isinstance(error, telegram.error.BadRequest):
            logger.error(f"Ошибка в запросе: {error}")
            if isinstance(update, Update) and update.callback_query:
                try:
                    await update.callback_query.answer("Произошла ошибка, попробуйте снова")
                except:
                    pass
            return
        
        logger.error("Исключение при обработке запроса:", exc_info=context.error)
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.answer("Произошла ошибка, попробуйте позже")

    # Основные команды
    # Все методы класса теперь используют декоратор @admin_only
    @admin_only
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        welcome_text = (
            "👋 Привет! Я ваш личный финансовый помощник.\n"
            "Я помогу обработать PDF-выписки из банка, автоматически распределю транзакции по категориям и сохраню их в базу данных для дальнейшего анализа.\n\n"
            "<b>С чего начать:</b>\n"
            "1. 📤 <b>Отправьте мне PDF-файл</b> с банковской выпиской.\n"
            "2. 💾 После обработки я предложу сохранить распознанные транзакции.\n"
            "3. 📄 Транзакции, требующие ручной классификации, будут в отдельном файле.\n\n"
            "<b>Основные возможности и команды:</b>\n"
            "• /export - Выгрузить транзакции в CSV файл, используя гибкие фильтры.\n"
            "• /edit - Редактировать детали существующих записей в базе данных.\n"
            "• /date_ranges - Показать минимальные и максимальные даты операций по каждому типу PDF в базе.\n"
            "• /config - Центр управления: здесь можно настроить категории, паттерны для автоклассификации, просмотреть логи или перезагрузить бота.\n"
            "• <code>/add_pattern \"Категория\" \"Паттерн\"</code> - Быстро добавить новое правило для автоматической классификации транзакций (например, <code>/add_pattern \"Продукты\" \"АЗБУКА ВКУСА\"</code>).\n\n"
            "<b>Управление настройками обработки PDF:</b>\n"
            "Перед отправкой PDF-файла (или используя команду /add_settings) вы можете задать специфические параметры для обработки. Например:\n"
            "   <code>Описание: +Командировка СПб</code> (добавит текст к описанию всех транзакций из файла)\n"
            "   <code>PDF: 1</code> (для получения промежуточных файлов обработки)\n"
            "   <code>Класс: Личные расходы</code> (установит класс для всех транзакций)\n"
            "Для управления этими настройками:\n"
            "• /add_settings - Задать или изменить настройки обработки.\n"
            "• /show_settings - Посмотреть текущие активные настройки.\n"
            "• /reset_settings - Сбросить все настройки обработки к значениям по умолчанию.\n\n"
            "⏳ <i>Обработка PDF-файла может занять некоторое время.</i>\n"
            "✨ Успешной работы и точного учета!"            
        )
         
        await update.message.reply_text(welcome_text, parse_mode='HTML')
 

    async def main_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает callback из главного меню"""
        query = update.callback_query
        await query.answer()
        
        if query.data == 'view_config':
            await self.show_config_selection(update, context)
        elif query.data == 'edit_config':
            await self.show_edit_menu(update, context)
        elif query.data == 'view_logs':
            await self.view_logs_callback(update, context)
        elif query.data == 'restart':
            await self.restart_bot(update, context)


    async def send_config_files(self, query):
        """Отправляет содержимое конфигов как текстовые сообщения"""
        config_dir = os.path.join(os.path.dirname(__file__), 'config')
        # config_dir = '/app/config'
        
        config_files = {
            'categories.yaml': 'Категории транзакций',
            'special_conditions.yaml': 'Специальные условия',
            'timeouts.yaml': 'Таймауты обработки'
        }
        
        for filename, description in config_files.items():
            filepath = os.path.join(config_dir, filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Разбиваем на части если слишком длинное
                    if len(content) > 4000:
                        parts = [content[i:i+4000] for i in range(0, len(content), 4000)]
                        for part in parts:
                            await query.message.reply_text(f"`{part}`", parse_mode='Markdown')
                            await asyncio.sleep(0.5)
                    else:
                        await query.message.reply_text(f"*{description}*:\n`{content}`", 
                                                    parse_mode='Markdown')
                    
                except Exception as e:
                    logger.error(f"Ошибка при отправке файла {filename}: {e}")
                    await query.message.reply_text(f"Ошибка при отправке {filename}")
            else:
                await query.message.reply_text(f"Файл {filename} не найден")
                

    def remove_config_handlers(self):
        """Удаляет обработчики редактирования конфига"""
        logger.info("remove_config_handlers: Удаляю config_handlers из группы -1.")
        for handler_obj in self.config_handlers: # handler_obj
            handler_name = "handle_config_edit" if handler_obj.callback == self.handle_config_edit else "handle_config_upload"
            logger.debug(f"remove_config_handlers: Удаляю config_handler ({handler_name}) с ID: {id(handler_obj)} из группы -1.")
            self.application.remove_handler(handler_obj, group=-1)

    # Обработка документов

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обновленный обработчик документов"""
        user_data = context.user_data
        
        # Получаем сохраненные настройки или используем по умолчанию
        settings = user_data.get('processing_settings', {})
        return_files = settings.get('pdf', {'value': '0'})['value']
        if return_files not in ('0', '1', '2'):
            return_files = '0'
            logger.warning(f"Некорректное значение настройки PDF: {return_files}, использовано значение по умолчанию '0'")
        
        # Очищаем настройки после использования (опционально)
        user_data.pop('processing_settings', None)
        
        document = update.message.document
        if not document.file_name.lower().endswith('.pdf'):
            await update.message.reply_text("Пожалуйста, отправьте файл в формате PDF.")
            return

        if document.file_size > 10 * 1024 * 1024:  # 10 MB
            await update.message.reply_text("Файл слишком большой. Максимальный размер - 10 МБ.")
            return

        # logger.info(f"Получен файл: {document.file_name}")
        await update.message.reply_text("Начинаю обработку...")
        
        # logger.info(f"Начата обработка PDF: {document.file_name}, размер: {document.file_size} байт")
        logger.info(f"Начата обработка PDF: {document.file_name}, размер: {round(document.file_size / (1024 * 1024), 2)} МБ")
        logger.info(f"Используются настройки: return_files={return_files}")

        tmp_pdf_path = temp_csv_path = combined_csv_path = result_csv_path = unclassified_csv_path = None

        try:
            # Скачивание и обработка PDF
            pdf_file = BytesIO()
            file = await document.get_file()
            await file.download_to_memory(out=pdf_file)
            pdf_file.seek(0)  # Перемещаем указатель в начало

            with NamedTemporaryFile(suffix='.pdf', delete=False) as tmp_pdf:
                tmp_pdf.write(pdf_file.getbuffer())
                tmp_pdf_path = tmp_pdf.name

            temp_csv_path, pdf_type = await asyncio.to_thread(extract_pdf1, tmp_pdf_path)
            combined_csv_path = await asyncio.to_thread(extract_pdf2, temp_csv_path, pdf_type)
            
            # Получаем ОБА пути
            result_csv_path, unclassified_csv_path = await asyncio.to_thread(
                classify_transactions, combined_csv_path, pdf_type, user_settings=settings
            )

            df = pd.read_csv(
                result_csv_path,
                sep=';',          # Указываем разделитель
                quotechar='"',     # Символ кавычек
                encoding='utf-8',  # Кодировка
                on_bad_lines='warn' # Обработка битых строк
                )

            context.user_data['pending_data'] = {
                'df': df,
                'pdf_type': pdf_type,
                'timestamp': time.time()  # Фиксируем время получения данных
            }

            # Отправка файлов согласно настройкам
            files_to_send = []
            
            if return_files == '1':
                files_to_send.append(temp_csv_path)
            elif return_files == '2':
                files_to_send.extend([temp_csv_path, combined_csv_path])
            else:  # default - только итоговый файл
                files_to_send.append(result_csv_path)
                # Добавляем unclassified только при отправке итогового файла
                if unclassified_csv_path and os.path.exists(unclassified_csv_path):
                    unclassified_df = pd.read_csv(unclassified_csv_path)
                    unclassified_caption = f"✍️ Транзакции для ручной классификации\n🗂️ Всего записей: {len(unclassified_df)}"
                    with open(unclassified_csv_path, 'rb') as f:
                        await update.message.reply_document(document=f, caption=unclassified_caption)                    
                    # files_to_send.append(unclassified_csv_path)

            # Отправка выбранных файлов
            for file_path in files_to_send:
                if file_path and os.path.exists(file_path):
                    caption = "✍️ Транзакции для ручной классификации" if file_path == unclassified_csv_path else None
                    with open(file_path, 'rb') as f:
                        file_caption = f"🗃️ Всего записей: {len(df)}"
                        if caption:
                            file_caption = f"{caption}\n{file_caption}"
                        await update.message.reply_document(document=f, caption=file_caption)                        

            context.user_data['temp_files'] = [
                tmp_pdf_path,
                temp_csv_path,
                combined_csv_path,
                result_csv_path,
                unclassified_csv_path
            ]

            # Создаем клавиатуру с кнопками
            keyboard = [
                [InlineKeyboardButton("Да ✅", callback_data='save_yes'),
                InlineKeyboardButton("Нет ❌", callback_data='save_no')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Отправляем вопрос
            await update.message.reply_text(
                "Сохранить эти данные в базу данных?",
                reply_markup=reply_markup
            )

        except Exception as e:
            logger.error(f"Ошибка обработки PDF: {str(e)}", exc_info=True)
            await update.message.reply_text(
                "Произошла ошибка при обработке файла.\n"
                "Пожалуйста, убедитесь, что:\n"
                "1. Это корректная банковская выписка\n"
                "2. Файл не поврежден\n"
                "3. Формат соответствует поддерживаемым (Tinkoff, Сбербанк, Яндекс)"
            )
            # Удаляем pending_data в случае ошибки
            if 'pending_data' in context.user_data:
                del context.user_data['pending_data']

        finally:
            if pdf_file:
                pdf_file.close()
            if tmp_pdf:
                tmp_pdf.close()


    async def handle_save_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сохраняет данные после подтверждения пользователя."""
        query = update.callback_query
        await query.answer()
        
        user_data = context.user_data
        
        if query.data == 'save_no':
            await query.edit_message_text("ℹ️ Данные не сохранены")
            
            if 'temp_files' in user_data:
                await cleanup_files(user_data['temp_files'])
                del user_data['temp_files']
            
            if 'pending_data' in user_data:
                del user_data['pending_data']
            return
        
        # Только для ответа "Да" продолжаем проверки
        pending_data = user_data.get('pending_data', {})
        pdf_type_to_save = pending_data.get('pdf_type')

        user_id = update.effective_user.id
        df = context.user_data['pending_data']['df']
        # pdf_type = context.user_data['pending_data']['pdf_type']


        # if not pending_data or 'timestamp' not in pending_data or 'df' not in pending_data:
        if not pending_data or 'df' not in pending_data or 'pdf_type' not in pending_data:
            await query.edit_message_text("Данные для сохранения не найдены или повреждены (DataFrame или pdf_type отсутствуют)")
            return
            
        if time.time() - pending_data['timestamp'] > 300:
            await query.edit_message_text("⏳ Время подтверждения истекло (максимум 5 минут)")
            return

        logger.debug("Сохранение данных в БД: %s", pending_data['df'][['Дата']].head().to_dict())
        db = None
        try:
            db = DBConnection()
            stats = save_transactions(df, user_id=user_id, pdf_type=pdf_type_to_save, db=db)
            db.close()
            
            logger.info(
                f"💾 Сохранено: 🆕 новых - {stats['new']}, 📑 дубликатов - {stats['duplicates']}"
            )
            
            # Сохраняем статистику в user_data для использования в handle_duplicates_decision
            context.user_data['last_save_stats'] = stats

            if stats['duplicates'] > 0:
                context.user_data['pending_duplicates'] = stats['duplicates_list']
                keyboard = [
                    [InlineKeyboardButton("Обновить дубликаты 🔄", callback_data='update_duplicates')],
                    [InlineKeyboardButton("Пропустить ➡️", callback_data='skip_duplicates')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    f"🔍 Найдено {stats['duplicates']} дубликатов. Обновить записи?",
                    reply_markup=reply_markup
                )
            else:
                await query.edit_message_text(
                    f"✅ Успешно сохранено {stats['new']} записей"
                )
        except Exception as e:
            logger.error(f"Ошибка БД: {str(e)}", exc_info=True)
            await query.edit_message_text(
                "❌ Ошибка при сохранении в БД\n"
                "Проверьте:\n"
                "1. Запущен ли сервер PostgreSQL\n"
                "2. Правильно ли настроены переменные окружения (DB_HOST, DB_PORT и др.)"
            )
        finally:
            if db is not None:  # Закрываем соединение только если оно было создано
                db.close()
            
            # Очистка временных данных
            if 'temp_files' in user_data:
                await cleanup_files(user_data['temp_files'])
                del user_data['temp_files']
            
            if 'pending_data' in user_data:
                del user_data['pending_data']


    async def handle_duplicates_decision(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор пользователя для найденных дубликатов."""
        query = update.callback_query
        await query.answer()
        
        user_data = context.user_data
        duplicates = user_data.get('pending_duplicates', [])
        stats = user_data.get('last_save_stats', {'new': 0, 'duplicates': 0, 'duplicates_list': []})

        if not duplicates:
            await query.edit_message_text("ℹ️ Нет данных для обновления")
            user_data.pop('pending_duplicates', None)
            user_data.pop('last_save_stats', None)
            return

        if query.data == 'update_duplicates':
            try:
                updated = 0
                with DBConnection() as db:
                    for row in duplicates:
                        # Находим ID транзакции по критериям дубликата
                        with db.cursor(dict_cursor=True) as cur:
                            cur.execute("""
                                SELECT id FROM transactions 
                                WHERE transaction_date = %s 
                                AND amount = %s 
                                AND cash_source = %s
                            """, (row['дата'], row['сумма'], row.get('наличность')))
                            result = cur.fetchone()
                            if result:
                                # Обновляем транзакцию по ID
                                updates = {'category': (row.get('категория', None), 'replace')}
                                updated_ids = apply_edits(
                                    user_id=query.from_user.id,
                                    ids=[result['id']],
                                    updates=updates,
                                    db=db
                                )
                                if updated_ids:
                                    updated += 1
                logger.info(f"Обновлено {updated} дубликатов")
                await query.edit_message_text(
                    f"✅ Обновлено {updated} записей\n"
                    f"🆕 Сохранено ранее: {stats['new']} записей"
                )
            except Exception as e:
                logger.error(f"Ошибка обновления: {e}", exc_info=True)
                await query.edit_message_text("❌ Ошибка при обновлении")
        
        elif query.data == 'skip_duplicates':
            response = (
                f"🔄 Дубликаты пропущены: {stats['duplicates']}\n"
                f"✅ Успешно сохранено: {stats['new']}"
            )
            if stats['new'] == 0:
                response = (
                    f"🔄 Дубликаты пропущены: {stats['duplicates']}\n"
                    f"ℹ️ Новые записи не сохранены"
                )
            
            await query.edit_message_text(response)
        
        user_data.pop('pending_duplicates', None)
        user_data.pop('last_save_stats', None)



    async def handle_logfile_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор файла логов"""
        query = update.callback_query
        await query.answer()
        
        # Поддерживаем префиксы logfile_, logview_file_, logview_text_
        filename = re.sub(r'^(?:logfile_|logview_file_|logview_text_)', '', query.data)
        log_path = os.path.join(os.path.dirname(__file__), 'logs', filename)
        
        if not os.path.exists(log_path):
            await query.edit_message_text(f"Файл {filename} не найден")
            return
        
        # Создаем клавиатуру с вариантами просмотра
        keyboard = [
            [
                InlineKeyboardButton(text=f"Последние {self.log_lines_to_show} строк",callback_data=f'logview_text_{filename}'),
                InlineKeyboardButton("Скачать файл", callback_data=f'logview_file_{filename}')
            ],
            [InlineKeyboardButton("Назад к логам", callback_data='view_logs')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_text(
                text=f"Выбран файл: {filename}\nКак вы хотите просмотреть логи?",
                reply_markup=reply_markup
            )
        except telegram.error.BadRequest as e:
            if "not modified" in str(e):
                logger.debug("Сообщение не изменилось, пропускаем ошибку")
            else:
                logger.error(f"Ошибка при изменении сообщения: {e}")


    async def handle_log_view_option(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Обрабатывает нажатие кнопок:
        — logview_text_<filename>  — последние строки текста;
        — logview_file_<filename>  — скачивание всего файла.
        Меню с кнопками всегда удаляется перед отправкой.
        """
        query = update.callback_query
        await query.answer()

        # 1) Разбор callback_data: action и имя файла
        data = query.data.removeprefix('logview_')
        try:
            action, filename = data.split('_', 1)
        except ValueError:
            logger.error(f"Некорректные данные callback: {query.data}")
            await query.edit_message_text("Ошибка: неверная кнопка.")
            return

        # 2) Путь до лога и его размер
        log_path = os.path.join(os.path.dirname(__file__), 'logs', filename)
        if not os.path.exists(log_path):
            await query.edit_message_text(f"Файл `{filename}` не найден.", parse_mode='Markdown')
            return
        file_size = os.path.getsize(log_path)

        # 3) Удаляем исходное меню
        try:
            await query.message.delete()
        except Exception as e:
            logger.debug(f"Не удалось удалить меню: {e}")

        # 4) Если текстовый вывод
        if action == 'text':
            # Ограничение на размер для просмотра
            if file_size > 5 * 1024 * 1024:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Файл лога слишком большой (>5 MB) для просмотра. Пожалуйста, скачайте его целиком."
                )
                return

            # Чтение последних строк
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()[-self.log_lines_to_show:]
            except Exception as e:
                logger.error(f"Не удалось прочитать файл {filename}: {e}")
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"Ошибка чтения файла `{filename}`."
                )
                return

            content = ''.join(lines)
            content = sanitize_log_content(content)

            # Разбиваем на куски по 4000 символов
            for part in (content[i:i+4000] for i in range(0, len(content), 4000)):
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"Последние {self.log_lines_to_show} строк из `{filename}`:\n<pre>{part}</pre>",
                        parse_mode='HTML'
                    )
                except Exception:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"Последние {self.log_lines_to_show} строк из {filename}:\n{part}"
                    )
                await asyncio.sleep(0.3)
            return

        # 5) Если скачивание файла
        if action == 'file':
            try:
                with open(log_path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        filename=filename,
                        caption=f"Полный лог файл: {filename}"
                    )
            except Exception as e:
                logger.error(f"Не удалось отправить файл {filename}: {e}")
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"Ошибка отправки файла `{filename}`."
                )
            return

        # 6) Неизвестное действие
        logger.error(f"Неизвестное действие {action} в callback_data")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Ошибка: неизвестное действие."
        )


    # Перезагрузка бота
    @admin_only
    async def restart_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Перезапускает бота"""
        try:
            # Получаем query из update
            query = update.callback_query if hasattr(update, 'callback_query') else None
            
            if query:
                try:
                    await query.answer()
                    await query.edit_message_text(text="Перезапуск бота...")
                except telegram.error.BadRequest as e:
                    logger.warning(f"Не удалось изменить сообщение: {e}")
            else:
                if update.message:
                    await update.message.reply_text("Перезапуск бота...")
            
            # Планируем перезапуск через 1 секунду
            asyncio.create_task(self.delayed_restart())
            
        except Exception as e:
            logger.error(f"Ошибка при перезагрузке: {e}")
            if query:
                try:
                    await query.edit_message_text(text=f"Ошибка при перезагрузке: {e}")
                except telegram.error.BadRequest:
                    pass

    async def delayed_restart(self):
        """Отложенный перезапуск бота"""
        if self._is_restarting:
            return
        self._is_restarting = True
        
        try:
            logger.info("Начало процесса перезагрузки...")
            if self._in_docker:
                logger.info("Перезагрузка в Docker не поддерживается. Используйте 'docker restart'.")
                return

            # Останавливаем приложение
            if self.application.updater and self.application.updater.running:
                logger.info("Останавливаем updater...")
                await self.application.updater.stop()
                await asyncio.sleep(1)
                
            if self.application.running:
                logger.info("Останавливаем application...")
                await self.application.stop()
                await asyncio.sleep(1)
                
                logger.info("Завершаем работу application...")
                await self.application.shutdown()
                await asyncio.sleep(1)
            
            # Запускаем новый процесс (только вне Docker)
            TOKEN = TELEGRAM_BOT_TOKEN
            if not TOKEN:
                logger.error("Не удалось получить токен бота")
                return
            
            logger.info("Запуск нового процесса...")
            os.execv(sys.executable, [sys.executable, __file__])
            
            logger.info("Завершение текущего процесса...")
            await asyncio.sleep(3)  # Даем время для завершения
            os._exit(0)

            await asyncio.wait_for(self.application.shutdown(), timeout=10)
            
        except Exception as e:
            logger.error(f"Критическая ошибка при перезагрузке: {e}")
            os._exit(1)

        finally:
            self._is_restarting = False


    async def shutdown(self):
        """Останавливает бота и освобождает ресурсы."""
        try:
            logger.info("Начало процесса завершения работы...")
            await asyncio.sleep(1)  # Даем время для завершения операций
            
            if self.application.updater and self.application.updater.running:
                logger.info("Останавливаем updater...")
                await self.application.updater.stop()
                await asyncio.sleep(1)
                
            if self.application.running:
                logger.info("Останавливаем application...")
                await self.application.stop()
                await asyncio.sleep(1)
                
                logger.info("Завершаем работу application...")
                await self.application.shutdown()
                await asyncio.sleep(1)
            
            logger.info("Все задачи завершены.")
        
        except Exception as e:
            logger.error(f"Ошибка при завершении работы: {e}")
            raise

    def run(self):
        """Запускает бота и инициализирует его работу."""
        if self._is_running:
            logger.warning("Бот уже запущен, пропускаем повторный запуск")
            return
            
        self._is_running = True
        
        if self._in_docker:
            logger.info("Запуск бота в Docker-контейнере")
        else:
            logger.info("Запуск бота")
        
        try:
            logger.debug("!!!!!!!!!!!!!!!!! RUN_POLLING СТАРТУЕТ !!!!!!!!!!!!!!!!!") # Отладочный лог
            self.application.run_polling(
                poll_interval=2.0,
                timeout=self.request_timeout,
                # close_loop=False, # Это по умолчанию True, можно попробовать убрать или оставить
                stop_signals=None # Убедитесь, что никакие сигналы не прерывают его случайно
            )
        except Exception as e:
            logger.error(f"Ошибка при работе бота: {e}", exc_info=True) # Добавлено exc_info
           
def docker_healthcheck():
    """Проверяет состояние бота для Docker healthcheck."""
    try:
        # Можно добавить реальные проверки работоспособности
        return True
    except Exception:
        return False

if __name__ == '__main__':
    # Проверка на дублирующийся запуск уже добавлена в начале файла
    
    # Получаем токен из переменной окружения
    TOKEN = TELEGRAM_BOT_TOKEN
    if not TOKEN:
        print("Необходимо установить переменную окружения TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    
    try:
        bot = TransactionProcessorBot(TOKEN)
        # logger.info("Запуск бота...")
        bot.run()
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
        sys.exit(1)