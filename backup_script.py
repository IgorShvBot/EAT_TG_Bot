import os
import logging
from db.backup import create_backup
from dotenv import load_dotenv

# Настройка логирования
logger = logging.getLogger('backup')
logger.setLevel(logging.INFO)
logger.handlers = []  # Очищаем существующие обработчики

# Добавляем обработчики
stream_handler = logging.StreamHandler()
file_handler = logging.FileHandler('/app/logs/backup.log', mode='a')
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)
logger.addHandler(stream_handler)
logger.addHandler(file_handler)

if __name__ == '__main__':
    logger.info("Запуск скрипта резервного копирования")
    load_dotenv()
    
    # Проверка переменных окружения
    required_vars = ['DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST', 'DB_PORT', 'BACKUP_DIR']
    for var in required_vars:
        if not os.getenv(var):
            logger.error(f"Переменная окружения {var} не задана")
            exit(1)
        logger.info(f"{var}: {os.getenv(var)}")
    
    try:
        logger.info("Инициализация подключения к базе данных")
        logger.info("Создание резервной копии")
        backup_file = create_backup()
        logger.info(f"Резервная копия успешно создана: {backup_file}")
    except Exception as e:
        logger.error(f"Ошибка при создании резервной копии: {e}", exc_info=True)
        exit(1)