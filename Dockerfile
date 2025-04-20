# FROM python:3.10
FROM python:3.10-slim

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libpoppler-cpp-dev \
    poppler-utils \
    ghostscript \
    libgl1 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*


# Устанавливаем временную зону
ENV TZ=Europe/Moscow
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt .

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

# Создаем директорию для конфигов
RUN mkdir -p /app/config

# Указываем переменные окружения
ENV PYTHONUNBUFFERED=1
ENV DOCKER_MODE=1

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import socket; socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM).bind('\0bank-bot-healthcheck')" || exit 1

CMD ["python", "bot.py"]

