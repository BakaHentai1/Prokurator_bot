"""Настройки бота. Все значения берутся из файла .env рядом с этим файлом."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

_missing: list[str] = []


def _int(name: str, required: bool = True) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        if required:
            _missing.append(name)
        return 0
    try:
        return int(raw)
    except ValueError:
        _missing.append(f"{name} (должно быть число, а сейчас: {raw})")
        return 0


def _ids(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    result = []
    for part in raw.replace(" ", "").split(","):
        if part.isdigit():
            result.append(int(part))
    return result


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN:
    _missing.append("DISCORD_TOKEN")

GUILD_ID = _int("GUILD_ID")
REGISTRATUM_CHANNEL_ID = _int("REGISTRATUM_CHANNEL_ID")
FORMS_CHANNEL_ID = _int("FORMS_CHANNEL_ID")
MEMBER_ROLE_ID = _int("MEMBER_ROLE_ID")
NEWCOMER_ROLE_ID = _int("NEWCOMER_ROLE_ID", required=False)  # «Плебей»
ALLY_ROLE_ID = _int("ALLY_ROLE_ID", required=False)  # «Союзник»
WARDEN_ROLE_ID = _int("WARDEN_ROLE_ID", required=False)  # «Варден» (гости другой фракции)
REVIEWER_ROLE_IDS = _ids("REVIEWER_ROLE_IDS")

DATA_DIR = BASE_DIR / "data"


def check() -> None:
    """Останавливает запуск, если в .env чего-то не хватает."""
    if _missing:
        print("\nОшибка: в файле .env не заполнено:")
        for name in _missing:
            print(f"  - {name}")
        print("\nОткройте .env, впишите значения и запустите бота снова.\n")
        raise SystemExit(1)
