import os
import logging
import pandas as pd
from datetime import datetime
from tempfile import NamedTemporaryFile

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)
from handlers.utils import ADMIN_FILTER
from telegram.error import BadRequest
from telegram_bot_calendar import DetailedTelegramCalendar, LSTEP

from db.base import DBConnection
from db.transactions import get_transactions, get_last_import_ids, get_unique_values
from handlers.filters import get_default_filters
from handlers.pdf_type_filter import make_pdf_type_button


logger = logging.getLogger(__name__)
EXPORT_FILTER_KEYS = ["category", "transaction_type", "cash_source", "transaction_class", "pdf_type", "import_id"]


def build_filters_keyboard(filters: dict, edit_mode: bool = False) -> InlineKeyboardMarkup:
    """
    Генерирует клавиатуру фильтров с учетом режима: экспорт или редактирование.
    """
    keyboard = [
        [InlineKeyboardButton(f"📅 Дата начала: {filters['start_date']}", callback_data='set_start_date')],
        [InlineKeyboardButton(f"📅 Дата окончания: {filters['end_date']}", callback_data='set_end_date')],
        [InlineKeyboardButton(f"📦 ID импорта: {filters.get('import_id', 'Все')}", callback_data='set_import_id')],
        [InlineKeyboardButton(f"🏷 Категория: {filters['category']}", callback_data='set_category')],
        [InlineKeyboardButton(f"🔀 Тип: {filters['transaction_type']}", callback_data='set_type')],
        [InlineKeyboardButton(f"💳 Наличность: {filters['cash_source']}", callback_data='set_cash_source')],
        [InlineKeyboardButton(f"📝 Описание: {filters['description']}", callback_data='set_description')],
        [InlineKeyboardButton(f"👥 Контрагент: {filters['counterparty']}", callback_data='set_counterparty')],
        [InlineKeyboardButton(f"🧾 Чек: {filters['check_num']}", callback_data='set_check_num')],
        [InlineKeyboardButton(f"📊 Класс: {filters['transaction_class']}", callback_data='set_class')],
        [make_pdf_type_button(filters)],
    ]

    if edit_mode:
        keyboard += [
            [InlineKeyboardButton("➡️ К выбору полей", callback_data='edit_filter_proceed_to_fields')],
            [InlineKeyboardButton("✖️ Отмена", callback_data='cancel_edit')],
        ]
    else:
        keyboard += [
            [InlineKeyboardButton("📤 Сформировать отчет", callback_data='generate_report')],
            [InlineKeyboardButton("✖️ Отмена", callback_data='cancel_export')],
        ]

    return InlineKeyboardMarkup(keyboard)


async def show_filters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_mode: bool = False):
    """
    Показывает меню фильтров для экспорта или редактирования.
    """
    filters = context.user_data.setdefault("export_filters", {})
    text = "⚙️ Настройте параметры редактирования:" if edit_mode else "⚙️ Настройте параметры отчета:"
    reply_markup = build_filters_keyboard(filters, edit_mode=edit_mode)

    try:
        if update.callback_query:
            await update.callback_query.answer()
            # Безопасная попытка обновить текст и клавиатуру
            await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            # Повторно обновим только reply_markup
            try:
                await update.callback_query.edit_message_reply_markup(reply_markup=reply_markup)
            except Exception:
                pass  # suppress
        else:
            raise


async def handle_export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /export — запускает настройку фильтров.
    """
    context.user_data["edit_mode"] = False  # Явно выключаем режим редактирования
    context.user_data["export_filters"] = get_default_filters()
    await show_filters_menu(update, context)


async def set_export_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.removeprefix("set_")
    context.user_data["export_filters"][key] = "..."  # Placeholder
    await show_filters_menu(update, context)


async def export_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает экспорт с дефолтными фильтрами (дата с начала месяца, категории и т.д.)"""
    context.user_data["export_filters"] = get_default_filters()
    # context.user_data['export_filters'] = {
    #     'start_date': datetime.now().replace(day=1).strftime('%d.%m.%Y'),
    #     'end_date': datetime.now().strftime('%d.%m.%Y'),
    #     'category': 'Все',
    #     'transaction_type': 'Все',
    #     'cash_source': 'Все',
    #     'counterparty': 'Все',
    #     'check_num': 'Все',
    #     'transaction_class': 'Все',
    #     'description': 'Все',
    #     'pdf_type': 'Все',
    #     'import_id': 'Все'
    # }
    await show_filters_menu(update, context)


async def cancel_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие 'Отмена' при экспорте. Очищает фильтры и удаляет меню."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop('export_filters', None)
    await query.edit_message_text("ℹ️ Экспорт отменен")


async def generate_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Формирует CSV-отчет на основе установленных фильтров.
    """
    query = update.callback_query
    await query.answer("Формирую отчет...")

    filters = context.user_data.get("export_filters")
    if not filters:
        await query.edit_message_text("❌ Ошибка: фильтры экспорта не найдены")
        return

    try:
        # Преобразуем даты
        filters['start_date'] = datetime.strptime(filters['start_date'], "%d.%m.%Y")
        filters['end_date'] = datetime.strptime(filters['end_date'], "%d.%m.%Y")

        # Отфильтровываем значения
        db_filters = {
            k: v for k, v in filters.items()
            if v not in ("Все", None, "") or k in ["description", "counterparty", "check_num"]
        }

        with DBConnection() as db:
            df = get_transactions(
                user_id=query.from_user.id,
                start_date=filters['start_date'],
                end_date=filters['end_date'],
                filters=db_filters,
                db=db
            )

        if df.empty:
            await query.edit_message_text("⚠ По вашему запросу ничего не найдено")
            return

        df = df.fillna('').replace('NaN', '').astype(str)
        df['transaction_date'] = pd.to_datetime(df['transaction_date']).dt.strftime('%d.%m.%Y %H:%M')

        column_mapping = {
            'id': 'ID', 'transaction_date': 'Дата', 'amount': 'Сумма',
            'cash_source': 'Наличность', 'target_amount': 'Сумма (куда)',
            'target_cash_source': 'Наличность (куда)', 'category': 'Категория',
            'description': 'Описание', 'transaction_type': 'Тип транзакции',
            'counterparty': 'Контрагент', 'check_num': 'Чек #', 'transaction_class': 'Класс'
        }
        df.rename(columns=column_mapping, inplace=True)

        with NamedTemporaryFile(suffix='.csv', delete=False, mode='w', encoding='utf-8') as tmp:
            df.to_csv(tmp.name, index=False, sep=',')
            tmp_path = tmp.name

        applied_filters = format_filters(filters)

        await context.bot.send_document(
            chat_id=query.from_user.id,
            document=open(tmp_path, 'rb'),
            filename='report.csv',
            caption=f"Отчет за {filters['start_date'].strftime('%d.%m.%Y')} – {filters['end_date'].strftime('%d.%m.%Y')}\n"
                    f"📌 Записей: {len(df)}"
        )

        os.unlink(tmp_path)

        await query.edit_message_text(
            f"✅ Отчет успешно сформирован\n\n"
            f"⚙️ Примененные фильтры:\n{applied_filters}"
        )

    except Exception as e:
        logger.error("Ошибка генерации отчета: %s", e, exc_info=True)
        await query.edit_message_text("❌ Ошибка при формировании отчета. Попробуйте позже")


def format_filters(filters: dict) -> str:
    """
    Форматирует словарь фильтров в текст для отображения пользователю.
    """
    lines = []
    if 'start_date' in filters and 'end_date' in filters:
        lines.append(f"📅 Период: {filters['start_date'].strftime('%d.%m.%Y')} – {filters['end_date'].strftime('%d.%m.%Y')}")
    for key, label in [
        ('cash_source', '💳 Наличность'),
        ('target_cash_source', '💸 Куда'),
        ('category', '📂 Категория'),
        ('transaction_type', '🔄 Тип'),
        ('transaction_class', '🏷 Класс'),
        ('description', '📝 Описание'),
        ('counterparty', '👤 Контрагент'),
        ('check_num', '🔢 Чек #'),
        ('pdf_type', '📎 Тип PDF'),
        ('import_id', '🆔 ID импорта')
    ]:
        if filters.get(key) not in [None, '', 'Все']:
            lines.append(f"{label}: {filters[key]}")
    return "\n".join(lines)


async def handle_category_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Сохраняет выбранную категорию (callback_data вида 'cat_<value>') и возвращает в меню фильтров.
    """
    query = update.callback_query
    await query.answer()

    raw = query.data.removeprefix("cat_")            # e.g. "Домашние_расходы"
    safe_value = raw                                # например, "Домашние_расходы"

    # В каком словаре хранятся фильтры: edit_mode или export_filters
    edit_mode_active = (
        context.user_data.get('edit_mode') and
        context.user_data['edit_mode'].get('type') == 'edit_by_filter'
    )
    if edit_mode_active:
        filters_storage = context.user_data['edit_mode'].setdefault('edit_filters', get_default_filters())
    else:
        filters_storage = context.user_data.setdefault('export_filters', get_default_filters())

    try:
        # Получаем оригинальное название категории из БД
        with DBConnection() as db:
            categories = get_unique_values('category', user_id=query.from_user.id, db=db)
        original_value = next(
            (cat for cat in categories
             if cat.replace(" ", "_").replace("'", "").replace('"', "")[:50] == safe_value),
            safe_value
        )
        filters_storage['category'] = original_value
    except Exception as e:
        logger.error("Ошибка при выборе категории: %s", e)
        await query.edit_message_text("❌ Ошибка при выборе категории.")
        return

    await show_filters_menu(update, context, edit_mode=edit_mode_active)


async def handle_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Сохраняет выбранный transaction_type (callback_data вида 'type_<value>') и возвращает в меню фильтров.
    """
    query = update.callback_query
    await query.answer()

    raw = query.data.removeprefix("type_")      # e.g. "Перевод_на_счёт"
    transaction_type = raw.replace("_", " ")    # "Перевод на счёт"

    edit_mode_active = (
        context.user_data.get('edit_mode') and
        context.user_data['edit_mode'].get('type') == 'edit_by_filter'
    )
    if edit_mode_active:
        filters_storage = context.user_data['edit_mode'].setdefault('edit_filters', get_default_filters())
    else:
        filters_storage = context.user_data.setdefault('export_filters', get_default_filters())

    filters_storage['transaction_type'] = transaction_type
    await show_filters_menu(update, context, edit_mode=edit_mode_active)


async def handle_source_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Сохраняет выбранный cash_source (callback_data вида 'source_<value>') и возвращает в меню фильтров.
    """
    query = update.callback_query
    await query.answer()

    raw = query.data.removeprefix("source_")   # e.g. "Tinkoff"
    cash_source = raw.replace("_", " ")        # можно оставить без изменений, если в БД нет пробелов

    edit_mode_active = (
        context.user_data.get('edit_mode') and
        context.user_data['edit_mode'].get('type') == 'edit_by_filter'
    )
    if edit_mode_active:
        filters_storage = context.user_data['edit_mode'].setdefault('edit_filters', get_default_filters())
    else:
        filters_storage = context.user_data.setdefault('export_filters', get_default_filters())

    filters_storage['cash_source'] = cash_source
    await show_filters_menu(update, context, edit_mode=edit_mode_active)


async def handle_class_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Сохраняет выбранный transaction_class (callback_data вида 'class_<value>') и возвращает в меню фильтров.
    """
    query = update.callback_query
    await query.answer()

    raw = query.data.removeprefix("class_")       # e.g. "Личные_расходы"
    transaction_class = raw.replace("_", " ")      # "Личные расходы"

    edit_mode_active = (
        context.user_data.get('edit_mode') and
        context.user_data['edit_mode'].get('type') == 'edit_by_filter'
    )
    if edit_mode_active:
        filters_storage = context.user_data['edit_mode'].setdefault('edit_filters', get_default_filters())
    else:
        filters_storage = context.user_data.setdefault('export_filters', get_default_filters())

    filters_storage['transaction_class'] = transaction_class
    await show_filters_menu(update, context, edit_mode=edit_mode_active)


async def set_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    calendar, step = DetailedTelegramCalendar(locale='ru').build()
    await query.edit_message_text(
        text=f"📅 Выберите дату начала ({LSTEP[step]}):",
        reply_markup=calendar
    )
    context.user_data["calendar_context"] = "start_date"

async def set_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    calendar, step = DetailedTelegramCalendar(locale='ru').build()
    await query.edit_message_text(
        text=f"📅 Выберите дату окончания ({LSTEP[step]}):",
        reply_markup=calendar
    )
    context.user_data["calendar_context"] = "end_date"

async def set_import_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    from_user_id = query.from_user.id
    try:
        with DBConnection() as db:
            import_ids = get_last_import_ids(user_id=from_user_id, limit=10, db=db)

        keyboard = [[InlineKeyboardButton("Все", callback_data="import_id_Все")]]
        for import_id, created_at, pdf_type in import_ids:
            label = f"#{import_id} ({created_at.strftime('%d.%m.%Y %H:%M')}"
            if pdf_type:
                label += f", {pdf_type}"
            label += ")"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"import_id_{import_id}")])
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back_to_filters")])

        await query.edit_message_text(
            "Выберите ID импорта:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Ошибка при загрузке ID импорта: {e}")
        await query.edit_message_text("❌ Не удалось загрузить список ID импорта")

async def set_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        with DBConnection() as db:
            categories = ['Все'] + get_unique_values('category', user_id=query.from_user.id, db=db)

        keyboard = []
        for cat in categories:
            # Заменяем пробелы и кавычки, обрезаем длину до 50 символов
            safe_name = cat.replace(" ", "_").replace("'", "").replace('"', "")[:30]
            callback_data = f"cat_{safe_name}"
            keyboard.append([InlineKeyboardButton(cat, callback_data=callback_data)])

        # Кнопка «Назад» возвращает в главное меню фильтров
        keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back_to_filters")])
        await query.edit_message_text("Выберите категорию:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Ошибка загрузки категорий: {e}")
        await query.edit_message_text("❌ Ошибка при загрузке категорий.")

async def set_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    with DBConnection() as db:
        types = ['Все'] + get_unique_values('transaction_type', user_id=query.from_user.id, db=db)
    keyboard = [[InlineKeyboardButton(t, callback_data=f"type_{t}")] for t in types]
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back_to_filters")])
    await query.edit_message_text("Выберите тип транзакции:", reply_markup=InlineKeyboardMarkup(keyboard))

async def set_cash_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    with DBConnection() as db:
        sources = ['Все'] + get_unique_values('cash_source', user_id=query.from_user.id, db=db)
    keyboard = [
        [InlineKeyboardButton(src, callback_data=f"source_{src}") for src in sources[i:i+2]]
        for i in range(0, len(sources), 2)
    ]
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back_to_filters")])
    await query.edit_message_text("Выберите источник средств:", reply_markup=InlineKeyboardMarkup(keyboard))

async def set_description_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите текст для фильтрации по описанию (или 'Все' для сброса фильтра):\n"
        "ℹ️ Будет выполнен поиск по частичному совпадению без учета регистра."
    )
    context.user_data['awaiting_input'] = 'description'

async def set_counterparty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите имя контрагента или часть названия:")
    context.user_data['awaiting_input'] = 'counterparty'

async def set_check_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите номер чека или часть номера:")
    context.user_data['awaiting_input'] = 'check_num'

async def set_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    with DBConnection() as db:
        classes = ['Все'] + get_unique_values('transaction_class', user_id=query.from_user.id, db=db)
    keyboard = [
        [InlineKeyboardButton(cls, callback_data=f"class_{cls}") for cls in classes[i:i+3]]
        for i in range(0, len(classes), 3)
    ]
    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="back_to_filters")])
    await query.edit_message_text("Выберите класс транзакции:", reply_markup=InlineKeyboardMarkup(keyboard))


def register_export_handlers(application):
    """
    Регистрирует все хендлеры, связанные с экспортом отчёта.
    """
    # application.add_handler(CommandHandler("export", export_start))
    application.add_handler(CommandHandler("export", handle_export_command, filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(generate_report, pattern="^generate_report$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(show_filters_menu, pattern="^back_to_main$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(show_filters_menu, pattern="^apply_export_filters$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_start_date, pattern="^set_start_date$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_end_date, pattern="^set_end_date$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_import_id, pattern="^set_import_id$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_category, pattern="^set_category$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_type, pattern="^set_type$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_cash_source, pattern="^set_cash_source$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_description_filter, pattern="^set_description$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_counterparty, pattern="^set_counterparty$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_check_num, pattern="^set_check_num$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(set_class, pattern="^set_class$", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(cancel_export, pattern="^cancel_export$", filters=ADMIN_FILTER))
    # Четыре отдельных хендлера 2-го уровня:
    application.add_handler(CallbackQueryHandler(handle_category_selection, pattern="^cat_", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(handle_type_selection,     pattern="^type_", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(handle_source_selection,   pattern="^source_", filters=ADMIN_FILTER))
    application.add_handler(CallbackQueryHandler(handle_class_selection,    pattern="^class_", filters=ADMIN_FILTER))
        # Хендлер для кнопки «↩️ Назад» во втором уровне:
    application.add_handler(CallbackQueryHandler(show_filters_menu,         pattern="^back_to_filters$", filters=ADMIN_FILTER))