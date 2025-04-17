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

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import requests; exit(0 if requests.get('http://localhost:8080/health').status_code == 200 else 1)" || exit 1

CMD ["python", "bot.py"]






