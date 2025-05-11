-- Таблица транзакций
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    import_id INTEGER,
    user_id INTEGER NOT NULL,
    transaction_date TIMESTAMP NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    cash_source VARCHAR(100),
    category VARCHAR(100),
    description TEXT,
    counterparty VARCHAR(200),
    check_num VARCHAR(50),
    transaction_type VARCHAR(50),
    -- Новые поля:
    transaction_class VARCHAR(100),  -- Класс
    target_amount NUMERIC(12, 2),    -- Сумма (куда)
    target_cash_source VARCHAR(100), -- Наличность (куда)
    -- Для фиксации изменений в БД
    -- is_modified BOOLEAN DEFAULT FALSE,
    -- modified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT unique_transaction UNIQUE (user_id, transaction_date, cash_source, amount)
);

-- Для фиксации изменений в БД
ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS edited_by INTEGER REFERENCES users(id), -- Добавлено IF NOT EXISTS
  ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP,                   -- Добавлено IF NOT EXISTS
  ADD COLUMN IF NOT EXISTS edited_ids INTEGER[];                  -- Добавлено IF NOT EXISTS

-- Секвенция для импорта
CREATE SEQUENCE IF NOT EXISTS import_id_seq;