-- Таблица транзакций
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    import_id INT NOT NULL,
    user_id INT NOT NULL,
    transaction_date DATE NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    cash_source VARCHAR(50),
    category VARCHAR(100),
    description TEXT,
    counterparty VARCHAR(255),
    check_num VARCHAR(50),
    transaction_type VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Секвенция для импорта
CREATE SEQUENCE IF NOT EXISTS import_id_seq;