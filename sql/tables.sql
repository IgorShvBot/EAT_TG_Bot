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
    check_num VARCHAR(200),
    transaction_type VARCHAR(50),
    -- Новые поля:
    transaction_class VARCHAR(100),  -- Класс
    target_amount NUMERIC(12, 2),    -- Сумма (куда)
    target_cash_source VARCHAR(100), -- Наличность (куда)
    created_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT unique_transaction UNIQUE (user_id, transaction_date, cash_source, amount)
);

-- Для фиксации изменений в БД
ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS edited_by INTEGER, -- Удалено REFERENCES users(id)
  ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP WITH TIME ZONE,
  ADD COLUMN IF NOT EXISTS edited_ids INTEGER[];

ALTER TABLE transactions 
  ADD COLUMN IF NOT EXISTS pdf_type VARCHAR(50);
  
-- Секвенция для импорта
CREATE SEQUENCE IF NOT EXISTS import_id_seq;