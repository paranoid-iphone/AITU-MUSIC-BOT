import json
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent / "locales"


@lru_cache
def load_locale(language: str) -> dict[str, str]:
    path = BASE_DIR / f"{language}.json"
    if not path.exists():
        path = BASE_DIR / "ru.json"
    return json.loads(path.read_text(encoding="utf-8"))


def t(language: str, key: str) -> str:
    return load_locale(language).get(key) or load_locale("ru").get(key, key)
