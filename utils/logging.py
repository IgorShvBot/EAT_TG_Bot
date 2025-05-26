import os
import sys
import glob
import logging
from datetime import datetime

LOG_DIR = "logs"
MAX_BACKUPS = 5

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%d-%m-%Y %H:%M:%S"


def setup_logging():
    """
    Инициализирует логирование:
    - Лог в консоль
    - Лог в файл вида logs/YYYY-MM-DD_bot.log
    - Удаление старых логов (оставляет последние MAX_BACKUPS)
    """
def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(LOG_DIR, f"{today_str}_bot.log")

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()  # удаляем старые (важно!)
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Подавляем шумные логи от httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    # print(f"[DEBUG] Лог-файл инициализирован: {log_path}")
    # root_logger.info(">>> Инициализация логгера завершена <<<")

    rotate_old_logs()


def rotate_old_logs():
    """
    Удаляет старые логи, если превышен MAX_BACKUPS
    """
    logs = sorted(glob.glob(os.path.join(LOG_DIR, "*_bot.log")))
    if len(logs) > MAX_BACKUPS:
        for old_log in logs[:-MAX_BACKUPS]:
            try:
                os.remove(old_log)
                logging.getLogger(__name__).debug("Удалён старый лог: %s", old_log)
            except Exception as e:
                logging.getLogger(__name__).warning("Не удалось удалить лог: %s [%s]", old_log, e)
