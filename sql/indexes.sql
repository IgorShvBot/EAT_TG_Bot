-- Индексы для ускорения поиска
CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_transactions_cash_source ON transactions(cash_source);
CREATE INDEX IF NOT EXISTS idx_transactions_import_id ON transactions(import_id);
-- Составной индекс для нового запроса /max_dates и других запросов по типу и дате
CREATE INDEX IF NOT EXISTS idx_transactions_user_type_date ON transactions(user_id, pdf_type, transaction_date DESC);
-- Составной индекс для ускорения запросов get_transactions с фильтрацией по пользователю и дате
CREATE INDEX IF NOT EXISTS idx_transactions_user_date_desc ON transactions(user_id, transaction_date DESC);
CREATE INDEX IF NOT EXISTS trgm_idx_transactions_description ON transactions USING gin (description gin_trgm_ops);