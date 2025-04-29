import psycopg2
from datetime import datetime

class Database:
    def __init__(self):
        self.conn = psycopg2.connect(
            dbname="finance_bot",
            user="bot_user",
            password="secure_password",
            host="localhost"
        )
        self.cursor = self.conn.cursor()

    def save_transactions(self, df, user_id):
        # Получаем следующий import_id
        self.cursor.execute("SELECT nextval('import_id_seq')")
        import_id = self.cursor.fetchone()[0]
        
        # Вставляем данные
        for _, row in df.iterrows():
            try:
                self.cursor.execute("""
                    INSERT INTO transactions (
                        import_id, user_id, transaction_date, amount,
                        cash_source, category, description, counterparty,
                        CHECK_NUM, transaction_type
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (transaction_date, cash_source, amount) DO NOTHING
                """, (
                    import_id, user_id, row['Дата'], float(row['Сумма']),
                    row['Наличность'], row['Категория'], row['Описание'],
                    row['Контрагент'], row['Чек #'], row['Тип транзакции']
                ))
            except Exception as e:
                print(f"Ошибка вставки: {e}")
        self.conn.commit()

    def get_transactions(self, user_id, start_date, end_date, filters=None):
        query = """
            SELECT * FROM transactions 
            WHERE user_id = %s 
            AND transaction_date BETWEEN %s AND %s
        """
        params = [user_id, start_date, end_date]
        
        if filters:
            query += " AND " + " AND ".join([f"{k} = %s" for k in filters.keys()])
            params.extend(filters.values())
        
        self.cursor.execute(query, params)
        columns = [desc[0] for desc in self.cursor.description]
        return pd.DataFrame(self.cursor.fetchall(), columns=columns)

    def close(self):
        self.conn.close()