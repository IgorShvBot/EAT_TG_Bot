import os
import subprocess
import logging
from datetime import datetime, timedelta
from db.config import DB_CONFIG

logger = logging.getLogger(__name__)

def create_backup(backup_dir: str = None) -> str:
    """
    Создаёт резервную копию базы в формате PostgreSQL custom dump (.backup)

    Args:
        backup_dir: путь к папке с резервными копиями. Если не задан — используется .env BACKUP_DIR

    Returns:
        Путь к созданному файлу
    """
    backup_dir = backup_dir or os.getenv('BACKUP_DIR', os.path.join(os.path.dirname(__file__), 'backups'))

    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)

    backup_file = os.path.join(backup_dir, f"{datetime.now().strftime('%Y-%m-%d')}.backup")

    db_url = f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"

    try:
        cmd = [
            "pg_dump",
            f"--dbname={db_url}",
            "-Fc",
            "-f", backup_file
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        logger.info("Резервная копия создана: %s", backup_file)

        cleanup_old_backups(backup_dir)
        return backup_file
    except subprocess.CalledProcessError as e:
        logger.error("Ошибка создания резервной копии: %s", e.stderr)
        raise


def cleanup_old_backups(backup_dir: str, days: int = 30) -> None:
    """
    Удаляет резервные копии старше указанного количества дней

    Args:
        backup_dir: путь к папке с резервными копиями
        days: сколько дней хранить
    """
    cutoff_date = datetime.now() - timedelta(days=days)
    for file in os.listdir(backup_dir):
        file_path = os.path.join(backup_dir, file)
        if file.endswith(".backup"):
            try:
                file_date = datetime.strptime(file.split(".")[0], "%Y-%m-%d")
                if file_date < cutoff_date:
                    os.unlink(file_path)
                    logger.info("Удалена старая резервная копия: %s", file_path)
            except ValueError:
                continue
