-- Индексы для ускорения поиска
CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_transactions_cash_source ON transactions(cash_source);
CREATE INDEX IF NOT EXISTS idx_transactions_import_id ON transactions(import_id);