# python backup_manual.py

import os
from database import Database

def main():
    try:
        # Инициализация подключения к БД
        db = Database()
        
        # Создание резервной копии
        # backup_file = db.create_backup()
        backup_file = db.create_backup(backup_dir="/Users/IgorShvyrkin/Documents/EAT_TG_Bot_Docker/backups")
        print(f"Резервная копия успешно создана: {backup_file}")
        
        # Закрытие соединения
        db.close()
    except Exception as e:
        print(f"Ошибка при создании резервной копии: {e}")

if __name__ == '__main__':
    main()