# syntax=docker/dockerfile:1.4

# Этап сборки (builder)
FROM python:3.10-slim AS builder

# Установка системных зависимостей с кешированием
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libpq-dev \
    libpoppler-cpp-dev \
    poppler-utils \
    postgresql-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Обновляем pip и основные инструменты сборки
# RUN pip install --upgrade pip setuptools wheel

# Установка Python-зависимостей с кешированием
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user -r requirements.txt

# Финальный образ
FROM python:3.10-slim

# Установка только runtime зависимостей
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt/lists \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    libpq-dev \
    libpoppler-cpp-dev \
    poppler-utils \
    postgresql-client && \
    rm -rf /var/lib/apt/lists/*

# Копирование установленных пакетов
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Настройка времени
ENV TZ=Europe/Moscow
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app
COPY . .

# Установка прав для директории конфигурации
# RUN chown -R 1000:1000 /app/config && chmod -R 775 /app/config

CMD ["python", "bot.py"]