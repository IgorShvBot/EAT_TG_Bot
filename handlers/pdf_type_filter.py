from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
from handlers.utils import ADMIN_FILTER
from db.base import DBConnection
from db.transactions import get_unique_values

# единственное состояние
PDF_TYPE = 0

# сюда при регистрации запишем функцию show_filters_menu из bot.py
_show_filters_menu = None


async def ask_pdf_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: показываем список типов PDF."""
    query = update.callback_query
    await query.answer()
    # Запоминаем, откуда зашли — в export или в edit_mode
    # Если есть флаг edit_mode, значит мы правим запись, иначе формируем отчёт
    context.user_data['pdf_filter_origin'] = 'edit' if context.user_data.get('edit_mode') else 'export'
 
    user_id = query.from_user.id

    try:
        with DBConnection() as db:
            pdf_types = ["Все"] + get_unique_values("pdf_type", user_id=user_id, db=db)

    except Exception:
        await query.edit_message_text("❌ Не удалось загрузить типы PDF.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(t, callback_data=f"pdf_{t}")]
        for t in pdf_types
    ]
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back_to_filters")])

    await query.edit_message_text(
        "Выберите тип PDF для фильтрации:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PDF_TYPE


async def receive_pdf_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 2: получили выбор пользователя, сохранили его и вернулись в меню фильтров."""
    query = update.callback_query
    await query.answer()

    # распарсим callback_data = 'pdf_<значение>'
    _, raw = query.data.split("_", 1)
    origin = context.user_data.pop('pdf_filter_origin', 'export')
    # Сохраняем в нужный словарь и выставляем флаг edit_mode у show_filters_menu
    if origin == 'edit':
        # редактирование записи
        filters = context.user_data['edit_mode'].setdefault('edit_filters',
                                                           context.user_data.get('export_filters', {}).copy())
        edit_flag = True
    else:
        # экспорт отчёта
        filters = context.user_data.setdefault('export_filters', {})
        # если вдруг был установлен старый edit_mode — сбросим его
        context.user_data.pop('edit_mode', None)
        edit_flag = False

    filters['pdf_type'] = None if raw in ("Все", "Все типы") else raw

    # Возвращаемся к нужному меню
    await _show_filters_menu(update, context, edit_mode=edit_flag)
    return ConversationHandler.END


async def cancel_pdf_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: просто вернуться в меню фильтров без изменений."""
    query = update.callback_query
    await query.answer()
    await _show_filters_menu(update, context, edit_mode=True)
    return ConversationHandler.END


def register_pdf_type_handler(application, show_filters_menu_func):
    """
    Регистрирует ConversationHandler для PDF‐фильтра.
    show_filters_menu_func — это метод TransactionProcessorBot.show_filters_menu
    """
    global _show_filters_menu
    _show_filters_menu = show_filters_menu_func

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ask_pdf_type, pattern="^set_pdf_type$"),
        ],
        states={
            PDF_TYPE: [
                CallbackQueryHandler(receive_pdf_type, pattern="^pdf_"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_pdf_type, pattern="^back_to_filters$"),
        ],
        per_user=True,
        per_chat=True,
    )
    application.add_handler(conv)


def make_pdf_type_button(filters: dict | None = None) -> InlineKeyboardButton:
    """
    Кнопка «Тип PDF» для главного меню фильтров.
    Всегда показывает выбранное значение, даже если это «Все».
    """
    # Вытащим текущее значение, по-умолчанию — Все
    raw = None
    if filters:
        raw = filters.get("pdf_type")
    # Если выбор не задан или явно None — считаем «Все»
    value = raw if raw not in (None, "") else "Все"

    # Формируем текст: «Тип PDF: значение»
    text = f"Тип PDF: {value}"
    return InlineKeyboardButton(text, callback_data="set_pdf_type")