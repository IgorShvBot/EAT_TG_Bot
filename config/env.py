import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMINS = set(map(int, filter(None, os.getenv("ADMINS", "").split(","))))
DOCKER_MODE = os.getenv("DOCKER_MODE") is not None
