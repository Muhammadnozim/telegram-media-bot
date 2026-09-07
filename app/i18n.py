import json
import os

LOCALES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "locales")
translations = {}

def load_locales():
    global translations
    for lang in ["uz", "ru", "en"]:
        path = os.path.join(LOCALES_DIR, f"{lang}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                translations[lang] = json.load(f)

load_locales()

def get_text(lang: str, key: str, **kwargs) -> str:
    lang_data = translations.get(lang, translations.get("uz", {}))
    text = lang_data.get(key, key)
    if kwargs:
        return text.format(**kwargs)
    return text
