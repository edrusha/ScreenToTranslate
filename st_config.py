"""Настройки приложения: %APPDATA%\\ScreenToTranslate\\config.json + окружение.

Портативный режим: если рядом с программой лежит config.json, используется он.
Ключи платных/бесплатных API читаются отсюда и НИКОГДА не печатаются в лог.
Приоритет: переменная окружения (DEEPL_KEY / AZURE_KEY) → config.json.
"""
from __future__ import annotations

import copy
import json
import os

APP_NAME = "ScreenToTranslate"
HERE = os.path.dirname(os.path.abspath(__file__))
_LOCAL_CONFIG = os.path.join(HERE, "config.json")


def _user_dir() -> str:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or HERE
    return os.path.join(base, APP_NAME)


USER_DIR = _user_dir()
# Портативный режим: config.json рядом с программой. Иначе — пользовательские
# данные в %APPDATA%, чтобы установка в Program Files оставалась только для чтения.
CONFIG_PATH = os.environ.get("ST_CONFIG") or (
    _LOCAL_CONFIG if os.path.isfile(_LOCAL_CONFIG) else os.path.join(USER_DIR, "config.json")
)

DEFAULTS = {
    "hotkey_mode": "alt",                # alt = Alt+Ё | single = одна клавиша Ё | ctrl = Ctrl+Ё
    "target_lang": "auto",               # auto: латиница→ru, кириллица→en; либо "ru"/"en"
    "ocr_languages": ["ru", "en"],
    "use_adaptive_ocr": False,           # медленный вариант предобработки
    "panel": {"font_size": 15, "width": 560, "opacity": 0.97, "hide_on_click_away": True},
    "engines": {
        # primary — выбранный в настройках сервис, он идёт первым; остальные — фолбэк
        "primary": "google_free",
        "order": ["google_free", "bing_free", "mymemory_free",
                  "deepl_api", "bing_api", "yandex_api"],
        "deepl": "",                     # ключ DeepL API Free (api-free.deepl.com)
        "azure": "",                     # ключ Azure Translator (тир F0)
        "azure_region": "",
        "yandex": "",                    # ключ Yandex Translate API
        "yandex_folder": "",             # folderId каталога Yandex Cloud
        "mymemory_email": "",            # необязательно: увеличивает лимит MyMemory
    },
}


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    # deepcopy, иначе переменные окружения навсегда меняли бы DEFAULTS.
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    except Exception:
        pass
    # совместимость со старым ключом "hotkey"
    if not cfg.get("hotkey_mode") and cfg.get("hotkey"):
        cfg["hotkey_mode"] = "alt"
    eng = cfg.setdefault("engines", {})
    eng["deepl"] = os.environ.get("DEEPL_KEY") or eng.get("deepl", "")
    eng["azure"] = os.environ.get("AZURE_KEY") or eng.get("azure", "")
    eng["azure_region"] = os.environ.get("AZURE_REGION") or eng.get("azure_region", "")
    eng["yandex"] = os.environ.get("YANDEX_KEY") or eng.get("yandex", "")
    eng["yandex_folder"] = os.environ.get("YANDEX_FOLDER") or eng.get("yandex_folder", "")
    return cfg


def ensure_file() -> str:
    """Создаёт config.json с настройками по умолчанию при первом запуске."""
    if not os.path.exists(CONFIG_PATH):
        save(DEFAULTS)
    return CONFIG_PATH


def save(cfg: dict) -> None:
    """Сохраняет конфиг атомарно. Ошибку записи пробрасывает вызывающему."""
    folder = os.path.dirname(CONFIG_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def keys_present() -> dict[str, bool]:
    eng = load()["engines"]
    return {"deepl": bool(eng.get("deepl")), "azure": bool(eng.get("azure")),
            "yandex": bool(eng.get("yandex"))}
