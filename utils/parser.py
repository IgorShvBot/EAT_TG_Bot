import re

def parse_settings_from_text(message_text: str) -> dict:
    settings = {}
    if not message_text:
        return settings

    lines = [line.strip() for line in message_text.split('\n')[:100] if line and len(line) < 100]
    pattern = re.compile(r"^(.+?)\s*:\s*(\+?)\s*(.*)$", re.IGNORECASE)

    for line in lines:
        match = pattern.match(line)
        if match:
            key = match.group(1).strip().lower()
            operator = match.group(2).strip()
            value = match.group(3).strip()

            if key in ['контрагент', 'контрагента']:
                key = 'Контрагент'
            elif key in ['чек', 'чек #', 'чек№']:
                key = 'Чек #'
            elif key in ['описание', 'описании']:
                key = 'Описание'
            elif key in ['наличность', 'нал', 'наличка']:
                key = 'Наличность'
            elif key in ['класс']:
                key = 'Класс'

            settings[key] = {
                'operator': operator,
                'value': value
            }

    return settings
