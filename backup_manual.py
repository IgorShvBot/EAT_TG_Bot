# DB_HOST=localhost python backup_manual.py

import os
from db.backup import create_backup

def main():
    try:      
        # Создание резервной копии
        backup_file = create_backup(backup_dir="/Users/IgorShvyrkin/Documents/EAT_TG_Bot_Docker/backups")
        print(f"Резервная копия успешно создана: {backup_file}")
        
    except Exception as e:
        print(f"Ошибка при создании резервной копии: {e}")

if __name__ == '__main__':
    main()