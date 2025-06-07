# Утилиты и константы
# — общие «кусочки» (префиксы callback_data, форматирование дат, константы состояний) держите в handlers/utils.py и handlers/states.py.

from telegram.ext import filters
from config.env import ADMINS

# Глобальный фильтр, ограничивающий доступ к хендлерам только администраторам
ADMIN_FILTER = filters.User(ADMINS)