__version__ = "3.6.1"

import os
import logging
from logging.handlers import TimedRotatingFileHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters
)
from io import BytesIO
import pandas as pd
from typing import Dict
from tempfile import NamedTemporaryFile
import asyncio
import time
import yaml
import socket
import sys
import subprocess
import shlex
import telegram
import re
import psycopg2
from database import Database
from datetime import datetime, timedelta
from telegram_bot_calendar import DetailedTelegramCalendar, LSTEP
from telegram.ext.filters import BaseFilter
import inspect

# Настройка логирования
def setup_logging():
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    date_format = '%d-%m-%Y %H:%M:%S' #%z'
    
    # Логи в консоль
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))
    
    # Логи в файл (если нужно)
    try:
        if not os.path.exists('logs'):
            os.makedirs('logs')
    except OSError as e:
        logger.error(f"Ошибка при создании директории для логов: {e}")

    file_handler = TimedRotatingFileHandler(
        'logs/bot.log',
        when='midnight',
        backupCount=5,
        encoding='utf-8'
    )
    
    file_handler.suffix = "%Y-%m-%d_bot.log"
    file_handler.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}_bot\.log$")
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
       
    # Основной логгер
    logger = logging.getLogger()

    # # Логирование изменений в БД
    # edit_handler = TimedRotatingFileHandler(
    #     'logs/edits.log', 
    #     when='midnight', 
    #     backupCount=30,
    #     encoding='utf-8')
    
    # edit_handler.setFormatter(logging.Formatter(log_format, date_format))
    # logger.addHandler(edit_handler)

    # Читаем уровень логирования из переменной окружения, по умолчанию INFO
    log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
    log_level_mapping = {
        'CRITICAL': logging.CRITICAL,
        'ERROR': logging.ERROR,
        'WARNING': logging.WARNING,
        'INFO': logging.INFO,
        'DEBUG': logging.DEBUG,
        'NOTSET': logging.NOTSET
    }
    log_level = log_level_mapping.get(log_level_str, logging.INFO) # Устанавливаем INFO по умолчанию, если значение неверное
    logger.setLevel(log_level)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    # Дополнительные настройки для конкретных логгеров
    logging.getLogger('httpx').setLevel(logging.WARNING)  # Уменьшаем логи httpx
    logging.getLogger('telegram').setLevel(logging.INFO)  # Настраиваем логи telegram
    # logging.getLogger("telegram").setLevel(logging.DEBUG) # Добавлено

    # Логируем установленный уровень
    logger.info(f"Уровень логирования для BOT установлен в: {logging.getLevelName(logger.level)}")

    return logger

logger = setup_logging()

# Добавляем загрузку списка админов
def load_admins():
    """Загружает список админов из переменной окружения"""
    admins = os.getenv('ADMINS', '').split(',')
    return set(map(int, filter(None, admins)))  # Преобразуем в set[int]

ALLOWED_USERS = load_admins()

def load_general_settings(config_path: str = None) -> Dict:
    """Загружает общие настройки из YAML-файла"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'settings.yaml')
    
    try:
        with open(config_path, 'r', encoding='utf-8') as file:
            settings = yaml.safe_load(file)
            if settings is None:  # Если файл пустой
                logger.warning(f"Файл настроек {config_path} пуст. Используются значения по умолчанию.")
                return {'LOG_LEVEL': 'INFO'}  # Возвращаем настройки по умолчанию
            return settings
    except FileNotFoundError:
        logger.warning(f"Файл настроек {config_path} не найден. Используются значения по умолчанию.")
        return {'LOG_LEVEL': 'INFO'}  # Настройки по умолчанию
    except Exception as e:
        logger.error(f"Ошибка загрузки файла настроек {config_path}: {e}", exc_info=True)
        return {'LOG_LEVEL': 'INFO'}  # Настройки по умолчанию
    
# Декоратор для проверки доступа
def admin_only(func):
    async def wrapper(*args, **kwargs): # Сам wrapper должен быть async
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
    print("Бот уже запущен! Завершаю работу.")
    sys.exit(1)

# Импорт ваших скриптов
from extract_transactions_pdf1 import process_pdf as extract_pdf1
from extract_transactions_pdf2 import process_csv as extract_pdf2
from classify_transactions_pdf import (classify_transactions, add_pattern_to_category)

def load_timeouts(config_path: str = None) -> Dict[str, int]:
    """Загружает конфигурацию таймаутов из YAML-файла"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'timeouts.yaml')
    with open(config_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)['timeouts']

def parse_user_settings(message_text: str) -> dict:
    settings = {}
    if not message_text:
        return settings
    
    # lines = [line.strip() for line in message_text.split('\n') if line.strip()]
    lines = [line.strip() for line in message_text.split('\n')[:100] if line and len(line) < 100]
    pattern = re.compile(r"^(.+?)\s*:\s*(\+?)\s*(.*)$", re.IGNORECASE)
    
    for line in lines:
        match = pattern.match(line)
        if match:
            key = match.group(1).strip().lower()
            operator = match.group(2).strip()
            value = match.group(3).strip()
            
            # Приводим названия полей к стандартному виду
            if key in ['контрагент', 'контрагента']:
                key = 'Контрагент'
            elif key in ['чек', 'чек #', 'чек№']:
                key = 'Чек #'
            elif key in ['описание', 'описании']:
                key = 'Описание'
            elif key in ['наличность', 'нал', 'наличка']:
                key = 'Наличность'
            elif key in ['класс']:
                key = 'Класс'
                
            settings[key] = {
                'operator': operator,
                'value': value
            }
    
    return settings

class TransactionProcessorBot:
    def __init__(self, token: str):
        self._active_tasks = 0
        self._max_active_tasks = 3  # Максимум 3 одновременно обрабатываемых файла

        self._is_running = False
        self._is_restarting = False  # Флаг перезагрузки  
        self._in_docker = os.getenv('DOCKER_MODE') is not None

        # Обработчики для редактирования конфига
        self.config_handlers = [
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_config_edit),
            MessageHandler(filters.Document.ALL, self.handle_config_upload)
        ]

        # Логируем ID созданных хендлеров для отладки
        for i, handler_obj in enumerate(self.config_handlers):
            handler_name = "handle_config_edit" if i == 0 else "handle_config_upload"
            logger.debug(f"__init__: Создан config_handler ({handler_name}) с ID: {id(handler_obj)}")

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

        self.application.add_handler(CallbackQueryHandler(
            self.config_selection_callback,
            pattern=re.compile(r'^(view_categories|view_special|view_pdf_patterns|view_timeouts|view_all|back_to_main)$')
        )
        # , group=-1
        )

    def setup_handlers(self):
        # Основные команды (только для админов)
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("config", self.show_config_menu))
        self.application.add_handler(CommandHandler("restart", self.restart_bot))
        self.application.add_handler(CommandHandler("add_pattern", self.add_pattern))
        self.application.add_handler(CommandHandler("add_settings", self.add_settings))
        self.application.add_handler(CommandHandler("settings", self.show_settings))
        self.application.add_handler(CommandHandler("export", self.export_start))
        self.application.add_handler(CommandHandler("edit", self.start_edit))
        self.application.add_handler(CommandHandler("reset", self.reset_settings))

        self.application.add_handler(CallbackQueryHandler(self.handle_calendar_callback, pattern=r"^cbcal_"),group=0)
        self.application.add_handler(CallbackQueryHandler(self.generate_report, pattern='^generate_report'))
        self.application.add_handler(CallbackQueryHandler(self.show_filters_menu, pattern='^back_to_filters'))
        self.application.add_handler(CallbackQueryHandler(self.handle_filter_callback, pattern='^(cat|type|source|class)_'))
        self.application.add_handler(CallbackQueryHandler(self.set_start_date, pattern='^set_start_date$'))
        self.application.add_handler(CallbackQueryHandler(self.set_end_date, pattern='^set_end_date$'))     
        self.application.add_handler(CallbackQueryHandler(self.set_category, pattern='^set_category$'))
        self.application.add_handler(CallbackQueryHandler(self.set_type, pattern='^set_type$'))
        self.application.add_handler(CallbackQueryHandler(self.set_cash_source, pattern='^set_cash_source'))
        self.application.add_handler(CallbackQueryHandler(self.set_counterparty, pattern='^set_counterparty'))
        self.application.add_handler(CallbackQueryHandler(self.set_check_num, pattern='^set_check_num'))
        self.application.add_handler(CallbackQueryHandler(self.set_class, pattern='^set_class'))
        self.application.add_handler(CallbackQueryHandler(self.set_import_id, pattern='^set_import_id$'))
        self.application.add_handler(CallbackQueryHandler(self.handle_import_id_callback, pattern='^import_id_'))
        self.application.add_handler(CallbackQueryHandler(self.cancel_export, pattern='^cancel_export$'))
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

        self.application.add_handler(MessageHandler(filters.Document.PDF, self.handle_document))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'^(\d+[\s,-]*)+\d+$'),self.process_ids_input)) #, group=1)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input)) # Добавить перед apply_edits
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_config_edit),group=2)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,self.apply_edits))
        
        # Обработчики callback-запросов (только для админов)
        self.application.add_handler(
            CallbackQueryHandler(
                self.main_menu_callback,
                pattern='^(view_config|edit_config|restart|view_logs)$'
            )
            # ,
            # group=-1
        )
        
        self.application.add_handler(
            CallbackQueryHandler(
                self.edit_menu_callback,
                pattern='^(edit_categories|edit_special|edit_pdf_patterns|edit_timeouts|cancel)$'
            )
            # ,
            # group=-1
        )
        
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
        
        # Добавляем обработчик выбора файла логов
        self.application.add_handler(
            CallbackQueryHandler(
                self.handle_logfile_selection,
                pattern='^logfile_'
            )
        )
        
        self.application.add_handler(
            CallbackQueryHandler(
                self.handle_log_view_option,
                pattern='^logview_'
            )
        )

        self.application.add_handler(
            CallbackQueryHandler(
                self.handle_save_confirmation,
                pattern='^save_(yes|no)$'
            )
        )

        self.application.add_handler(
            CallbackQueryHandler(
                self.handle_duplicates_decision,
                pattern='^(update_duplicates|skip_duplicates)$'
            )
        )
        
        self.application.add_handler(CallbackQueryHandler(self.handle_edit_filter_proceed, pattern='^edit_filter_proceed_to_fields$'))

        self.application.add_handler(CommandHandler("cancel", self.cancel_operation))

        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)

    def get_default_filters(self) -> dict:
        return {
            'start_date': datetime.now().replace(day=1).strftime('%d.%m.%Y'),
            'end_date': datetime.now().strftime('%d.%m.%Y'),
            'category': 'Все',
            'transaction_type': 'Все',
            'cash_source': 'Все',
            'counterparty': 'Все',
            'check_num': 'Все',
            'transaction_class': 'Все'
        }

    async def handle_edit_filter_proceed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        # Например, получение ID по фильтрам (этот код нужно адаптировать из apply_edits или get_transactions)
        db = Database()
        try:
            filters_for_db = context.user_data['edit_mode']['edit_filters']
            db_parsed_filters = {k: v for k, v in filters_for_db.items() if v != 'Все' and k not in ['start_date', 'end_date']}

            start_date_dt = datetime.strptime(filters_for_db['start_date'], '%d.%m.%Y')
            end_date_dt = datetime.strptime(filters_for_db['end_date'], '%d.%m.%Y')

            df_transactions = db.get_transactions(
                user_id=update.effective_user.id,
                start_date=start_date_dt,
                end_date=end_date_dt,
                filters=db_parsed_filters
            )
            ids_from_filter = df_transactions['id'].tolist()

            if not ids_from_filter:
                await query.edit_message_text("⚠ По выбранным фильтрам не найдено записей для редактирования.")
                # Можно предложить вернуться к фильтрам или отменить
                return 

            context.user_data['edit_mode']['ids'] = ids_from_filter
            logger.info(f"Редактирование по фильтру: найдено {len(ids_from_filter)} ID. IDs: {ids_from_filter[:10]}...") # Лог первых 10 ID

        except Exception as e:
            logger.error(f"Ошибка получения ID по фильтрам: {e}", exc_info=True)
            await query.edit_message_text("⚠️ Ошибка при применении фильтров")
            context.user_data.pop('edit_mode', None)
            return
        finally:
            db.close()

        await query.edit_message_text(f"ℹ️ Найдено {len(ids_from_filter)} записей для редактирования.")
        await self._select_fields_to_edit(update, context) # Переход к выбору поля для редактирования

    async def show_filters_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE, edit_mode: bool = False):
        user_id = update.effective_user.id
        logger.debug(f"show_filters_menu вызвана для user_id: {user_id}, edit_mode: {edit_mode}")

        # Получаем текущие фильтры или создаем новый словарь, если их нет
        current_filters = context.user_data.get('export_filters', {})

        default_filters = self.get_default_filters()
            # Значения по умолчанию для всех полей фильтра

        # Объединяем: приоритет у current_filters, недостающие берутся из default_filters
        filters = {**default_filters, **current_filters}
        
        # Сохраняем обновленные фильтры
        context.user_data['export_filters'] = filters

        if edit_mode:
            if 'edit_mode' not in context.user_data:
                context.user_data['edit_mode'] = {}
            # Используем default_filters как значение по умолчанию для setdefault
            filters = context.user_data['edit_mode'].setdefault('edit_filters', default_filters.copy()) # Используем .copy() чтобы избежать изменения оригинала
        else:
            # Используем default_filters как значение по умолчанию для setdefault
            filters = context.user_data.setdefault('export_filters', default_filters.copy()) # Используем .copy()

        logger.debug(f"show_filters_menu: Используемые фильтры для генерации меню: {filters}")

        # Формируем клавиатуру
        if edit_mode:
            keyboard = [
                [InlineKeyboardButton(f"📅 Дата начала: {filters['start_date']}", callback_data='set_start_date')],
                [InlineKeyboardButton(f"📅 Дата окончания: {filters['end_date']}", callback_data='set_end_date')],
                [InlineKeyboardButton(f"📦 ID импорта: {filters.get('import_id', 'Все')}", callback_data='set_import_id')],
                [InlineKeyboardButton(f"🏷 Категория: {filters['category']}", callback_data='set_category')],
                [InlineKeyboardButton(f"🔀 Тип: {filters['transaction_type']}", callback_data='set_type')],
                [InlineKeyboardButton(f"💳 Наличность: {filters['cash_source']}", callback_data='set_cash_source')],
                [InlineKeyboardButton(f"👥 Контрагент: {filters['counterparty']}", callback_data='set_counterparty')],
                [InlineKeyboardButton(f"🧾 Чек: {filters['check_num']}", callback_data='set_check_num')],
                [InlineKeyboardButton(f"📊 Класс: {filters['transaction_class']}", callback_data='set_class')],
                [InlineKeyboardButton("➡️ К выбору полей", callback_data='edit_filter_proceed_to_fields')],
                [InlineKeyboardButton("✖️ Отмена", callback_data='cancel_edit')]
            ]
            message_text = "⚙ Настройте фильтры для выбора записей для редактирования:"
        else:
            keyboard = [
                [InlineKeyboardButton(f"📅 Дата начала: {filters['start_date']}", callback_data='set_start_date')],
                [InlineKeyboardButton(f"📅 Дата окончания: {filters['end_date']}", callback_data='set_end_date')],
                [InlineKeyboardButton(f"📦 ID импорта: {filters.get('import_id', 'Все')}", callback_data='set_import_id')],
                [InlineKeyboardButton(f"🏷 Категория: {filters['category']}", callback_data='set_category')],
                [InlineKeyboardButton(f"🔀 Тип: {filters['transaction_type']}", callback_data='set_type')],
                [InlineKeyboardButton(f"💳 Наличность: {filters['cash_source']}", callback_data='set_cash_source')],
                [InlineKeyboardButton(f"👥 Контрагент: {filters['counterparty']}", callback_data='set_counterparty')],
                [InlineKeyboardButton(f"🧾 Чек: {filters['check_num']}", callback_data='set_check_num')],
                [InlineKeyboardButton(f"📊 Класс: {filters['transaction_class']}", callback_data='set_class')],
                [InlineKeyboardButton("✅ Сформировать отчет", callback_data='generate_report')],
                [InlineKeyboardButton("✖️ Отмена", callback_data='cancel_export')]
            ]
            message_text = "⚙ Настройте параметры отчета:"

        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.callback_query and update.callback_query.message:
            try:
                await update.callback_query.edit_message_text(
                    text=message_text,
                    reply_markup=reply_markup
                )
            except telegram.error.BadRequest as e:
                logger.error(f"Ошибка при редактировании сообщения: {e}")
        else:
            await update.message.reply_text(
                text=message_text,
                reply_markup=reply_markup
            )

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
                default_filters = self.get_default_filters()
                context.user_data['edit_mode']['edit_filters'] = default_filters.copy()
            context.user_data['edit_mode']['type'] = 'edit_by_filter'
            await self.show_filters_menu(update, context, edit_mode=True)

        if query.data == 'edit_by_id':
            context.user_data['edit_mode'] = {'type': 'edit_by_id', 'awaiting_ids': True} # Устанавливаем флаг
            await query.edit_message_text(
                "📝 Введите ID записей через запятую (например: 15, 28, 42):\n"
                "Или диапазон через дефис (15-28)"
            )
        else:  # edit_by_filter
            await self.show_filters_menu(update, context, edit_mode=True)

    async def process_ids_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает ввод ID записей"""
        edit_mode_data = context.user_data.get('edit_mode', {})
        if not (edit_mode_data.get('type') == 'edit_by_id' and edit_mode_data.get('awaiting_ids')):
            # Это не тот случай, когда мы ждем ID, передаем дальше или игнорируем
            return 
        logger.debug(f"Получены ID для редактирования: {update.message.text}")        
        
        # db = Database() # Инициализация DB должна быть внутри try, или после парсинга ID,
                        # чтобы не делать запрос к БД, если ID невалидны
        try:
            ids_input = update.message.text.strip()
            ids = []

            if '-' in ids_input:  # Обработка диапазона
                try:
                    start, end = map(int, ids_input.split('-'))
                    ids = list(range(start, end + 1))
                except ValueError:
                    await update.message.reply_text("⚠️ Неверный формат диапазона. Пример: 10-20")
                    return
            else:  # Обработка списка
                try:
                    ids = [int(id_str.strip()) for id_str in ids_input.split(',')] # Исправлено: id -> id_str
                except ValueError:
                    await update.message.reply_text("⚠️ Неверный формат ID. Пример: 15, 28, 42")
                    return

            # ---> ПЕРЕМЕСТИТЬ ПРОВЕРКУ ID СЮДА <---
            db = Database() # Инициализация DB здесь, после успешного парсинга ID
            try:
                existing_ids_from_db = db.check_existing_ids(ids) # Переименовал, чтобы не путать с ids
                if len(existing_ids_from_db) != len(ids):
                    missing = set(ids) - set(existing_ids_from_db)

                    ids = [id_val for id_val in ids if id_val in existing_ids_from_db]
                    if not ids:
                        await update.message.reply_text("⚠️ Нет действительных ID для редактирования.")
                        context.user_data.pop('edit_mode', None)
                        return

                    await update.message.reply_text(f"⚠ ID {', '.join(map(str, missing))} не найдены в базе. Будут обработаны только существующие.")
                    ids = [id_val for id_val in ids if id_val in existing_ids_from_db] # Обновляем ids, оставляя только существующие
                    if not ids: # Если после фильтрации список ids пуст (на всякий случай)
                        await update.message.reply_text("⚠️ Ошибка: нет действительных ID для редактирования после проверки.")
                        context.user_data.pop('edit_mode', None) # Очищаем состояние редактирования
                        return
            finally:
                db.close()
            # ---> КОНЕЦ ПЕРЕМЕЩЕННОГО БЛОКА <---

            context.user_data['edit_mode']['ids'] = ids # Сохраняем только существующие ID
            # context.user_data['edit_mode'].pop('awaiting_ids', None) # Удаляем флаг
            context.user_data['edit_mode'] = {'type': 'edit_by_id','ids': ids}         
            await self._select_fields_to_edit(update, context)

        except psycopg2.errors.UndefinedTable as db_err: # Перехват ошибки отсутствия таблицы
            logger.error(f"Ошибка базы данных при обработке ID: {db_err}", exc_info=True)
            await update.message.reply_text("❌ Критическая ошибка конфигурации базы данных. Обратитесь к администратору.")
            # Важно не продолжать, если БД не готова
            return
        except Exception as e:
            logger.error(f"Ошибка обработки ID: {e}", exc_info=True)
            await update.message.reply_text("❌ Ошибка обработки. Проверьте формат ввода")

    async def _select_fields_to_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню выбора полей для редактирования"""
        logger.debug(f"Вызов _select_fields_to_edit для user_id: {update.effective_user.id}")

        keyboard = [
            [InlineKeyboardButton("🏷 Категория", callback_data='edit_field_category')],
            [InlineKeyboardButton("📝 Описание", callback_data='edit_field_description')],
            [InlineKeyboardButton("👥 Контрагент", callback_data='edit_field_counterparty')],
            [InlineKeyboardButton("🧾 Чек #", callback_data='edit_field_check_num')],
            [InlineKeyboardButton("💳 Наличность", callback_data='edit_field_cash_source')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_edit_choice')],
            [InlineKeyboardButton("✖️ Отмена", callback_data='cancel_edit')]
        ]
        
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.edit_message_text(
                "✏️ Выберите поле для редактирования:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                "✏️ Выберите поле для редактирования:",
                reply_markup=InlineKeyboardMarkup(keyboard)
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
        """Применяет изменения к базе данных"""
        edit_data = context.user_data.get('edit_mode', {})
        new_value = update.message.text
        
        if not edit_data or 'field' not in edit_data or 'mode' not in edit_data: # Добавил проверку 'mode'
            # Эта проверка может быть избыточной, если первая проверка выше работает
            logger.warning("apply_edits: edit_data неполный. edit_data: %s", edit_data)
            # Не отправляем сообщение об ошибке здесь, чтобы не дублировать, если проблема в другом
            return

        db = Database()
        try:
            if edit_data['type'] == 'edit_by_filter':
                ids = edit_data.get('ids', [])
                if not ids: # Дополнительная проверка
                    logger.warning("apply_edits (edit_by_filter): 'ids' не найдены в edit_data.")
                    # Попытка получить ID заново, если их нет (менее предпочтительно)
                    edit_filters_data = context.user_data.get('edit_mode', {}).get('edit_filters')
                    if not edit_filters_data:
                        await update.message.reply_text("⚠ Ошибка: Фильтры для редактирования не найдены.")
                        context.user_data.pop('edit_mode', None)
                        return
                    df = db.get_transactions(
                        user_id=update.effective_user.id,
                        start_date=datetime.strptime(edit_filters_data['start_date'], '%d.%m.%Y'),
                        end_date=datetime.strptime(edit_filters_data['end_date'], '%d.%m.%Y'),
                        filters={k: v for k, v in edit_filters_data.items() if v != 'Все'}
                    )

                    ids = df['id'].tolist()

                    if not ids:
                        await update.message.reply_text("⚠ По выбранным фильтрам не найдено записей для редактирования.")
                        context.user_data.pop('edit_mode', None)
                        return

                # Получаем ID по фильтрам
                # filters = context.user_data.get('export_filters', {})
            else:
                ids = edit_data.get('ids', [])

            # Формируем обновления
            updates = {
                edit_data['field']: (new_value, edit_data['mode'])
            }
            
            # Выполняем обновление
            updated_ids = db.update_transactions(
                user_id=update.effective_user.id,
                ids=ids,
                updates=updates
            )
            
            # Логируем действие
            logger.info(
                f"User {update.effective_user.id} edited {len(updated_ids)} records. "
                f"IDs: {updated_ids}. Changes: {updates}"
            )
            
            await update.message.reply_text(
                f"✅ Успешно обновлено {len(updated_ids)} записей!\n"
                f"Измененное поле: {edit_data['field']}\n"
                f"Новое значение: {new_value}"
            )

        except Exception as e:
            logger.error(f"Ошибка при редактировании: {e}", exc_info=True)
            await update.message.reply_text("❌ Ошибка при обновлении. Проверьте подключение к базе данных или обратитесь к администратору.")

        finally:
            db.close()
            logger.debug("Очистка состояния edit_mode после apply_edits")
            context.user_data.pop('edit_mode', None) # <-- Важная строка, очищает состояние
            # Чтобы точно предотвратить дальнейшую обработку этого текстового сообщения
            # другими общими текстовыми хендлерами, которые могут среагировать,
            # можно попробовать установить флаг, но обычно очистки user_data достаточно
            # context.user_data['message_handled_by_apply_edits'] = True 
            # И тогда в handle_config_edit и handle_text_input проверять этот флаг.
            # Но проще всего, если группы хендлеров и их специфичность настроены правильно.

    @admin_only
    async def export_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начало процесса экспорта с предзаполненными фильтрами"""
        user_data = context.user_data
        user_data['export_filters'] = {
            'start_date': datetime.now().replace(day=1).strftime('%d.%m.%Y'),
            'end_date': datetime.now().strftime('%d.%m.%Y'),
            'category': 'Все',
            'transaction_type': 'Все',
            'cash_source': 'Все',
            'counterparty': 'Все',
            'check_num': 'Все',
            'transaction_class': 'Все'
            }
        
        await self.show_filters_menu(update, context)


    async def set_start_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.debug("Создание календаря для выбора даты начала")
        logger.debug("Вызов set_start_date для user_id=%s", update.effective_user.id)
        query = update.callback_query
        await query.answer()
        calendar, step = DetailedTelegramCalendar(locale='ru').build()
        await query.edit_message_text(
            text=f"📅 Выберите дату начала ({LSTEP[step]}):",  # Используем LSTEP для отображения текущего шага (год/месяц/день)
            reply_markup=calendar
        )
        context.user_data["calendar_context"] = "start_date" 


    async def set_end_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.debug("Вызов set_end_date для user_id=%s", update.effective_user.id)
        query = update.callback_query
        await query.answer()
        calendar, step = DetailedTelegramCalendar(locale='ru').build()
        await query.edit_message_text(
            text=f"📅 Выберите дату окончания ({LSTEP[step]}):", # Используем LSTEP для отображения текущего шага
            reply_markup=calendar
        )
        context.user_data["calendar_context"] = "end_date"


    async def handle_calendar_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                    context.user_data['edit_mode']['edit_filters'] = self.get_default_filters().copy()
                
                target_filters_dict = context.user_data['edit_mode']['edit_filters']
                log_source_for_filters = "edit_mode['edit_filters']"
            else:
                if 'export_filters' not in context.user_data:
                    context.user_data['export_filters'] = self.get_default_filters().copy()
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

            await self.show_filters_menu(update, context, edit_mode=is_editing_filters)

    async def set_category(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Обработчик set_category вызван для user_id=%s", update.effective_user.id)
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        db = Database()
        try:
            categories = ['Все'] + db.get_unique_values("category", user_id)
            logger.info("Полученные категории: %s", categories)

            if not categories or categories == ['Все']:
                try:
                    await query.edit_message_text(
                        "Категории не найдены. Убедитесь, что в базе данных есть транзакции с категориями."
                    )
                except telegram.error.BadRequest as e:
                    logger.warning(f"Ошибка Telegram API: {e}")
                    await query.message.reply_text(
                        "Категории не найдены. Убедитесь, что в базе данных есть транзакции с категориями."
                    )
                return

            keyboard = []
            for cat in categories:
                safe_cat = cat.replace(" ", "_").replace("'", "").replace('"', "")[:50]
                keyboard.append([InlineKeyboardButton(cat, callback_data=f"cat_{safe_cat}")])
            keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data='back_to_filters')])

            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await query.edit_message_text("Выберите категорию:", reply_markup=reply_markup)
            except telegram.error.BadRequest as e:
                logger.warning(f"Ошибка Telegram API при обновлении сообщения: {e}")
                await query.message.reply_text("Выберите категорию:", reply_markup=reply_markup)

        except Exception as e:
            logger.error("Ошибка в set_category: %s", e, exc_info=True)
            try:
                await query.edit_message_text("❌ Ошибка при загрузке категорий. Попробуйте позже.")
            except telegram.error.BadRequest as e:
                logger.warning(f"Ошибка Telegram API: {e}")
                await query.message.reply_text("❌ Ошибка при загрузке категорий. Попробуйте позже.")
        finally:
            db.close()


    async def set_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Обработчик set_type вызван")
        query = update.callback_query
        await query.answer()

        # Получение типов из базы
        user_id = query.from_user.id
        db = Database()
        types = ['Все'] + db.get_unique_values("transaction_type", user_id)
        db.close()

        keyboard = [
            [InlineKeyboardButton(type, callback_data=f"type_{type}")]
            for type in types
        ]
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data='back_to_filters')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выберите тип транзакции:", reply_markup=reply_markup)


    async def set_cash_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню выбора Наличности"""
        logger.info("Обработчик set_cash_source вызван")
        query = update.callback_query
        await query.answer()
        
        db = Database()
        sources = ['Все'] + db.get_unique_values('cash_source', query.from_user.id)
        db.close()
        
        keyboard = [
            [InlineKeyboardButton(src, callback_data=f'source_{src}') 
            for src in sources[i:i+2]]
            for i in range(0, len(sources), 2)
        ]
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data='back_to_filters')])
        
        await query.edit_message_text(
            "Выберите источник средств:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
      
    async def set_counterparty(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню выбора Контрагента"""
        logger.info("Обработчик set_counterparty вызван")
        query = update.callback_query
        await query.answer()
        
        await query.edit_message_text(
            "Введите имя контрагента или часть названия:"
        )
        context.user_data['awaiting_input'] = 'counterparty'


    async def set_check_num(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню выбора Чека"""
        logger.info("Обработчик set_check_num вызван")
        query = update.callback_query
        await query.answer()
        
        await query.edit_message_text(
            "Введите номер чека или часть номера:"
        )
        context.user_data['awaiting_input'] = 'check_num'


    async def set_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню выбора Класса"""
        logger.info("Обработчик set_class вызван")
        query = update.callback_query
        await query.answer()
        
        db = Database()
        classes = ['Все'] + db.get_unique_values('transaction_class', query.from_user.id)
        db.close()
        
        keyboard = [
            [InlineKeyboardButton(cls, callback_data=f'class_{cls}') 
            for cls in classes[i:i+3]]
            for i in range(0, len(classes), 3)
        ]
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data='back_to_filters')])
        
        await query.edit_message_text(
            "Выберите класс транзакции:",
            reply_markup=InlineKeyboardMarkup(keyboard))

    async def set_import_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Меню выбора Import ID"""
            logger.info("Обработчик set_import_id вызван")
            query = update.callback_query
            await query.answer()

            user_id = query.from_user.id
            db = Database()
            try:
                # Получаем последние N import_id и даты
                last_imports = db.get_last_import_ids(user_id, self.export_last_import_ids_count)
                logger.debug(f"Получены последние {len(last_imports)} import_id для user_id {user_id}")

                keyboard = [[InlineKeyboardButton('Все', callback_data='import_id_Все')]] # Кнопка "Все"
                # Формируем кнопки для каждого import_id
                
                # last_imports теперь будет содержать (import_id, created_at, pdf_type_val)
                for import_id, created_at, pdf_type_val in last_imports: # <--- Добавлен pdf_type_val
                    date_str = created_at.strftime('%d.%m.%Y %H:%M')
                    # Формируем текст кнопки, добавляя pdf_type, если он есть
                    button_text = f"#{import_id} ({date_str}"
                    if pdf_type_val:
                        button_text += f", {pdf_type_val}"
                    button_text += ")"
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=f'import_id_{import_id}')])

                keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data='back_to_filters')])
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    f"Выберите ID импорта (последние {self.export_last_import_ids_count}):",
                    reply_markup=reply_markup
                )
                context.user_data['awaiting_input'] = None # Убедимся, что не ждем текстового ввода

            except Exception as e:
                logger.error(f"Ошибка в set_import_id: {e}", exc_info=True)
                await query.edit_message_text("❌ Ошибка при загрузке ID импортов. Попробуйте позже.")
            finally:
                db.close()

    async def cancel_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data.pop('export_filters', None)
        await query.edit_message_text("ℹ️ Экспорт отменен")


    async def debug_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        # logger.info("Получен callback: %s", query.data)
        logger.debug(f"DEBUG_CALLBACK: Получен callback_data: '{query.data}' от user_id: {query.from_user.id}") # Улучшенный лог
        await query.answer()

    # Обновим обработчик текстового ввода


    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.message.from_user.id
        text = update.message.text

        edit_mode_data = context.user_data.get('edit_mode', {})
        is_in_edit_process = bool(edit_mode_data) 

        logger.info(f"handle_text_input: Обработка текста '{text}' для user_id {user_id}. Режим: {'edit_mode' if is_in_edit_process else 'фильтры'}")

        if not text:
            await update.message.reply_text("Пожалуйста, введите непустое значение")
            return

        if is_in_edit_process and 'field' in edit_mode_data and 'mode' in edit_mode_data:
            # Если мы в процессе редактирования поля и ожидаем значение
            await self.apply_edits(update, context)
            return

        # Логика для фильтров (экспорт или edit_by_filter, когда вводится значение для контрагента/чека)
        # или если мы ожидаем ввод ID
        if context.user_data.get('edit_mode', {}).get('awaiting_ids'):
            # Если мы ожидаем ввод ID, этот текст должен быть обработан process_ids_input
            # Этот return предотвратит дальнейшую обработку этого текста в handle_text_input
            # если process_ids_input не справился или не должен был.
            # Но лучше, если process_ids_input сам решает, что делать.
            # Пока оставим так, чтобы текстовый ввод ID не попадал в логику фильтров ниже.
            # Это предположение, что process_ids_input обработает ID.
            return


        # Получаем default_filters асинхронно ОДИН РАЗ
        default_filters = self.get_default_filters()

        edit_mode_active = edit_mode_data.get('type') == 'edit_by_filter'

        # Определяем, где хранятся фильтры
        if edit_mode_active:
            if 'edit_filters' not in context.user_data['edit_mode']:
                context.user_data['edit_mode']['edit_filters'] = default_filters.copy()
            filters_storage = context.user_data['edit_mode']['edit_filters']
        else: # Это для export_filters
            if 'export_filters' not in context.user_data:
                context.user_data['export_filters'] = default_filters.copy()
            filters_storage = context.user_data['export_filters']

        # Теперь filters_storage точно является словарем
        if not isinstance(filters_storage, dict): # Дополнительная проверка на всякий случай
            logger.error(f"filters_storage не является словарем: {type(filters_storage)}, значение: {filters_storage}")
            await update.message.reply_text("❌ Внутренняя ошибка при обработке фильтров. Обратитесь к администратору.")
            return

        # Обработка ввода для фильтров 'Контрагент' или 'Чек'
        awaiting_input_type = context.user_data.pop('awaiting_input', None) # Получаем и удаляем флаг
        if awaiting_input_type == 'counterparty':
            filters_storage['counterparty'] = text
        elif awaiting_input_type == 'check_num':
            filters_storage['check_num'] = text
        # elif awaiting_input_type: # Если были другие типы ожидаемого ввода
            # filters_storage[awaiting_input_type] = text # Общая логика (не рекомендуется без четкого понимания)
        else:
            # Если мы не ожидали специфического ввода (контрагент/чек),
            # и это не ввод значения для редактирования поля (обработано выше),
            # и это не ввод ID (обработано выше),
            # то это может быть неожиданный текстовый ввод.
            # Можно добавить логику или просто проигнорировать, или ответить пользователю.
            logger.warning(f"Получен неожиданный текстовый ввод: '{text}' от user_id {user_id} при отсутствии awaiting_input.")
            # await update.message.reply_text("Неясно, к чему относится ваш ввод. Пожалуйста, используйте кнопки или команды.")
            # Пока не будем ничего отвечать, чтобы не мешать другим потокам.
            return # Важно, чтобы не вызывался show_filters_menu без надобности

        # После установки значения для контрагента или чека, показываем обновленное меню фильтров
        await self.show_filters_menu(update, context, edit_mode=edit_mode_active)


    async def handle_filter_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data.split('_', 1)  # Разделяем только на первую часть
        filter_type = data[0]
        value = data[1] if len(data) > 1 else ''
        
        edit_mode_active = context.user_data.get('edit_mode') and context.user_data['edit_mode'].get('type') == 'edit_by_filter'

        if edit_mode_active:
            filters_storage = context.user_data['edit_mode'].setdefault('edit_filters', self.get_default_filters())
        else:
            filters_storage = context.user_data.setdefault('export_filters', self.get_default_filters())

        # Для категории ищем оригинальное значение в базе
        if filter_type == 'cat':
            db = Database()
            try:
                categories = db.get_unique_values("category", query.from_user.id)
                # Ищем категорию, соответствующую safe_value
                safe_value = value
                original_value = next((cat for cat in categories if cat.replace(" ", "_").replace("'", "").replace('"', "")[:50] == safe_value), safe_value)
                context.user_data['export_filters']['category'] = original_value
                filters_storage['category'] = original_value
            except Exception as e:
                logger.error("Ошибка при получении категорий: %s", e)
                await query.edit_message_text("❌ Ошибка при выборе категории.")
                return
            finally:
                db.close()
        elif filter_type == 'type':
            filters_storage['transaction_type'] = value
        elif filter_type == 'source':
            filters_storage['cash_source'] = value
        elif filter_type == 'class':
            filters_storage['transaction_class'] = value
        
        await self.show_filters_menu(update, context, edit_mode=edit_mode_active)

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
                filters_storage = context.user_data['edit_mode'].setdefault('edit_filters', self.get_default_filters())
            else:
                filters_storage = context.user_data.setdefault('export_filters', self.get_default_filters())

            # Сохраняем определенное значение import_id
            filters_storage['import_id'] = selected_import_id
            logger.debug(f"handle_import_id_callback: Установлен import_id в фильтрах: {filters_storage['import_id']}")

            # --- ДОБАВЛЕНО: Автоматическая установка даты начала при выборе ID импорта ---
            # Если выбран конкретный ID (не "Все"), устанавливаем давнюю дату начала
            if filters_storage['import_id'] != 'Все':
                from datetime import datetime
                past_start_date = datetime(2000, 1, 1) # Желаемая дата начала (например, 1 января 2000)
                filters_storage['start_date'] = past_start_date.strftime('%d.%m.%Y')
                logger.debug(f"handle_import_id_callback: Дата начала автоматически установлена в {filters_storage['start_date']} после выбора ID импорта.")
            # --- КОНЕЦ ДОБАВЛЕНО ---

            # Возвращаемся к основному меню фильтров
            try:
                await self.show_filters_menu(update, context, edit_mode=edit_mode_active)
            except Exception as e:
                logger.error(f"Ошибка при вызове show_filters_menu: {e}", exc_info=True)
                await update.callback_query.message.reply_text("⚠️ Не удалось обновить меню. Фильтр ID импорта установлен.")
                    
    async def generate_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Генерация и отправка отчета"""
        query = update.callback_query
        await query.answer("Формирую отчет...")
        
        user_id = query.from_user.id
        user_data = context.user_data
        filters = user_data['export_filters']
        logger.debug("Генерация отчета с фильтрами: %s", filters)

        db_filters = {}
        for key in ['category', 'transaction_type', 'cash_source', 'counterparty', 'check_num', 'transaction_class']:
            if filters[key] != 'Все':
                db_filters[key] = filters[key]

        if filters.get('import_id') and filters['import_id'] != 'Все':
            db_filters['import_id'] = filters['import_id']

        filters['start_date'] = datetime.strptime(filters['start_date'], '%d.%m.%Y')
        filters['end_date'] = datetime.strptime(filters['end_date'], '%d.%m.%Y')

        db = Database()
        try:
            df = db.get_transactions(
                user_id=query.from_user.id,
                start_date=filters['start_date'],
                end_date=filters['end_date'],
                filters=db_filters if db_filters else None
            )
            logger.info("Получено %d записей из базы данных", len(df))
            
            if df.empty:
                await query.edit_message_text("⚠ По вашему запросу ничего не найдено")
                db.close()
                if 'export_filters' in context.user_data:
                    del context.user_data['export_filters']
                return

            df.fillna('', inplace=True)
            df['transaction_date'] = pd.to_datetime(df['transaction_date']).dt.strftime('%d.%m.%Y %H:%M')
            df.replace('NaN', '', inplace=True) # Дополнительная замена строки "NaN"
            
            logger.debug("Значения NaN (и другие отсутствующие) заменены на пустые строки.")
            
            # Словарь для переименования столбцов
            column_mapping = {
                'id': 'ID',
                'transaction_date': 'Дата',
                'amount': 'Сумма',
                'cash_source': 'Наличность',
                'target_amount': 'Сумма (куда)',
                'target_cash_source': 'Наличность (куда)',
                'category': 'Категория',
                'description': 'Описание',
                'transaction_type': 'Тип транзакции',
                'counterparty': 'Контрагент',
                'check_num': 'Чек #',
                'transaction_class': 'Класс'
            }
            
            # Переименовываем столбцы
            # df = df.rename(columns=column_mapping)
            df_renamed = df.rename(columns=column_mapping)
            logger.debug("Столбцы после переименования: %s", df_renamed.columns.tolist())
            
            with NamedTemporaryFile(suffix='.csv', delete=False, mode='w', encoding='utf-8') as tmp:
                df_renamed.to_csv(tmp.name, index=False, encoding='utf-8', sep=',')
                
            try:    
                await context.bot.send_document(
                    chat_id=query.from_user.id,
                    document=open(tmp.name, 'rb'),
                    caption=f"Отчет за {filters['start_date'].strftime('%d.%m.%Y')} - {filters['end_date'].strftime('%d.%m.%Y')}\n"
                            f"📌 Всего записей: {len(df_renamed)}"
                )
            
                # --- ФОРМИРОВАНИЕ СВОДКИ ПО ФИЛЬТРАМ ---
                filter_summary_lines = []
                # Добавляем диапазон дат (он всегда есть)
                filter_summary_lines.append(f"📅 Период: {filters.get('start_date').strftime('%d.%m.%Y')} - {filters.get('end_date').strftime('%d.%m.%Y')}")

                # Словарь для красивых названий фильтров
                filter_display_names = {
                    'category': '🏷 Категория',
                    'transaction_type': '🔀 Тип',
                    'cash_source': '💳 Наличность',
                    'counterparty': '👥 Контрагент',
                    'check_num': '🧾 Чек',
                    'transaction_class': '📊 Класс',
                    'import_id': '📦 ID импорта'
                }

                # Добавляем остальные активные фильтры (те, что не 'Все')
                for key, display_name in filter_display_names.items():
                    filter_value = filters.get(key)
                    # Показываем фильтр, если он был задан и не равен 'Все'
                    if filter_value and filter_value != 'Все':
                        filter_summary_lines.append(f"{display_name}: {filter_value}")

                filter_summary = "\n".join(filter_summary_lines)
                # -----------------------------------------

                # --- ОБНОВЛЕНИЕ СООБЩЕНИЯ ОБ УСПЕХЕ ---
                success_message = "✅ Отчет успешно сформирован"
                # Добавляем сводку по фильтрам, если она не пустая
                if filter_summary:
                    success_message += "\n\n⚙️ <b>Примененные фильтры:</b>\n" + filter_summary

                # Используем parse_mode='HTML' для жирного шрифта
                await query.edit_message_text(success_message, parse_mode='HTML')
                # -----------------------------------------

            # await query.edit_message_text("✅ Отчет успешно сформирован") 

            except Exception as send_error:
                # ... (обработка ошибки отправки) ...
                logger.error(f"Ошибка отправки файла отчета: {send_error}", exc_info=True)
                await query.edit_message_text("❌ Ошибка при отправке файла отчета.")
            finally:
                # ... (удаление временного файла) ...
                if os.path.exists(tmp.name):
                    os.unlink(tmp.name)

        except Exception as e:
            logger.error("Ошибка генерации отчета: %s", e, exc_info=True)
            await query.edit_message_text("❌ Ошибка при формировании отчета")
        finally:
            # Гарантированно закрываем соединение с БД
            if db:
                db.close()
            # Гарантированно очищаем фильтры из user_data *после* их использования
            context.user_data.pop('export_filters', None)

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
        settings = parse_user_settings(full_text)

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
        settings = parse_user_settings(message_text)
        
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
        # Разбираем аргументы с учетом кавычек
            args = shlex.split(update.message.text)
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


    async def config_selection_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор конфига для просмотра"""
        query = update.callback_query
        await query.answer()
        
        config_map = {
            'view_categories': 'categories.yaml',
            'view_special': 'special_conditions.yaml',
            'view_pdf_patterns': 'pdf_patterns.yaml',
            'view_timeouts': 'timeouts.yaml'
        }
        
        if query.data == 'back_to_main':
            # Используем query.message вместо update
            await self.show_config_menu(query.message)
            return
        elif query.data == 'view_all':
            await self.send_all_config_files(query)
            return
        
        filename = config_map[query.data]
        await self.send_single_config_file(query, filename)


    async def send_single_config_file(self, query, filename):
        """Отправляет один выбранный конфигурационный файл"""
        config_dir = os.path.join(os.path.dirname(__file__), 'config')
        # config_dir = '/app/config'
        filepath = os.path.join(config_dir, filename)
        
        descriptions = {
            'categories.yaml': 'Категории транзакций',
            'special_conditions.yaml': 'Специальные условия',
            'pdf_patterns.yaml': 'PDF паттерны',
            'timeouts.yaml': 'Таймауты обработки'
        }
        
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Отправляем как текстовое сообщение
                await query.message.reply_text(
                    f"*{descriptions.get(filename, filename)}*:\n```yaml\n{content}\n```",
                    parse_mode='Markdown'
                )
                
                # Или как файл, если предпочтительнее
                # with open(filepath, 'rb') as f:
                #     await query.message.reply_document(
                #         document=f,
                #         caption=f"{descriptions.get(filename, filename)}"
                #     )
                
            except Exception as e:
                logger.error(f"Ошибка при отправке файла {filename}: {e}")
                await query.message.reply_text(f"Ошибка при отправке файла {filename}")
        else:
            await query.message.reply_text(f"Файл {filename} не найден")


    async def send_all_config_files(self, query):
        """Отправляет все конфигурационные файлы"""
        config_files = {
            'categories.yaml': 'Категории транзакций',
            'special_conditions.yaml': 'Специальные условия',
            'pdf_patterns.yaml': 'PDF паттерны',
            'timeouts.yaml': 'Таймауты обработки'
        }
        
        for filename, description in config_files.items():
            await self.send_single_config_file(query, filename)
            await asyncio.sleep(0.5)
            
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

    @admin_only
    async def show_config_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        """Показывает меню управления конфигурацией"""
        # Получаем сообщение из разных источников
        if isinstance(update, Update) and update.message:
            message = update.message
        elif isinstance(update, Update) and update.callback_query and update.callback_query.message:
            message = update.callback_query.message
        elif hasattr(update, 'reply_text'):  # Если передано сообщение напрямую
            message = update
        else:
            logger.error("Не удалось определить сообщение для show_config_menu")
            return
        
        keyboard = [
            [InlineKeyboardButton("🔎 Просмотреть конфиг", callback_data='view_config')],
            [InlineKeyboardButton("✏️ Редактировать конфиг", callback_data='edit_config')],
            [InlineKeyboardButton("📝 Добавить Категорию - Паттерн", callback_data='add_pattern_interactive')],
            [InlineKeyboardButton("👁️ Просмотреть логи", callback_data='view_logs')],
            [InlineKeyboardButton("🔄 Перезагрузить бота", callback_data='restart')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text(
            "Управление конфигурацией:",
            reply_markup=reply_markup
        )

    # Callback обработчики

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


    async def show_config_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню выбора конфига для просмотра"""
        query = update.callback_query
        await query.answer()

        """Показывает меню выбора конфига для просмотра"""
        keyboard = [
            [InlineKeyboardButton("Категории", callback_data='view_categories')],
            [InlineKeyboardButton("Добавить Категорию - паттерн", callback_data='add_pattern_interactive')],
            [InlineKeyboardButton("Спец. условия", callback_data='view_special')],
            [InlineKeyboardButton("PDF паттерны", callback_data='view_pdf_patterns')],
            [InlineKeyboardButton("Таймауты", callback_data='view_timeouts')],
            [InlineKeyboardButton("Все файлы", callback_data='view_all')],
            [InlineKeyboardButton("↩️ Назад", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try: # <-- Добавьте этот блок try
            await query.edit_message_text(
                text="Выберите конфигурационный файл для просмотра:",
                reply_markup=reply_markup
            )
            logger.info("Successfully edited message in show_config_selection") # Опционально: добавьте логирование успешного действия
        except telegram.error.BadRequest as e: # <-- Обработка специфичной ошибки Telegram
            logger.error(f"Ошибка BadRequest при редактировании сообщения в show_config_selection: {e}", exc_info=True) # Логируйте ошибку
            # Отправьте пользователю сообщение об ошибке, так как редактирование не удалось
            try:
                 await query.message.reply_text("Произошла ошибка при отображении меню. Попробуйте еще раз.")
            except Exception as reply_error:
                 logger.error(f"Не удалось отправить сообщение об ошибке пользователю: {reply_error}")
        except Exception as e: # <-- Обработка любых других неожиданных ошибок
            logger.error(f"Неожиданная ошибка в show_config_selection: {e}", exc_info=True) # Логируйте ошибку
            try:
                 await query.message.reply_text("Произошла непредвиденная ошибка.")
            except Exception as reply_error:
                 logger.error(f"Не удалось отправить сообщение об ошибке пользователю: {reply_error}")


    async def edit_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает callback из меню редактирования"""
        logger.debug("edit_menu_callback: Вызван обработчик")
        query = update.callback_query
        await query.answer()
        
        if query.data == 'cancel':
            await query.edit_message_text(text="Редактирование отменено")
            return
        
        config_map = {
            'edit_categories': 'categories.yaml',
            'edit_special': 'special_conditions.yaml',
            'edit_pdf_patterns': 'pdf_patterns.yaml',
            'edit_timeouts': 'timeouts.yaml',
            'edit_class_contractor.yaml': 'class_contractor.yaml'
        }
        filename = config_map.get(query.data)

        if not filename:
            await query.edit_message_text("Ошибка: Неизвестный файл для редактирования.")
            return

        context.user_data['editing_file'] = filename
        logger.info(f"edit_menu_callback: Установлен editing_file: {filename}. Добавляю config_handlers в группу -1.")

        await query.edit_message_text(
            text=f"Отправьте новое содержимое файла {filename} в виде текста "
                 "или файлом YAML. Используйте /cancel для отмены."
        )
        
        handlers_added_count = 0
        for handler_obj in self.config_handlers: # handler_obj чтобы не путать с переменной handler из PTB
            handler_name = "handle_config_edit" if handler_obj.callback == self.handle_config_edit else "handle_config_upload"
            logger.debug(f"edit_menu_callback: Добавляю config_handler ({handler_name}) с ID: {id(handler_obj)} в группу -1.")
            self.application.add_handler(handler_obj, group=-1)
            handlers_added_count += 1
        logger.info(f"edit_menu_callback: Добавлено {handlers_added_count} обработчиков из self.config_handlers в группу -1.")


    async def show_edit_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню выбора файла для редактирования"""
        logger.debug("show_edit_menu: Вызван обработчик")
        query = update.callback_query
        await query.answer()
        
        keyboard = [
            [InlineKeyboardButton("Категории", callback_data='edit_categories')],
            [InlineKeyboardButton("Спец. условия", callback_data='edit_special')],
            [InlineKeyboardButton("PDF паттерны", callback_data='edit_pdf_patterns')],
            [InlineKeyboardButton("Таймауты", callback_data='edit_timeouts')],
            [InlineKeyboardButton("↩️ Отмена", callback_data='cancel')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try: # <-- Добавьте этот блок try
            await query.edit_message_text(
                text="Выберите файл для редактирования:",
                reply_markup=reply_markup
            )
            logger.info("Successfully edited message in show_config_selection") # Опционально: добавьте логирование успешного действия
        except telegram.error.BadRequest as e: # <-- Обработка специфичной ошибки Telegram
            logger.error(f"Ошибка BadRequest при редактировании сообщения в show_config_selection: {e}", exc_info=True) # Логируйте ошибку
            # Отправьте пользователю сообщение об ошибке, так как редактирование не удалось
            try:
                 await query.message.reply_text("Произошла ошибка при отображении меню. Попробуйте еще раз.")
            except Exception as reply_error:
                 logger.error(f"Не удалось отправить сообщение об ошибке пользователю: {reply_error}")
        except Exception as e: # <-- Обработка любых других неожиданных ошибок
            logger.error(f"Неожиданная ошибка в show_edit_menu: {e}", exc_info=True) # Логируйте ошибку
            try:
                 await query.message.reply_text("Произошла непредвиденная ошибка.")
            except Exception as reply_error:
                 logger.error(f"Не удалось отправить сообщение об ошибке пользователю: {reply_error}")


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
                

    async def handle_config_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает текстовое редактирование конфига"""
        logger.debug("handle_config_edit: editing_file = %s", context.user_data.get('editing_file'))
        logger.debug("handle_config_edit: Получен текст от пользователя")
        logger.info("handle_config_edit: Начало обработки текстового редактирования.") # Добавлено
        
        # Пропускаем если это часть процесса редактирования транзакций
        # или если edit_mode все еще существует (на всякий случай)
        if context.user_data.get('edit_mode'): # Проверяем наличие ключа 'edit_mode'
            logger.debug("handle_config_edit: Обнаружен активный 'edit_mode', пропускаем.")
            return

        if 'editing_file' not in context.user_data:
            logger.warning("handle_config_edit: 'editing_file' не найден в user_data.")
            # Это сообщение не должно появляться, если мы не в режиме редактирования конфига.
            # Если оно появляется после apply_edits, значит apply_edits не остановил обработку.
            # await update.message.reply_text("Не выбрано файл для редактирования") # Пока закомментируем, чтобы не мешало
            return

        filename = context.user_data['editing_file']
        new_content_text = update.message.text # Получаем текст

        logger.info(f"handle_config_edit: Редактируется файл: {filename}") # Добавлено
        logger.debug(f"handle_config_edit: Получено новое содержимое:\n{new_content_text[:500]}...") # Добавлено (первые 500 символов)

        try:
            logger.info("handle_config_edit: Попытка парсинга YAML...") # Добавлено
            # Проверяем валидность YAML
            parsed_data = yaml.safe_load(new_content_text)
            # Можно добавить более строгую проверку структуры, если нужно
            if parsed_data is None:
                 await update.message.reply_text("Ошибка: Не удалось распознать YAML.")
                 return
            if not isinstance(parsed_data, dict): # Проверка, что корень - словарь
                 await update.message.reply_text("Ошибка: Корневой элемент YAML должен быть словарем (например, начинаться с 'categories:')")
                 return
            # Дополнительная проверка для categories.yaml
            if filename == 'categories.yaml' and ('categories' not in parsed_data or not isinstance(parsed_data['categories'], list)):
                await update.message.reply_text("Ошибка: Файл categories.yaml должен содержать ключ 'categories' со списком категорий.")
                return
            
            # Получаем абсолютный путь к файлу конфигурации
            config_dir = os.path.join(os.path.dirname(__file__), 'config')
            # config_dir = '/app/config'
            filepath = os.path.join(config_dir, filename)
            logger.info(f"handle_config_edit: Путь для сохранения файла: {filepath}") # Добавлено
            # logger.info(f"Попытка сохранения файла в: {filepath}")
            
            # Проверяем существование директории
            if not os.path.exists(config_dir):
                try:
                    os.makedirs(config_dir, mode=0o775)
                    logger.info(f"Создана директория: {config_dir}")
                except OSError as e:
                    logger.error(f"Ошибка создания директории {config_dir}: {e}")
                    await update.message.reply_text(f"Ошибка создания директории: {e}")
                    return

            # Проверяем права доступа
            if not os.access(config_dir, os.W_OK):
                logger.error(f"Нет прав на запись в директорию {config_dir}")
                await update.message.reply_text(f"Нет прав на запись в директорию {config_dir}")
                return

            # Проверяем права доступа к файлу (добавлено)
            if os.path.exists(filepath) and not os.access(filepath, os.W_OK):
                logger.error(f"Нет прав на запись в файл {filepath}")
                await update.message.reply_text(f"Нет прав на запись в файл {filepath}")
                return

            # Сохраняем изменения
            try:
                # Преобразуем Python объект обратно в YAML строку с правильным форматированием
                # ensure_ascii=False для поддержки кириллицы, allow_unicode=True тоже полезно
                logger.info("handle_config_edit: Попытка записи файла...") # Добавлено
                yaml_to_write = yaml.dump(parsed_data, allow_unicode=True, sort_keys=False, indent=2)

                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(yaml_to_write) # <-- Пишем отформатированный YAML
                logger.info(f"handle_config_edit: Файл {filename} успешно сохранен по пути: {filepath}")
                # !!! Добавляем сообщение об успехе !!!
                await update.message.reply_text(f"✅ Файл {filename} успешно обновлен!")

            except (IOError, PermissionError) as e:
                logger.error(f"Ошибка сохранения файла {filepath}: {e}")
                await update.message.reply_text(f"Ошибка сохранения файла: {e}")
                return
                        
        except yaml.YAMLError as e:
            logger.error(f"handle_config_edit: Ошибка парсинга YAML: {str(e)}", exc_info=True) # Добавлено exc_info
            await update.message.reply_text(f"Ошибка в YAML: {str(e)}\nПопробуйте еще раз")
        except Exception as e:
            logger.error(f"handle_config_edit: Ошибка при сохранении файла: {str(e)}", exc_info=True) # Добавлено exc_info
            await update.message.reply_text(f"Ошибка при сохранении файла: {str(e)}")

        finally: # Добавлено
            logger.info("handle_config_edit: Удаление обработчиков и 'editing_file'.") # Добавлено
            self.remove_config_handlers() # Убедитесь, что это вызывается
            if 'editing_file' in context.user_data:
                del context.user_data['editing_file']
            # Возвращаем в главное меню
            # await self.show_config_menu(update, context) # Возможно, это не нужно здесь, или нужно update.message


    async def handle_config_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает загрузку конфига файлом"""
        logger.debug("handle_config_upload: Вызван обработчик")
        if 'editing_file' not in context.user_data:
            await update.message.reply_text("Не выбрано файл для редактирования")
            return
            
        filename = context.user_data['editing_file']
        document = update.message.document
        
        if not document.file_name.lower().endswith('.yaml'):
            await update.message.reply_text("Пожалуйста, отправьте файл в формате YAML")
            return
        
        try:
            # Получаем абсолютный путь
            config_dir = os.path.join(os.path.dirname(__file__), 'config')
            # config_dir = '/app/config'
            filepath = os.path.join(config_dir, filename)
            
            # Скачиваем временный файл
            # file = await document.get_file()
            file = await document.get_file(
                read_timeout=30,
                connect_timeout=30,
                pool_timeout=30,
                write_timeout=30
            )
            downloaded_file = await file.download_to_drive()
            
            # Проверяем валидность YAML
            with open(downloaded_file, 'r', encoding='utf-8') as f:
                content = f.read()
                yaml.safe_load(content)
            
            # Проверяем существование директории
            if not os.path.exists(config_dir):
                try:
                    os.makedirs(config_dir)
                except OSError as e:
                    logger.error(f"Ошибка создания директории {config_dir}: {e}")
                    await update.message.reply_text(f"Ошибка создания директории: {e}")
                    return

            # Сохраняем файл
            os.replace(downloaded_file, filepath)

            logger.info(f"Файл {filename} успешно обновлен по пути: {filepath}")
            await update.message.reply_text(f"✅ Файл {filename} успешно обновлен!")
            
        except yaml.YAMLError as e:
            logger.error(f"Ошибка в YAML: {str(e)}")
            await update.message.reply_text(f"Ошибка в YAML: {str(e)}\nПопробуйте еще раз")
            if os.path.exists(downloaded_file):
                os.unlink(downloaded_file)
        except Exception as e:
            logger.error(f"Ошибка: {str(e)}")
            await update.message.reply_text(f"Ошибка: {str(e)}")
            if os.path.exists(downloaded_file):
                os.unlink(downloaded_file)

        finally: # Добавлено
            logger.info("handle_config_upload: Удаление обработчиков и 'editing_file'.") # Добавлено
            self.remove_config_handlers() # Убедитесь, что это вызывается
            if 'editing_file' in context.user_data:
                del context.user_data['editing_file']

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
        query = update.callback_query
        await query.answer()
        
        user_data = context.user_data
        
        if query.data == 'save_no':
            await query.edit_message_text("ℹ️ Данные не сохранены")
            
            if 'temp_files' in user_data:
                await self.cleanup_files(user_data['temp_files'])
                del user_data['temp_files']
            
            if 'pending_data' in user_data:
                del user_data['pending_data']
            return
        
        # Только для ответа "Да" продолжаем проверки
        pending_data = user_data.get('pending_data', {})
        df_to_save = pending_data.get('df')
        pdf_type_to_save = pending_data.get('pdf_type')

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
            db = Database()
            stats = db.save_transactions(pending_data['df'], query.from_user.id, pdf_type_to_save)
            
            logger.info(
                f"💾 Сохранено: 🆕 новых - {stats['new']}, 📑 дубликатов - {stats['duplicates']}"
            )
            
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
                await self.cleanup_files(user_data['temp_files'])
                del user_data['temp_files']
            
            if 'pending_data' in user_data:
                del user_data['pending_data']

    def update_transaction(self, date, amount, new_category):
        with self.get_cursor() as cur:
            cur.execute("""
                UPDATE transactions 
                SET category = %s 
                WHERE transaction_date = %s 
                AND amount = %s
            """, (new_category, date, amount))


    async def handle_duplicates_decision(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user_data = context.user_data
        duplicates = user_data.get('pending_duplicates', [])
        
        if not duplicates:
            await query.edit_message_text("ℹ️ Нет данных для обновления")
            return

        if query.data == 'update_duplicates':
            try:
                db = Database()
                updated = 0
                
                for row in duplicates:
                    # Логика обновления существующих записей
                    # Например:
                    db.update_transaction(
                        date=row['Дата'],
                        amount=row['Сумма'],
                        new_category=row['Категория']
                    )
                    updated += 1
                    
                logger.info(f"Обновлено {updated} дубликатов")
                await query.edit_message_text(f"✅ Обновлено {updated} записей")
                
            except Exception as e:
                logger.error(f"Ошибка обновления: {e}")
                await query.edit_message_text("❌ Ошибка при обновлении")
                
        elif query.data == 'skip_duplicates':
            await query.edit_message_text("Дубликаты пропущены")
        
        # Очистка временных данных
        user_data.pop('pending_duplicates', None)



    async def cleanup_files(self, file_paths):
        for path in file_paths:
            if path and os.path.exists(path) and os.path.isfile(path):
                try:
                    await asyncio.to_thread(os.unlink, path)
                    logger.debug(f"Удален временный файл: {path}")
                except Exception as e:
                    logger.error(f"Ошибка удаления {path}: {e}")


    async def handle_logfile_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор файла логов"""
        query = update.callback_query
        await query.answer()
        
        filename = query.data.replace('logfile_', '')
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
        """Обрабатывает выбор варианта просмотра логов"""
        query = update.callback_query
        await query.answer()
        
        action, filename = query.data.replace('logview_', '').split('_', 1)
        log_path = os.path.join(os.path.dirname(__file__), 'logs', filename)

        file_size = os.path.getsize(log_path)
        if file_size > 5 * 1024 * 1024:  # 5 MB
            await query.message.reply_text("Файл лога слишком большой (>5 MB) для просмотра. Используйте скачивание.")
            return
        
        try:
            if action == 'text':
                # Читаем настроенное количество последних строк
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()[-self.log_lines_to_show:] # <--- Используем self.log_lines_to_show
                    content = ''.join(lines)

                # Очищаем текст от проблемных символов
                content = self.sanitize_log_content(content)
                
                # Разбиваем на части если слишком длинное
                if len(content) > 4000:
                    parts = [content[i:i+4000] for i in range(0, len(content), 4000)]
                    for part in parts:
                        try:
                            await query.message.reply_text(
                                f"Последние {self.log_lines_to_show} строк из {filename}:\n<pre>{part}</pre>",
                                parse_mode='HTML'
                            )
                        except Exception:
                            await query.message.reply_text(
                                f"Последние {self.log_lines_to_show} строк из {filename}:\n{part}"
                            )
                        await asyncio.sleep(0.5)
                else:
                    try:
                        await query.message.reply_text(
                            f"Последние {self.log_lines_to_show} строк из {filename}:\n<pre>{content}</pre>",
                            parse_mode='HTML'
                        )
                    except Exception:
                        await query.message.reply_text(
                            f"Последние {self.log_lines_to_show} строк из {filename}:\n{content}"
                        )
                    
            elif action == 'file':
                # Отправляем файл целиком
                with open(log_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        caption=f"Полный лог файл: {filename}"
                    )
            
            # Возвращаемся к выбору вариантов просмотра
            try:
                await self.handle_logfile_selection(update, context)
            except telegram.error.BadRequest as e:
                if "not modified" in str(e):
                    logger.debug("Сообщение не изменилось, пропускаем ошибку")
                else:
                    raise
                    
        except Exception as e:
            logger.error(f"Ошибка при обработке логов: {e}")
            try:
                await query.edit_message_text(f"Ошибка: {str(e)}")
            except telegram.error.BadRequest:
                pass

    def sanitize_log_content(self, content: str) -> str:
        """Очищает текст лога от проблемных символов"""
        # Удаляем или заменяем символы, которые могут вызывать проблемы с форматированием
        replacements = {
            '<': '&lt;',
            '>': '&gt;',
            '&': '&amp;',
            '`': "'",
            '*': '',
            '_': '',
            '[': '(',
            ']': ')',
            '~': '-'
        }
        for old, new in replacements.items():
            content = content.replace(old, new)
        return content

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
            TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
            if not TOKEN:
                logger.error("Не удалось получить токен бота")
                return
            
            logger.info("Запуск нового процесса...")
            subprocess.Popen([sys.executable, __file__])
            
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
        """Останавливает бота"""
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
    """Простая проверка здоровья для Docker"""
    try:
        # Можно добавить реальные проверки работоспособности
        return True
    except Exception:
        return False

# if os.getenv('DOCKER_MODE'):
#     logger.info("Бот запущен в Docker-контейнере")
    # Здесь можно добавить специфичные для Docker настройки

if __name__ == '__main__':
    # Проверка на дублирующийся запуск уже добавлена в начале файла
    
    # Получаем токен из переменной окружения
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
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