__version__ = "2.1.0"

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

# Настройка логирования
def setup_logging():
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    date_format = '%d-%m-%Y %H:%M:%S' #%z'
    
    # Логи в консоль
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))
    
    # Логи в файл (если нужно)
    if not os.path.exists('logs'):
        os.makedirs('logs')
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
    logger.setLevel(logging.INFO)
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    # Дополнительные настройки для конкретных логгеров
    logging.getLogger('httpx').setLevel(logging.WARNING)  # Уменьшаем логи httpx
    logging.getLogger('telegram').setLevel(logging.INFO)  # Настраиваем логи telegram
    
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
    """Декоратор для ограничения доступа только админам"""
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ALLOWED_USERS:
            logger.warning(f"Попытка доступа от неавторизованного пользователя: {user_id}")
            if update.message:
                await update.message.reply_text("🚫 Доступ запрещен. Вы не авторизованы для использования этого бота.")
            elif update.callback_query:
                await update.callback_query.answer("Доступ запрещен")
            return
        return await func(self, update, context)
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
    
    lines = [line.strip() for line in message_text.split('\n') if line.strip()]
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
        
        # Обработчики для редактирования конфига
        self.config_handlers = [
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_config_edit),
            MessageHandler(filters.Document.ALL, self.handle_config_upload)
        ]

        self.application.add_handler(CallbackQueryHandler(
            self.config_selection_callback,
            pattern='^(view_categories|view_special|view_pdf_patterns|view_timeouts|view_all|back_to_main)$'
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
        self.application.add_handler(CommandHandler("settings", self.show_settings))
        self.application.add_handler(CommandHandler("reset", self.reset_settings))
        
        # Обработчики сообщений и документов (только для админов)
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.handle_settings
        ))
        
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

        self.application.add_handler(CommandHandler("cancel", self.cancel_operation))

        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)
    
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
    async def view_logs_callback(self, query):
        """Показывает меню выбора логов"""
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
            await self.show_config_selection(query)  # Изменено: теперь показываем меню выбора
        elif query.data == 'edit_config':
            await self.show_edit_menu(query)
        elif query.data == 'view_logs':
            await self.view_logs_callback(query)
        elif query.data == 'restart':
            await self.restart_bot(update, context)

    @admin_only
    async def show_config_selection(self, query):
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
            'edit_timeouts': 'timeouts.yaml'
        }
        
        filename = config_map.get(query.data)
        if not filename:
            await query.edit_message_text("Неизвестный тип конфига")
            return
        
        context.user_data['editing_file'] = filename
        await query.edit_message_text(
            text=f"Отправьте новое содержимое файла {filename} в виде текста "
                "или файлом YAML. Используйте /cancel для отмены."
        )
        # Активируем обработчики редактирования
        for handler in self.config_handlers:
            self.application.add_handler(handler)

    @admin_only
    async def show_edit_menu(self, query):
        """Показывает меню выбора файла для редактирования"""
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

    # Работа с конфигурационными файлами
    # async def send_config_files(self, query):
    #     """Отправляет текущие конфигурационные файлы"""
    #     config_dir = os.path.join(os.path.dirname(__file__), 'config')
    #     if not os.path.exists(config_dir):
    #         await query.message.reply_text("Папка config не найдена")
    #         return
        
    #     config_files = {
    #         'categories.yaml': 'Категории транзакций',
    #         'special_conditions.yaml': 'Специальные условия',
    #         'timeouts.yaml': 'Таймауты обработки'
    #     }
        
    #     for filename, description in config_files.items():
    #         filepath = os.path.join(config_dir, filename)
    #         if os.path.exists(filepath):
    #             try:
    #                 with open(filepath, 'rb') as f:
    #                     await query.message.reply_document(
    #                         document=f,
    #                         caption=f"{description} ({filename})"
    #                     )
    #                 await asyncio.sleep(1)
    #             except Exception as e:
    #                 logger.error(f"Ошибка при отправке файла {filename}: {e}")
    #                 await query.message.reply_text(f"Ошибка при отправке файла {filename}")
    #         else:
    #             await query.message.reply_text(f"Файл {filename} не найден")

    @admin_only
    async def send_config_files(self, query):
        """Отправляет содержимое конфигов как текстовые сообщения"""
        config_dir = os.path.join(os.path.dirname(__file__), 'config')
        
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
        if 'editing_file' not in context.user_data:
            await update.message.reply_text("Не выбрано файл для редактирования")
            return
            
        filename = context.user_data['editing_file']
        new_content = update.message.text
        
        try:
            # Проверяем валидность YAML
            parsed = yaml.safe_load(new_content)
            if not isinstance(parsed, dict):  # Проверяем, что это словарь
                raise yaml.YAMLError("Конфиг должен быть в формате YAML словаря")
            
            # Получаем абсолютный путь к файлу конфигурации
            config_dir = os.path.join(os.path.dirname(__file__), 'config')
            filepath = os.path.join(config_dir, filename)
            
            # Проверяем существование директории
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            
            # Сохраняем изменения
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            logger.info(f"Файл {filename} успешно обновлен по пути: {filepath}")
            await update.message.reply_text(f"Файл {filename} успешно обновлен!")
            
            # Удаляем обработчики редактирования
            self.remove_config_handlers()
            del context.user_data['editing_file']
            
        except yaml.YAMLError as e:
            logger.error(f"Ошибка в YAML: {str(e)}")
            await update.message.reply_text(f"Ошибка в YAML: {str(e)}\nПопробуйте еще раз")
        except Exception as e:
            logger.error(f"Ошибка при сохранении файла: {str(e)}")
            await update.message.reply_text(f"Ошибка при сохранении файла: {str(e)}")

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
            filepath = os.path.join(config_dir, filename)
            
            # Скачиваем временный файл
            file = await document.get_file()
            downloaded_file = await file.download_to_drive()
            
            # Проверяем валидность YAML
            with open(downloaded_file, 'r', encoding='utf-8') as f:
                content = f.read()
                yaml.safe_load(content)
            
            # Проверяем существование директории
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            
            # Сохраняем файл
            os.replace(downloaded_file, filepath)
            
            logger.info(f"Файл {filename} успешно обновлен по пути: {filepath}")
            await update.message.reply_text(f"Файл {filename} успешно обновлен!")
            
            # Удаляем обработчики редактирования
            self.remove_config_handlers()
            del context.user_data['editing_file']
            
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
                
    def remove_config_handlers(self):
        """Удаляет обработчики редактирования конфига"""
        for handler in self.config_handlers:
            self.application.remove_handler(handler)

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
            
            # Отправка выбранных файлов
            for file_path in files_to_send:
                if file_path and os.path.exists(file_path):
                    caption = "Транзакции для ручной классификации" if file_path == unclassified_csv_path else None
                    with open(file_path, 'rb') as f:
                        await update.message.reply_document(document=f, caption=caption)

        except Exception as e:
            logger.error(f"Ошибка обработки PDF: {str(e)}", exc_info=True)
            await update.message.reply_text(
                "Произошла ошибка при обработке файла. Пожалуйста, убедитесь, что:\n"
                "1. Это корректная банковская выписка\n"
                "2. Файл не поврежден\n"
                "3. Формат соответствует поддерживаемым (Tinkoff, Сбербанк, Яндекс)"
            )

        finally:
            if pdf_file:
                pdf_file.close()
                del pdf_file  # Явное освобождение памяти
            if tmp_pdf:
                tmp_pdf.close()

            await self.cleanup_files([
                tmp_pdf_path,
                temp_csv_path,
                combined_csv_path,
                result_csv_path,
                unclassified_csv_path
            ])

    @admin_only
    async def cleanup_files(self, file_paths):
        """Удаляет временные файлы"""
        for path in file_paths:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                    await asyncio.sleep(self.delay_between_operations)
                except Exception as e:
                    logger.error(f"Ошибка при удалении файла {path}: {e}")

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
            self.application.run_polling(
                poll_interval=2.0,
                timeout=self.request_timeout,
                close_loop=False,
                stop_signals=None
            )
        except Exception as e:
            logger.error(f"Ошибка при работе бота: {e}")
            if not self._in_docker:
                sys.exit(1)
        
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