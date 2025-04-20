import os
import logging
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
from classify_transactions_pdf import classify_transactions

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%d-%m-%Y %H:%M:%S',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

def load_timeouts(config_path: str = None) -> Dict[str, int]:
    """Загружает конфигурацию таймаутов из YAML-файла"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'config', 'timeouts.yaml')
    with open(config_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)['timeouts']

class TransactionProcessorBot:
    def __init__(self, token: str):
        self._is_restarting = False  # Флаг перезагрузки
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
            pattern='^(view_categories|view_special|view_timeouts|view_all|back_to_main)$'
        ))

    def setup_handlers(self):
        """Настройка всех обработчиков команд"""
        # Основные команды
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("config", self.show_config_menu))
        self.application.add_handler(CommandHandler("restart", self.restart_bot))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        
        # Обработчики callback-запросов
        self.application.add_handler(CallbackQueryHandler(
            self.main_menu_callback,
            pattern='^(view_config|edit_config|restart)$'
        ))
        self.application.add_handler(CallbackQueryHandler(
            self.edit_menu_callback,
            pattern='^(edit_categories|edit_special|edit_timeouts|cancel)$'
        ))
        
        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)

    async def config_selection_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор конфига для просмотра"""
        query = update.callback_query
        await query.answer()
        
        config_map = {
            'view_categories': 'categories.yaml',
            'view_special': 'special_conditions.yaml',
            'view_timeouts': 'timeouts.yaml'
        }
        
        if query.data == 'back_to_main':
            await self.show_config_menu(update)  # Передаем update вместо query.message
            return
        elif query.data == 'view_all':
            await self.send_all_config_files(query)
            return
        
        filename = config_map[query.data]
        await self.send_single_config_file(query, filename)

    async def send_single_config_file(self, query, filename):
        """Отправляет один выбранный конфигурационный файл"""
        config_dir = os.path.join(os.path.dirname(__file__), 'config')
        filepath = os.path.join(config_dir, filename)
        
        descriptions = {
            'categories.yaml': 'Категории транзакций',
            'special_conditions.yaml': 'Специальные условия',
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
            'timeouts.yaml': 'Таймауты обработки'
        }
        
        for filename, description in config_files.items():
            await self.send_single_config_file(query, filename)
            await asyncio.sleep(0.5)  # Небольшая задержка между отправками

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Логирует ошибки и уведомляет пользователя"""
        logger.error("Исключение при обработке запроса:", exc_info=context.error)
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.answer("Произошла ошибка, попробуйте позже")

    # Основные команды
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        await update.message.reply_text(
            "Привет! Я бот для обработки банковских выписок.\n"
            "Отправьте мне файл в формате PDF, и я обработаю его для вас.\n"
            "Обработка может занять несколько минут, пожалуйста, подождите."
        )

    async def show_config_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        """Показывает меню управления конфигурацией"""
        # Получаем сообщение из update или callback_query
        if hasattr(update, 'message'):
            message = update.message
        elif hasattr(update, 'callback_query') and update.callback_query.message:
            message = update.callback_query.message
        else:
            message = update  # если передано сообщение напрямую
        
        keyboard = [
            [InlineKeyboardButton("Просмотреть конфиг", callback_data='view_config')],
            [InlineKeyboardButton("Редактировать конфиг", callback_data='edit_config')],
            [InlineKeyboardButton("Перезагрузить бота", callback_data='restart')]
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
            await self.show_config_selection(query)  # Изменено: теперь показываем меню выбора
        elif query.data == 'edit_config':
            await self.show_edit_menu(query)
        elif query.data == 'restart':
            await self.restart_bot(update, context)

    async def show_config_selection(self, query):
        """Показывает меню выбора конфига для просмотра"""
        keyboard = [
            [InlineKeyboardButton("Категории", callback_data='view_categories')],
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
            'edit_timeouts': 'timeouts.yaml'
        }
        
        filename = config_map[query.data]
        context.user_data['editing_file'] = filename
        await query.edit_message_text(
            text=f"Отправьте новое содержимое файла {filename} в виде текста "
                 "или файлом YAML. Используйте /cancel для отмены."
        )
        # Активируем обработчики редактирования
        for handler in self.config_handlers:
            self.application.add_handler(handler)

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
                
    async def handle_config_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает текстовое редактирование конфига"""
        if 'editing_file' not in context.user_data:
            await update.message.reply_text("Не выбрано файл для редактирования")
            return
            
        filename = context.user_data['editing_file']
        new_content = update.message.text
        
        try:
            yaml.safe_load(new_content)
            filepath = os.path.join('config', filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            await update.message.reply_text(f"Файл {filename} успешно обновлен!")
            self.remove_config_handlers()
            del context.user_data['editing_file']
        except yaml.YAMLError as e:
            await update.message.reply_text(f"Ошибка в YAML: {str(e)}\nПопробуйте еще раз")

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
            file = await document.get_file()
            downloaded_file = await file.download_to_drive()
            
            with open(downloaded_file, 'r', encoding='utf-8') as f:
                content = f.read()
                yaml.safe_load(content)
            
            os.replace(downloaded_file, os.path.join('config', filename))
            await update.message.reply_text(f"Файл {filename} успешно обновлен!")
            self.remove_config_handlers()
            del context.user_data['editing_file']
        except yaml.YAMLError as e:
            await update.message.reply_text(f"Ошибка в YAML: {str(e)}\nПопробуйте еще раз")
            if os.path.exists(downloaded_file):
                os.unlink(downloaded_file)
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {str(e)}")
            if os.path.exists(downloaded_file):
                os.unlink(downloaded_file)

    def remove_config_handlers(self):
        """Удаляет обработчики редактирования конфига"""
        for handler in self.config_handlers:
            self.application.remove_handler(handler)

    # Обработка документов
    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает полученный документ"""
        document = update.message.document

        if not document.file_name.lower().endswith('.pdf'):
            await update.message.reply_text("Пожалуйста, отправьте файл в формате PDF.")
            return

        logger.info(f"Получен файл: {document.file_name}, размер: {document.file_size} байт")
        await update.message.reply_text("Начинаю обработку выписки. Это может занять несколько минут...")

        tmp_pdf_path = None
        temp_csv_path = None
        combined_csv_path = None
        result_csv_path = None

        try:
            # Скачиваем файл
            pdf_file = BytesIO()
            file = await document.get_file(read_timeout=self.download_timeout)
            await file.download_to_memory(out=pdf_file, read_timeout=self.download_timeout)
            pdf_file.seek(0)

            # Сохраняем временный файл
            with NamedTemporaryFile(suffix='.pdf', delete=False) as tmp_pdf:
                tmp_pdf.write(pdf_file.read())
                tmp_pdf_path = tmp_pdf.name

            # Обработка PDF
            temp_csv_path, pdf_type = await asyncio.wait_for(
                asyncio.to_thread(extract_pdf1, tmp_pdf_path),
                timeout=self.processing_timeout
            )

            combined_csv_path = await asyncio.wait_for(
                asyncio.to_thread(extract_pdf2, temp_csv_path, pdf_type),  # Передаем тип PDF
                timeout=self.processing_timeout
            )

            result_csv_path = await asyncio.wait_for(
                asyncio.to_thread(classify_transactions, combined_csv_path, pdf_type), # Передаем тип PDF
                timeout=self.processing_timeout
            )

            # Отправляем результат
            with open(result_csv_path, 'rb') as result_file:
                await update.message.reply_document(
                    document=result_file,
                    caption="Вот ваш обработанный файл с транзакциями",
                    read_timeout=self.download_timeout,
                    write_timeout=self.download_timeout
                )
        except asyncio.TimeoutError:
            await update.message.reply_text(
                "Обработка файла заняла слишком много времени. "
                "Пожалуйста, попробуйте снова или отправьте файл меньшего размера."
            )
        except Exception as e:
            logger.error(f"Ошибка обработки файла: {e}", exc_info=True)
            await update.message.reply_text(
                "Произошла ошибка при обработке файла. Пожалуйста, убедитесь, что файл корректный."
            )
        finally:
            await self.cleanup_files([
                tmp_pdf_path,
                temp_csv_path,
                combined_csv_path,
                result_csv_path
            ])

    async def cleanup_files(self, file_paths):
        """Удаляет временные файлы"""
        for path in file_paths:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                    await asyncio.sleep(self.delay_between_operations)
                except Exception as e:
                    logger.error(f"Ошибка при удалении файла {path}: {e}")

    # Перезагрузка бота
    async def restart_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Перезапускает бота"""
        try:
            # Получаем query из update
            query = update.callback_query if hasattr(update, 'callback_query') else None
            
            if query:
                await query.answer()
                await query.edit_message_text(text="Перезапуск бота...")
            else:
                if update.message:
                    await update.message.reply_text("Перезапуск бота...")
            
            # Планируем перезапуск через 1 секунду
            asyncio.create_task(self.delayed_restart())
            
        except Exception as e:
            logger.error(f"Ошибка при перезагрузке: {e}")
            if query:
                await query.edit_message_text(text=f"Ошибка при перезагрузке: {e}")

    async def delayed_restart(self):
        """Отложенный перезапуск бота"""
        await asyncio.sleep(1)  # Даем время для ответа пользователю
        
        # Останавливаем текущий экземпляр
        await self.shutdown()
        
        # Запускаем новый процесс
        TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
        if not TOKEN:
            logger.error("Не удалось получить токен бота")
            return
        
        # Используем subprocess для чистого перезапуска
        import sys
        import subprocess
        subprocess.Popen([sys.executable, __file__])
        
        # Завершаем текущий процесс
        os._exit(0)

    async def shutdown(self):
        """Останавливает бота"""
        logger.info("Ожидание завершения задач...")
        
        if self.application.running:
            # Останавливаем polling
            await self.application.stop()
            # Даем время на завершение
            await asyncio.sleep(1)
            # Завершаем работу
            await self.application.shutdown()
        
        logger.info("Все задачи завершены.")

    def run(self):
        """Запускает бота с обработкой ошибок"""
        try:
            # Проверяем, не выполняется ли уже перезапуск
            if hasattr(self, '_is_restarting') and self._is_restarting:
                logger.warning("Попытка запуска во время перезагрузки")
                return

            logger.info("Запуск бота...")
            self.application.run_polling(
                poll_interval=2.0,
                timeout=self.request_timeout,
                close_loop=False  # Важно для корректного перезапуска
            )
        except KeyboardInterrupt:
            logger.info("Остановка бота по запросу пользователя...")
            asyncio.run(self.shutdown())
        except Exception as e:
            logger.error(f"Критическая ошибка при работе бота: {e}", exc_info=True)
            
            # Попытка аварийного завершения
            try:
                asyncio.run(self.shutdown())
            except Exception as shutdown_error:
                logger.error(f"Ошибка при аварийном завершении: {shutdown_error}")
            
            # Перезапуск через 5 секунд (опционально)
            logger.info("Попытка перезапуска через 5 секунд...")
            time.sleep(5)
            self._restart_async()
        
def docker_healthcheck():
    """Простая проверка здоровья для Docker"""
    try:
        # Можно добавить реальные проверки работоспособности
        return True
    except Exception:
        return False

if os.getenv('DOCKER_MODE'):
    logger.info("Бот запущен в Docker-контейнере")
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
        logger.info("Запуск бота...")
        bot.run()
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
        sys.exit(1)