__version__ = "3.4.0"

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
from database import Database
from datetime import datetime, timedelta
from telegram_bot_calendar import DetailedTelegramCalendar, LSTEP
from telegram.ext.filters import BaseFilter

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
        backupCount=30,
        encoding='utf-8'
    )
    
    file_handler.suffix = "%Y-%m-%d_bot.log"
    file_handler.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}_bot\.log$")
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
       
    # Основной логгер
    logger = logging.getLogger()

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

# Декоратор для проверки доступа
def admin_only(func):
    async def wrapper(*args, **kwargs):
        # ... (логика определения update)
        # update = args[1] if len(args) >= 2 else (kwargs.get('update') or args[0]) # Упрощенный пример
        
        # Определяем update из аргументов (код из вашего файла)
        if len(args) >= 2 and isinstance(args[1], Update): # Проверка для методов класса
            update = args[1]
        elif len(args) >= 1 and isinstance(args[0], Update): # Проверка для обычных функций
             update = args[0]
        elif 'update' in kwargs and isinstance(kwargs['update'], Update):
            update = kwargs['update']
        else: # Попытка найти Update, если он не первый или не именованный аргумент
            found_update = next((arg for arg in args if isinstance(arg, Update)), None)
            if not found_update:
                 logger.error("admin_only: Не удалось найти объект Update в аргументах.")
                 # В зависимости от строгости, можно либо пропустить, либо вернуть ошибку
                 return await func(*args, **kwargs) # Или вернуть ошибку доступа
            update = found_update

        if not update or not hasattr(update, 'effective_user') or not update.effective_user:
            logger.error("admin_only: Объект Update или effective_user не найден или некорректен.")
            # Решите, как обрабатывать эту ситуацию: пропустить проверку или запретить доступ
            return await func(*args, **kwargs) # Пример: пропуск проверки, если нет пользователя

        user_id = update.effective_user.id
        logger.debug(f"admin_only: Проверка доступа для user_id: {user_id}. Входит в ALLOWED_USERS: {user_id in ALLOWED_USERS}") # <--- ДОБАВЬТЕ ЭТОТ ЛОГ

        if user_id not in ALLOWED_USERS:
            logger.warning(f"Попытка доступа от неавторизованного пользователя: {user_id}")
            if hasattr(update, 'message') and update.message:
                await update.message.reply_text("🚫 Доступ запрещен. Вы не авторизованы для использования этого бота.")
            elif hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.answer("Доступ запрещен")
                logger.debug(f"admin_only: Отправлен ответ 'Доступ запрещен' пользователю {user_id}") # <--- ДОБАВЬТЕ ЭТОТ ЛОГ
            return
        return await func(*args, **kwargs)
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

        # Настройка Application
        self.application = Application.builder() \
            .token(token) \
            .read_timeout(self.request_timeout) \
            .write_timeout(self.request_timeout) \
            .build()

        # Регистрация обработчиков
        self.setup_handlers()
        
        # self.application.add_handler(CallbackQueryHandler(
        #     self.config_selection_callback,
        #     pattern='^(view_categories|view_special|view_pdf_patterns|view_timeouts|view_all|back_to_main)$'
        # ))

        self.application.add_handler(CallbackQueryHandler(
            self.config_selection_callback,
            pattern=re.compile(r'^(view_categories|view_special|view_pdf_patterns|view_timeouts|view_all|back_to_main)$')
        ))

        # Обработчик для ввода паттерна
        self.pattern_handler = MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.handle_pattern_input
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
        self.application.add_handler(CommandHandler("reset", self.reset_settings))

        # self.application.add_handler(CallbackQueryHandler(self.set_filter, pattern='^set_'))
        # self.application.add_handler(CallbackQueryHandler(self.handle_calendar_callback, pattern=r"^calendar:"),group=0)
        self.application.add_handler(CallbackQueryHandler(self.handle_calendar_callback, pattern=r"^cbcal_"),group=0)
        self.application.add_handler(CallbackQueryHandler(self.generate_report, pattern='^generate_report'))
        self.application.add_handler(CallbackQueryHandler(self.show_filters_menu, pattern='^back_to_filters'))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_config_edit),group=-1)
        self.application.add_handler(CallbackQueryHandler(self.handle_filter_callback, pattern='^(cat|type|source|class)_'))
        self.application.add_handler(CallbackQueryHandler(self.set_start_date, pattern='^set_start_date$'))
        self.application.add_handler(CallbackQueryHandler(self.set_end_date, pattern='^set_end_date$'))     
        self.application.add_handler(CallbackQueryHandler(self.set_category, pattern='^set_category$'))
        self.application.add_handler(CallbackQueryHandler(self.set_type, pattern='^set_type$'))
        self.application.add_handler(CallbackQueryHandler(self.set_cash_source, pattern='^set_cash_source'))
        self.application.add_handler(CallbackQueryHandler(self.set_counterparty, pattern='^set_counterparty'))
        self.application.add_handler(CallbackQueryHandler(self.set_check_num, pattern='^set_check_num'))
        self.application.add_handler(CallbackQueryHandler(self.set_class, pattern='^set_class'))
        self.application.add_handler(CallbackQueryHandler(self.cancel_export, pattern='^cancel_export$'))
        self.application.add_handler(CallbackQueryHandler(self.debug_callback, pattern='.*'))

        # Регистрируем обработчик календаря, используя безопасную обертку, определенную в классе
        # Он регистрируется здесь после других обработчиков, чтобы гарантировать проверку обработчиков с более высоким приоритетом первыми.
        # self.application.add_handler(
        #     CallbackQueryHandler(
        #         self.handle_calendar_callback,
        #         pattern=self.safe_calendar_pattern_wrapper(DetailedTelegramCalendar.func())
        #     ),
        #     group=1
        # )

        # self.application.add_handler(CallbackQueryHandler(self.handle_calendar_callback, pattern=r"^calendar:"),group=0)

        self.application.add_handler(MessageHandler(
            filters.Document.ALL,
            self.handle_document
        ))
        
        # Обработчики callback-запросов (только для админов)
        self.application.add_handler(
            CallbackQueryHandler(
                self.main_menu_callback,
                pattern='^(view_config|edit_config|restart|view_logs)$'
            )
        )
        
        self.application.add_handler(
            CallbackQueryHandler(
                self.edit_menu_callback,
                pattern='^(edit_categories|edit_special|edit_pdf_patterns|edit_timeouts|cancel)$'
            )
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

        self.application.add_handler(CallbackQueryHandler(
        self.handle_save_confirmation,
        pattern='^save_(yes|no)$'
        ))

        self.application.add_handler(
            CallbackQueryHandler(
                self.handle_duplicates_decision,
                pattern='^(update_duplicates|skip_duplicates)$'
            )
        )

        self.application.add_handler(CommandHandler("cancel", self.cancel_operation))

        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)

    @admin_only
    async def show_filters_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обновленное меню фильтров с новыми полями"""
        user_data = context.user_data
        filters = user_data['export_filters']
        
        keyboard = [
            [InlineKeyboardButton(f"📅 Дата начала: {filters['start_date']}", callback_data='set_start_date')],
            [InlineKeyboardButton(f"📅 Дата окончания: {filters['end_date']}", callback_data='set_end_date')],
            [InlineKeyboardButton(f"🏷 Категория: {filters['category']}", callback_data='set_category')],
            [InlineKeyboardButton(f"🔀 Тип: {filters['transaction_type']}", callback_data='set_type')],
            [InlineKeyboardButton(f"💳 Наличность: {filters['cash_source']}", callback_data='set_cash_source')],
            [InlineKeyboardButton(f"👥 Контрагент: {filters['counterparty']}", callback_data='set_counterparty')],
            [InlineKeyboardButton(f"🧾 Чек: {filters['check_num']}", callback_data='set_check_num')],
            [InlineKeyboardButton(f"📊 Класс: {filters['transaction_class']}", callback_data='set_class')],
            [InlineKeyboardButton("✅ Сформировать отчет", callback_data='generate_report')],
            [InlineKeyboardButton("❌ Отмена", callback_data='cancel_export')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "⚙ Настройте параметры отчета:",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                "⚙ Настройте параметры отчета:",
                reply_markup=reply_markup
            )

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

    @admin_only
    async def set_start_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.debug("Создание календаря для выбора даты начала")
        logger.debug("Вызов set_start_date для user_id=%s", update.effective_user.id)
        query = update.callback_query
        await query.answer()
        calendar, step = DetailedTelegramCalendar().build()
        await query.message.reply_text(
            f"📅 Выберите дату начала ({LSTEP[step]}):",  # Используем LSTEP для отображения текущего шага (год/месяц/день)
            reply_markup=calendar
        )
        context.user_data["calendar_context"] = "start_date" 

    @admin_only
    async def set_end_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Вызов set_end_date для user_id=%s", update.effective_user.id)
        query = update.callback_query
        await query.answer()
        calendar, step = DetailedTelegramCalendar().build()
        await query.message.reply_text(
            f"📅 Выберите дату окончания ({LSTEP[step]}):", # Используем LSTEP для отображения текущего шага
            reply_markup=calendar
        )
        context.user_data["calendar_context"] = "end_date"

    @admin_only
    async def handle_calendar_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        logger.debug(f"Raw callback data: {update.callback_query.data}")
        logger.info(f"Calendar data received: {query.data}")
        logger.debug(f"Получен callback от календаря: {query.data}")
        await query.answer()
        result, key, step = DetailedTelegramCalendar().process(query.data)

        calendar_context = context.user_data.get("calendar_context") # Получаем контекст (start_date или end_date)

        if not result and key:
            # Если дата еще не выбрана (пользователь выбирает год/месяц), обновляем календарь
            await query.edit_message_text(f"📅 Выберите {calendar_context.replace('_', ' ')} ({LSTEP[step]}):", reply_markup=key)
        elif result:
            # Если дата выбрана (result - это объект datetime.date)
            selected_date_str = result.strftime('%d.%m.%Y') # Форматируем дату как строку

            if calendar_context == "start_date":
                context.user_data['export_filters']['start_date'] = selected_date_str
                logger.info("Установлена дата начала через календарь: %s", selected_date_str)
            elif calendar_context == "end_date":
                context.user_data['export_filters']['end_date'] = selected_date_str
                logger.info("Установлена дата окончания через календарь: %s", selected_date_str)

            # Очищаем временный контекст календаря
            if "calendar_context" in context.user_data:
                del context.user_data["calendar_context"]

            # Можно уведомить пользователя о выбранной дате (опционально, т.к. сразу покажем меню)
            # await query.edit_message_text(f"Выбрана дата: {selected_date_str}")

            # Возвращаемся к меню фильтров, где будет отображена выбранная дата
            await self.show_filters_menu(update, context)

    @admin_only
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
            keyboard.append([InlineKeyboardButton("Назад", callback_data='back_to_filters')])

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

    @admin_only
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
        keyboard.append([InlineKeyboardButton("Назад", callback_data='back_to_filters')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выберите тип транзакции:", reply_markup=reply_markup)

    @admin_only
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
        keyboard.append([InlineKeyboardButton("Назад", callback_data='back_to_filters')])
        
        await query.edit_message_text(
            "Выберите источник средств:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    @admin_only      
    async def set_counterparty(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню выбора Контрагента"""
        logger.info("Обработчик set_counterparty вызван")
        query = update.callback_query
        await query.answer()
        
        await query.edit_message_text(
            "Введите имя контрагента или часть названия:"
        )
        context.user_data['awaiting_input'] = 'counterparty'

    @admin_only
    async def set_check_num(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Меню выбора Чека"""
        logger.info("Обработчик set_check_num вызван")
        query = update.callback_query
        await query.answer()
        
        await query.edit_message_text(
            "Введите номер чека или часть номера:"
        )
        context.user_data['awaiting_input'] = 'check_num'

    @admin_only
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
        keyboard.append([InlineKeyboardButton("Назад", callback_data='back_to_filters')])
        
        await query.edit_message_text(
            "Выберите класс транзакции:",
            reply_markup=InlineKeyboardMarkup(keyboard))

    @admin_only
    async def cancel_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data.pop('export_filters', None)
        await query.edit_message_text("Экспорт отменен.")

    @admin_only
    async def debug_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        logger.info("Получен callback: %s", query.data)
        logger.debug(f"DEBUG_CALLBACK: Получен callback_data: '{query.data}' от user_id: {query.from_user.id}") # Улучшенный лог
        await query.answer()

    # Обновим обработчик текстового ввода
    @admin_only
    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Сначала проверяем, не находимся ли мы в режиме редактирования конфига
        if context.user_data.get('editing_file'):
            # Мы ожидаем, что handle_config_edit перехватит это сообщение.
            # Если же оно дошло сюда, значит, что-то не так с приоритетами
            # или регистрацией handle_config_edit.
            # Логируем это как потенциальную проблему, но не обрабатываем здесь,
            # чтобы дать шанс handle_config_edit (если он все же как-то сработает позже
            # или если проблема в другом).
            logger.warning(f"handle_text_input: Получен текст, но мы в режиме редактирования файла '{context.user_data['editing_file']}'. "
                           f"Ожидался вызов handle_config_edit. Текст: {update.message.text[:100]}...")
            return # Явно выходим, чтобы не обрабатывать это сообщение как обычный текст

        user_id = update.message.from_user.id
        text = update.message.text
        user_data = context.user_data
        logger.info("Получен текст от user_id %s: %s, user_data: %s", user_id, text, user_data) # user_id теперь используется

        if not text:
            await update.message.reply_text("Пожалуйста, введите непустое значение")
            return

        # Обработка других текстовых вводов (Контрагент, Чек)
        if 'awaiting_input' in user_data:
            filter_type = user_data['awaiting_input']
            user_data['export_filters'][filter_type] = text
            del user_data['awaiting_input']
            await self.show_filters_menu(update, context)

    @admin_only
    async def handle_filter_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data.split('_', 1)  # Разделяем только на первую часть
        filter_type = data[0]
        value = data[1] if len(data) > 1 else ''
        
        # Для категории ищем оригинальное значение в базе
        if filter_type == 'cat':
            db = Database()
            try:
                categories = db.get_unique_values("category", query.from_user.id)
                # Ищем категорию, соответствующую safe_value
                safe_value = value
                original_value = next((cat for cat in categories if cat.replace(" ", "_").replace("'", "").replace('"', "")[:50] == safe_value), safe_value)
                context.user_data['export_filters']['category'] = original_value
            except Exception as e:
                logger.error("Ошибка при получении категорий: %s", e)
                await query.edit_message_text("❌ Ошибка при выборе категории.")
                return
            finally:
                db.close()
        elif filter_type == 'type':
            context.user_data['export_filters']['transaction_type'] = value
        elif filter_type == 'source':
            context.user_data['export_filters']['cash_source'] = value
        elif filter_type == 'class':
            context.user_data['export_filters']['transaction_class'] = value
        
        await self.show_filters_menu(update, context)

    @admin_only
    async def generate_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Генерация и отправка отчета"""
        query = update.callback_query
        await query.answer()
        
        user_data = context.user_data
        filters = user_data['export_filters']
        logger.debug("Генерация отчета с фильтрами: %s", filters)

        db_filters = {}
        for key in ['category', 'transaction_type', 'cash_source', 'counterparty', 'check_num', 'transaction_class']:
            if filters[key] != 'Все':
                db_filters[key] = filters[key]
        
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
                'category': 'Категория',
                'description': 'Описание',
                'counterparty': 'Контрагент',
                'check_num': 'Чек #',
                'transaction_type': 'Тип транзакции',
                'transaction_class': 'Класс',
                'target_amount': 'Сумма (куда)',
                'target_cash_source': 'Наличность (куда)'
            }
            
            # Переименовываем столбцы
            # df = df.rename(columns=column_mapping)
            df_renamed = df.rename(columns=column_mapping)
            logger.debug("Столбцы после переименования: %s", df.columns.tolist())
            
            with NamedTemporaryFile(suffix='.csv', delete=False, mode='w', encoding='utf-8') as tmp:
                df_renamed.to_csv(tmp.name, index=False, encoding='utf-8', sep=',')
                
            try:    
                await context.bot.send_document(
                    chat_id=query.from_user.id,
                    document=open(tmp.name, 'rb'),
                    caption=f"Отчет за {filters['start_date'].strftime('%d.%m.%Y')} - {filters['end_date'].strftime('%d.%m.%Y')}"
                )
                # os.unlink(tmp.name)  # Удаляем временный файл
            
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
                    'transaction_class': '📊 Класс'
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
                success_message = "✅ Отчет успешно сформирован."
                # Добавляем сводку по фильтрам, если она не пустая
                if filter_summary:
                    success_message += "\n\n<b>Примененные фильтры:</b>\n" + filter_summary

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

    @admin_only
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

    @admin_only
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
            keyboard.append([InlineKeyboardButton("Назад", callback_data='back_to_main')])
            
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

    @admin_only
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
                "Используйте /cancel для отмены."
            )
        }
        
        # Добавляем обработчик следующего сообщения
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.handle_pattern_input
        ))

    @admin_only
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

    @admin_only
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
                f"Паттерн '{pattern}' успешно добавлен в категорию '{category}'"
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

    @admin_only
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

    @admin_only
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

    @admin_only
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

    @admin_only
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
            "👋 Привет! Я бот для обработки банковских выписок и управления финансами.\n\n"
            "📌 <b>Основные возможности:</b>\n"
            "• Обработка PDF-выписок из банков (Tinkoff, Сбербанк, Яндекс и другие)\n"
            "• Автоматическая классификация транзакций по категориям\n"
            "• Настройка категорий и паттернов для распознавания\n"
            "• Управление конфигурацией прямо в чате\n"
            "• Просмотр логов работы бота\n\n"
            "📄 <b>Как работать с ботом:</b>\n"
            "1. Просто отправьте мне PDF-файл с банковской выпиской\n"
            "2. Я обработаю его и верну структурированные данные\n"
            "3. Для транзакций, которые не удалось классифицировать, будет отдельный файл\n\n"
            "⚙ <b>Дополнительные команды:</b>\n"
            "/config - Управление конфигурацией (категории, паттерны, таймауты)\n"
            "/add_pattern - Добавить новый паттерн для категории\n"
            "/settings - Показать текущие настройки\n"
            "/reset - Сбросить настройки к значениям по умолчанию\n\n"
            "<b>Примеры команд:</b>\n"
            "• <code>/add_pattern \"Еда\" \"VKUSVILL\"</code> - добавить паттерн для категории\n"
            "• <code>PDF: 1</code> - сохранить промежуточные файлы обработки\n"
            "• <code>Чек #: + НДС</code> - добавить текст ко всем чекам\n\n"
            "Обработка файла может занять несколько минут, пожалуйста, подождите."
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
            [InlineKeyboardButton("Просмотреть конфиг", callback_data='view_config')],
            [InlineKeyboardButton("Редактировать конфиг", callback_data='edit_config')],
            [InlineKeyboardButton("Добавить Категорию - Паттерн", callback_data='add_pattern_interactive')],
            [InlineKeyboardButton("Просмотреть логи", callback_data='view_logs')],
            [InlineKeyboardButton("Перезагрузить бота", callback_data='restart')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text(
            "Управление конфигурацией:",
            reply_markup=reply_markup
        )

    # Callback обработчики
    @admin_only
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

    @admin_only
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
            [InlineKeyboardButton("Назад", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Выберите конфигурационный файл для просмотра:",
            reply_markup=reply_markup
        )

    @admin_only
    async def edit_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает callback из меню редактирования"""
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

    @admin_only
    async def show_edit_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню выбора файла для редактирования"""
        query = update.callback_query
        await query.answer()
        
        keyboard = [
            [InlineKeyboardButton("Категории", callback_data='edit_categories')],
            [InlineKeyboardButton("Спец. условия", callback_data='edit_special')],
            [InlineKeyboardButton("PDF паттерны", callback_data='edit_pdf_patterns')],
            [InlineKeyboardButton("Таймауты", callback_data='edit_timeouts')],
            [InlineKeyboardButton("Отмена", callback_data='cancel')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Выберите файл для редактирования:",
            reply_markup=reply_markup
        )

    @admin_only
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
                
    @admin_only
    async def handle_config_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает текстовое редактирование конфига"""
        logger.debug("handle_config_edit: editing_file = %s", context.user_data.get('editing_file'))
        logger.debug("handle_config_edit: Получен текст от пользователя")
        # logger.info(f"Путь к файлу: {filepath}")
        # logger.info(f"Файл существует: {os.path.exists(filepath)}")
        # logger.info(f"Директория доступна для записи: {os.access(config_dir, os.W_OK)}")
        # logger.info(f"Файл доступен для записи: {os.path.exists(filepath) and os.access(filepath, os.W_OK)}")
        logger.info("handle_config_edit: Начало обработки текстового редактирования.") # Добавлено
        
        if 'editing_file' not in context.user_data:
            logger.warning("handle_config_edit: 'editing_file' не найден в user_data.") # Добавлено
            await update.message.reply_text("Не выбрано файл для редактирования")
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

    @admin_only
    async def handle_config_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает загрузку конфига файлом"""
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
    @admin_only
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

        logger.info(f"Получен файл: {document.file_name}")
        await update.message.reply_text("Начинаю обработку...")
        
        logger.info(f"Начата обработка PDF: {document.file_name}, размер: {document.file_size} байт")
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
                    files_to_send.append(unclassified_csv_path)
            
            # db = Database()
            # db.save_transactions(pd.read_csv(result_csv_path), update.effective_user.id)
            # db.close()

            # Отправка выбранных файлов
            for file_path in files_to_send:
                if file_path and os.path.exists(file_path):
                    caption = "Транзакции для ручной классификации" if file_path == unclassified_csv_path else None
                    with open(file_path, 'rb') as f:
                        await update.message.reply_document(document=f, caption=caption)

            # Сохраняем DataFrame во временное хранилище
            # context.user_data['pending_data'] = {
            #     'df': pd.read_csv(result_csv_path),
            #     'timestamp': time.time()
            # }
            
            df = pd.read_csv(
                result_csv_path,
                sep=';',          # Указываем разделитель
                quotechar='"',     # Символ кавычек
                encoding='utf-8',  # Кодировка
                on_bad_lines='warn' # Обработка битых строк
                )

            context.user_data['pending_data'] = {
                'df': df,
                'timestamp': time.time()  # Фиксируем время получения данных
            }
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

    @admin_only
    async def handle_save_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user_data = context.user_data
        
        if query.data == 'save_no':
            await query.edit_message_text("Данные не сохранены")
            
            if 'temp_files' in user_data:
                await self.cleanup_files(user_data['temp_files'])
                del user_data['temp_files']
            
            if 'pending_data' in user_data:
                del user_data['pending_data']
            return
        
        # Только для ответа "Да" продолжаем проверки
        pending_data = user_data.get('pending_data', {})
        
        if not pending_data or 'timestamp' not in pending_data or 'df' not in pending_data:
            await query.edit_message_text("Данные для сохранения не найдены или повреждены")
            return
            
        if time.time() - pending_data['timestamp'] > 300:
            await query.edit_message_text("Время подтверждения истекло (максимум 5 минут)")
            return

        logger.info("Сохранение данных в БД: %s", pending_data['df'][['Дата']].head().to_dict())
        db = None
        try:
            db = Database()
            stats = db.save_transactions(pending_data['df'], query.from_user.id)
            
            logger.info(
                f"Сохранено: новых - {stats['new']}, дубликатов - {stats['duplicates']}"
            )
            
            if stats['duplicates'] > 0:
                context.user_data['pending_duplicates'] = stats['duplicates_list']
                keyboard = [
                    [InlineKeyboardButton("Обновить дубликаты ✅", callback_data='update_duplicates')],
                    [InlineKeyboardButton("Пропустить ❌", callback_data='skip_duplicates')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    f"Найдено {stats['duplicates']} дубликатов. Обновить записи?",
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

    @admin_only
    async def handle_duplicates_decision(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user_data = context.user_data
        duplicates = user_data.get('pending_duplicates', [])
        
        if not duplicates:
            await query.edit_message_text("Нет данных для обновления")
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


    @admin_only
    async def cleanup_files(self, file_paths):
        for path in file_paths:
            if path and os.path.exists(path) and os.path.isfile(path):
                try:
                    await asyncio.to_thread(os.unlink, path)
                    logger.info(f"Удален временный файл: {path}")
                except Exception as e:
                    logger.error(f"Ошибка удаления {path}: {e}")

    @admin_only
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
                InlineKeyboardButton("Последние 100 строк", callback_data=f'logview_text_{filename}'),
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

    @admin_only
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
                # Читаем последние 100 строк
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()[-100:]
                    content = ''.join(lines)
                
                # Очищаем текст от проблемных символов
                content = self.sanitize_log_content(content)
                
                # Разбиваем на части если слишком длинное
                if len(content) > 4000:
                    parts = [content[i:i+4000] for i in range(0, len(content), 4000)]
                    for part in parts:
                        try:
                            await query.message.reply_text(
                                f"Последние 100 строк из {filename}:\n<pre>{part}</pre>",
                                parse_mode='HTML'
                            )
                        except Exception:
                            await query.message.reply_text(
                                f"Последние 100 строк из {filename}:\n{part}"
                            )
                        await asyncio.sleep(0.5)
                else:
                    try:
                        await query.message.reply_text(
                            f"Последние 100 строк из {filename}:\n<pre>{content}</pre>",
                            parse_mode='HTML'
                        )
                    except Exception:
                        await query.message.reply_text(
                            f"Последние 100 строк из {filename}:\n{content}"
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

    @admin_only
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

    @admin_only
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