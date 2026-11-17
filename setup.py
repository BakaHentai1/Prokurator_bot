"""Мастер настройки: задаёт вопросы и записывает ответы в файл .env.

Запуск:  python setup.py
"""
from pathlib import Path

ENV = Path(__file__).resolve().parent / ".env"

QUESTIONS = [
    ("DISCORD_TOKEN", "Токен бота (Developer Portal → Bot → Reset Token)", True, False),
    ("GUILD_ID", "ID сервера «Легион»", True, True),
    ("REGISTRATUM_CHANNEL_ID", "ID канала «Регистратум»", True, True),
    ("FORMS_CHANNEL_ID", "ID закрытого канала для анкет", True, True),
    ("MEMBER_ROLE_ID", "ID роли, которую получает принятый (Легионеры)", True, True),
    ("NEWCOMER_ROLE_ID", "ID роли новичка, которую снять при принятии (Enter — пропустить)", False, True),
    ("REVIEWER_ROLE_IDS", "ID ролей офицеров через запятую (Сенат, Консулы, Легаты, Центурионы)", True, None),
]


def load_existing() -> dict:
    values = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def valid(value: str, numeric) -> bool:
    if numeric is True:
        return value.isdigit()
    if numeric is None:  # список ID через запятую
        parts = [p for p in value.replace(" ", "").split(",") if p]
        return bool(parts) and all(p.isdigit() for p in parts)
    return True


def main() -> None:
    old = load_existing()
    print("\n=== Настройка Прокуратора ===")
    print("Если значение уже есть, оно показано в скобках — просто нажмите Enter, чтобы оставить.\n")

    result = {}
    for key, prompt, required, numeric in QUESTIONS:
        current = old.get(key, "")
        shown = ("скрыт" if key == "DISCORD_TOKEN" else current) if current else ""
        while True:
            answer = input(f"{prompt}{f' [{shown}]' if shown else ''}: ").strip()
            if not answer:
                answer = current
            if not answer:
                if required:
                    print("  Это поле обязательно.")
                    continue
                break
            if not valid(answer, numeric):
                print("  Тут нужны только цифры (для нескольких ролей — через запятую).")
                continue
            break
        result[key] = answer

    ENV.write_text("".join(f"{k}={v}\n" for k, v in result.items()), encoding="utf-8")
    print("\nГотово! Настройки сохранены. Запускайте бота:  python bot.py\n")


if __name__ == "__main__":
    main()
