from db.base import DBConnection
from db.schema import create_tables, create_indexes
from utils.logging import setup_logging

def main():
    setup_logging()
    with DBConnection() as db:
        create_tables(db.conn)
        create_indexes(db.conn)

if __name__ == "__main__":
    main()