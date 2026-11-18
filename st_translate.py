"""Перевод: несколько бесплатных источников, без ключей и регистрации.

Порядок (первый удачный побеждает):
  1. Google gtx        translate.googleapis.com (client=gtx)   — лучшее качество
  2. Google clients5   clients5.google.com (dict-chrome-ex)
  3. Bing Translator   www.bing.com/ttranslatev3 — без ключа, двухшаговый (токены из HTML)
  4. MyMemory          api.mymemory.translated.net — keyless, но лимит 480 символов на запрос

Длинный текст режется на куски по строкам/предложениям: каждый движок имеет
свой лимит, поэтому «книжная страница» переводится целиком, а не обрезается.
Движок, упавший с 429/ошибкой, уходит в «карантин» (30 минут для лимита 429,
5 минут для прочих ошибок) — иначе каждый вызов тратил бы секунду на заведомо
мёртвый источник.
"""
from __future__ import annotations

import hashlib
import html
import gzip
import http.cookiejar
import json
import os
import re
import socket
import threading
import time
import urllib.parse
from urllib.parse import quote
import urllib.request
import uuid
from dataclasses import dataclass, field

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
_LEGACY_CACHE = os.path.join(HERE, "cache", "translations.json")


def _user_cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or HERE
    return os.path.join(base, "ScreenToTranslate", "cache", "translations.json")


# В установленной версии Program Files доступна только для чтения, поэтому кэш
# живёт в %LOCALAPPDATA%. Старый локальный кэш используется в портативном режиме.
CACHE_PATH = os.environ.get("ST_CACHE") or (
    _LEGACY_CACHE if os.path.isfile(_LEGACY_CACHE) else _user_cache_path()
)
COOLDOWN_SEC = 300.0        # сколько не трогать движок после ошибки
COOLDOWN_429_SEC = 1800.0   # 429 — это лимит по IP, за 5 минут он не кончится
_cooldown: dict[str, float] = {}
_last_error: dict[str, str] = {}   # причина, по которой движок в карантине


@dataclass
class Translation:
    text: str
    engine: str
    source_lang: str
    target_lang: str
    cached: bool = False
    ms: int = 0
    chunks: int = 1
    preferred: str = ""                     # какой сервис выбран в настройках
    errors: list[str] = field(default_factory=list)   # почему не сработали другие


class TranslateError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# кэш
# --------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def _key(text: str, src: str, dst: str, route: str = "") -> str:
    # route — набор активных движков. Без него после смены сервиса возвращался
    # перевод из кэша, сделанный предыдущим движком.
    return hashlib.sha1(f"{route}|{src}|{dst}|{text.strip()}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# утилиты
# --------------------------------------------------------------------------

def _get(url: str, timeout: float = 12.0, data: bytes | None = None,
         opener=None, headers: dict | None = None) -> tuple[int, str]:
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "ru,en;q=0.8"}
    if headers:
        hdrs.update(headers)
    if data is not None:
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, headers=hdrs)
    open_fn = opener.open if opener else urllib.request.urlopen
    with open_fn(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def detect_script(text: str) -> str:
    cyr = sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")
    lat = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if cyr == 0 and lat == 0:
        return "en"
    return "ru" if cyr >= lat else "en"


def _clean(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    # OCR любит пробел перед знаком: «слово , слово» -> «слово, слово».
    text = re.sub(r"\s+([,.;:!?…])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cyr_count(text: str) -> int:
    return sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")


def _looks_translated(source: str, result: str, dst: str) -> bool:
    """Грубая проверка, что пришёл именно перевод, а не исходный текст."""
    if not result.strip() or result.strip() == source.strip():
        return False
    if dst == "ru" and detect_script(source) == "en":
        return _cyr_count(result) >= max(2, len(result) // 12)
    if dst == "en" and detect_script(source) == "ru":
        return _cyr_count(result) < len(result) // 3
    return True


def split_chunks(text: str, limit: int) -> list[str]:
    """Режет текст на куски <= limit, не разрывая слова и по возможности строки."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 <= limit:
            cur = f"{cur}\n{para}" if cur else para
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(para) <= limit:
            cur = para
            continue
        for sent in re.split(r"(?<=[.!?;:])\s+", para):
            if len(cur) + len(sent) + 1 <= limit:
                cur = f"{cur} {sent}" if cur else sent
            else:
                if cur:
                    chunks.append(cur)
                    cur = ""
                rest = sent
                while len(rest) > limit:
                    cut = rest.rfind(" ", 0, limit)
                    cut = cut if cut > 0 else limit
                    chunks.append(rest[:cut])
                    rest = rest[cut:].lstrip()
                cur = rest
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


# --------------------------------------------------------------------------
# движки
# --------------------------------------------------------------------------

def _google_gtx(text: str, src: str, dst: str) -> str:
    url = ("https://translate.googleapis.com/translate_a/single"
           f"?client=gtx&sl={src}&tl={dst}&dt=t&dt=bd&dj=1&q={urllib.parse.quote(text)}")
    status, body = _get(url)
    if status != 200:
        raise TranslateError(f"gtx HTTP {status}")
    if body.lstrip().startswith("<"):
        raise TranslateError("gtx вернул HTML (обычно 429/CAPTCHA)")
    data = json.loads(body)
    out = "".join(s.get("trans", "") for s in data.get("sentences", [])).strip()
    if not out:
        raise TranslateError("gtx пустой ответ")
    return out


def _google_clients5(text: str, src: str, dst: str) -> str:
    url = ("https://clients5.google.com/translate_a/t?client=dict-chrome-ex"
           f"&sl={src}&tl={dst}&q={urllib.parse.quote(text)}")
    status, body = _get(url)
    if status != 200:
        raise TranslateError(f"clients5 HTTP {status}")
    if body.lstrip().startswith("<"):
        raise TranslateError("clients5 вернул HTML (обычно 429/CAPTCHA)")
    data = json.loads(body)
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, list):
        if data and isinstance(data[0], str):
            return " ".join(data).strip()
        if data and isinstance(data[0], list):
            return "".join(seg[0] for seg in data[0] if isinstance(seg, list) and seg).strip()
        if data and isinstance(data[0], dict):
            return " ".join(d.get("trans", "") for d in data).strip()
    raise TranslateError("clients5: незнакомый формат ответа")


class _Bing:
    """Microsoft Translator через веб: токены живут в HTML страницы переводчика."""

    URL = "https://www.bing.com/ttranslatev3?isVertical=1&&IG={ig}&IID=translator.5028"

    def __init__(self):
        self._opener = None
        self._token = None
        self._key = None
        self._ts = 0.0
        self._refresh_lock = threading.Lock()

    def _session(self):
        if self._opener is None:
            cj = http.cookiejar.CookieJar()
            self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        return self._opener

    def stale(self) -> bool:
        """Токен нужно получать заново? Живёт ~9 минут."""
        return self._token is None or time.monotonic() - self._ts > 540

    def _refresh(self):
        with self._refresh_lock:
            # Другой поток мог уже обновить токен, пока мы ждали блокировку.
            if not self.stale():
                return
            opener = self._session()
            # Bing отдаёт страницу сжатой: 325 КБ вместо 627 КБ и ~0,1 с быстрее.
            req = urllib.request.Request("https://www.bing.com/translator",
                                         headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8",
                                                  "Accept-Encoding": "gzip"})
            with opener.open(req, timeout=20) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
            html = raw.decode("utf-8", "replace")
            m = re.search(r"params_AbusePreventionHelper\s*=\s*\[(\d+),\s*\"([^\"]+)\"", html)
            if not m:
                raise TranslateError("bing: не нашёл токены в HTML")
            self._key, self._token = m.group(1), m.group(2)
            self._ts = time.monotonic()

    def __call__(self, text: str, src: str, dst: str) -> str:
        if self.stale():
            self._refresh()
        for attempt in (1, 2):
            data = urllib.parse.urlencode({
                "fromLang": "auto-detect" if src in ("auto", "", None) else src,
                "to": dst, "text": text, "token": self._token, "key": self._key,
            }).encode()
            try:
                status, body = _get(self.URL.format(ig=uuid.uuid4().hex.upper()), timeout=20,
                                    data=data, opener=self._session(),
                                    headers={"Referer": "https://www.bing.com/translator"})
            except Exception as exc:
                if attempt == 1:
                    self._refresh()
                    continue
                raise TranslateError(f"bing: {exc}") from exc
            if status != 200:
                if attempt == 1:
                    self._refresh()
                    continue
                raise TranslateError(f"bing HTTP {status}")
            try:
                payload = json.loads(body)
            except Exception:
                if attempt == 1:
                    self._refresh()
                    continue
                raise TranslateError(f"bing: не JSON ({body[:80]})")
            if isinstance(payload, dict):
                if attempt == 1:
                    self._refresh()
                    continue
                raise TranslateError(f"bing: {str(payload)[:120]}")
            out = "".join(seg.get("text", "") for seg in payload[0].get("translations", [])).strip()
            if not out:
                raise TranslateError("bing: пустой ответ")
            return out
        raise TranslateError("bing: не удалось")


def _mymemory(text: str, src: str, dst: str, email: str | None = None) -> str:
    url = ("https://api.mymemory.translated.net/get"
           f"?q={urllib.parse.quote(text)}&langpair={src}|{dst}")
    if email:
        url += f"&de={urllib.parse.quote(email)}"
    status, body = _get(url)
    if status != 200:
        raise TranslateError(f"mymemory HTTP {status}")
    data = json.loads(body)
    if int(data.get("responseStatus", 0)) != 200:
        raise TranslateError(f"mymemory: {data.get('responseDetails')}")
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if not out:
        raise TranslateError("mymemory пустой ответ")
    if "MYMEMORY WARNING" in out.upper():
        raise TranslateError("mymemory: лимит запросов")
    return out


def _deepl(text: str, src: str, dst: str, key: str) -> str:
    """DeepL API Free: бесплатный план, ключ из config.json / переменной DEEPL_KEY."""
    if not key:
        raise TranslateError("deepl: нет ключа")
    payload = json.dumps({"text": [text], "target_lang": dst.upper()}).encode("utf-8")
    if src not in ("auto", "", None):
        payload = json.dumps({"text": [text], "target_lang": dst.upper(),
                              "source_lang": src.upper()}).encode("utf-8")
    status, body = _get("https://api-free.deepl.com/v2/translate", timeout=20, data=payload,
                        headers={"Authorization": f"DeepL-Auth-Key {key}",
                                 "Content-Type": "application/json"})
    if status != 200:
        raise TranslateError(f"deepl HTTP {status}: {body[:100]}")
    data = json.loads(body)
    out = "".join(t.get("text", "") for t in data.get("translations", [])).strip()
    if not out:
        raise TranslateError("deepl: пустой ответ")
    return out


def _azure(text: str, src: str, dst: str, key: str, region: str) -> str:
    """Azure AI Translator, бесплатный тир F0."""
    if not key:
        raise TranslateError("azure: нет ключа")
    params = {"api-version": "3.0", "to": dst}
    if src not in ("auto", "", None):
        params["from"] = src
    url = "https://api.cognitive.microsofttranslator.com/translate?" + urllib.parse.urlencode(params)
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/json"}
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    status, body = _get(url, timeout=20, data=json.dumps([{"Text": text}]).encode("utf-8"),
                        headers=headers)
    if status != 200:
        raise TranslateError(f"azure HTTP {status}: {body[:100]}")
    data = json.loads(body)
    out = "".join(t.get("text", "") for seg in data for t in seg.get("translations", [])).strip()
    if not out:
        raise TranslateError("azure: пустой ответ")
    return out


def _yandex(text: str, src: str, dst: str, key: str, folder: str) -> str:
    """Yandex Translate API v2 (Yandex Cloud): нужен API-ключ и folderId каталога."""
    if not key:
        raise TranslateError("yandex: нет ключа")
    body: dict[str, object] = {"targetLanguageCode": dst, "texts": [text]}
    if src not in ("auto", "", None):
        body["sourceLanguageCode"] = src
    if folder:
        body["folderId"] = folder
    status, resp = _get("https://translate.api.cloud.yandex.net/translate/v2/translate",
                        timeout=20, data=json.dumps(body).encode("utf-8"),
                        headers={"Authorization": f"Api-Key {key}",
                                 "Content-Type": "application/json"})
    if status != 200:
        raise TranslateError(f"yandex HTTP {status}: {resp[:100]}")
    data = json.loads(resp)
    out = "".join(t.get("text", "") for t in data.get("translations", [])).strip()
    if not out:
        raise TranslateError("yandex: пустой ответ")
    return out


def _google_any(text: str, src: str, dst: str) -> str:
    """Сервис «Google» = два бесплатных эндпоинта, второй как запасной."""
    try:
        return _google_gtx(text, src, dst)
    except Exception:
        return _google_clients5(text, src, dst)


_bing = _Bing()


def prewarm_async() -> None:
    """Обновить токен Bing фоном — пока пользователь выделяет область.

    Вызывается по горячей клавише: пока идёт выделение и OCR (0,3–1 с),
    страница Bing успевает отдать токен. Если токен ещё свежий, в сеть
    не ходим вообще. В простое приложение не делает ни одного запроса.
    """
    if not _bing.stale() or _bing._refresh_lock.locked():
        return

    def run():
        try:
            _bing._refresh()
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def warmup() -> None:
    """Проверка основного сервиса при старте приложения.

    Если он заблокирован (429), сразу уходит в карантин — и первый перевод
    не тратит на него время. Если оживёт, проверка снимет карантин.
    Токен Bing здесь не тянем: он нужен только перед реальным переводом,
    а там его обновит prewarm_async() без лишнего трафика в простое.
    """
    try:
        import st_config
        cfg = st_config.load()
        primary = normalize_service(((cfg or {}).get("engines") or {}).get("primary"))
    except Exception:
        return
    # Ключевые сервисы не дёргаем зря — каждый их запрос стоит денег.
    svc = SERVICE_BY_NAME.get(primary) or {}
    if primary and not svc.get("needs_key"):
        probe_health([primary], timeout=6.0, use_cache=False)

# Что показывается в настройках.
#   needs_key=False → в подписи «free», поля ключа нет (бесплатный веб-эндпоинт);
#   needs_key=True  → в подписи «[api]», появляется поле ключа.
# Ключ всегда необязателен: без него сервис просто не участвует в цепочке.
SERVICES = [
    {"name": "google_free", "label": "Google free", "needs_key": False,
     "hint": "Бесплатный веб-перевод Google, без регистрации и ключа."},
    {"name": "google_api", "label": "Google [api]", "needs_key": True,
     "key_hint": "Ключ Google Cloud Translation API (в проекте включён Cloud Translation API).",
     "hint": "Облачный API Google. Без ключа не используется."},
    {"name": "bing_free", "label": "Bing free", "needs_key": False,
     "hint": "Бесплатный веб-перевод Bing, без регистрации и ключа."},
    {"name": "bing_api", "label": "Bing [api]", "needs_key": True,
     "key_hint": "Ключ Azure Translator (тир F0) и регион, например westeurope.",
     "hint": "Официальный API Microsoft Translator (Azure). Без ключа не используется."},
    {"name": "mymemory_free", "label": "MyMemory free", "needs_key": False,
     "hint": "Бесплатный веб-API MyMemory, без ключа (лимит на длину запроса)."},
    {"name": "deepl_api", "label": "DeepL [api]", "needs_key": True,
     "key_hint": "Ключ DeepL API Free (api-free.deepl.com).",
     "hint": "Официальный API DeepL. Без ключа не используется."},
    {"name": "yandex_api", "label": "Yandex [api]", "needs_key": True,
     "key_hint": "API-ключ сервисного аккаунта Yandex Cloud и folderId каталога.",
     "hint": "Официальный API Yandex Translate. Без ключа не используется."},
]
SERVICE_BY_NAME = {s["name"]: s for s in SERVICES}

# Хосты для лёгкого замера задержки (TCP-подключение к 443, без перевода текста).
SERVICE_HOSTS = {
    "google_free": "translate.googleapis.com",
    "google_api": "translation.googleapis.com",
    "bing_free": "www.bing.com",
    "bing_api": "api.cognitive.microsofttranslator.com",
    "mymemory_free": "api.mymemory.translated.net",
    "deepl_api": "api-free.deepl.com",
    "yandex_api": "translate.api.cloud.yandex.net",
}

# старые имена (из прежних версий config.json) -> новые
ALIASES = {
    "google": "google_free", "bing": "bing_free", "mymemory": "mymemory_free",
    "deepl": "deepl_api", "azure": "bing_api", "yandex": "yandex_api",
}


def normalize_service(name: str | None) -> str:
    return ALIASES.get(name or "", name or "")


def probe_latency(name: str, timeout: float = 0.8) -> int | None:
    """Примерная сетевая задержка сервиса в мс: TCP-коннект к его хосту.

    Ничего не переводит и не тратит квоту. Возвращает None, если хост не ответил.
    """
    host = SERVICE_HOSTS.get(normalize_service(name))
    if not host:
        return None
    started = time.perf_counter()
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            pass
    except OSError:
        return None
    return max(1, int((time.perf_counter() - started) * 1000))


def _google_api(text: str, src: str, dst: str, key: str) -> str:
    """Google Cloud Translation v2 — официальный API по ключу."""
    if not key:
        raise TranslateError("google_api: нет ключа")
    url = "https://translation.googleapis.com/language/translate/v2?key=" + quote(key)
    payload = {"q": [text], "target": dst, "format": "text"}
    if src not in ("auto", "", None):
        payload["source"] = src
    status, resp = _get(url, timeout=25, data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"})
    if status != 200:
        raise TranslateError(f"google_api HTTP {status}: {resp[:120]}")
    data = json.loads(resp)
    try:
        out = data["data"]["translations"][0]["translatedText"]
    except Exception:
        raise TranslateError(f"google_api: неожиданный ответ {resp[:120]}")
    return html.unescape(out).strip()


def _engine_table(cfg: dict | None = None) -> list[tuple[str, object, int]]:
    """Активные движки: выбранный в настройках первым, дальше фолбэки.
    Сервисы, которым нужен ключ, без ключа просто пропускаются."""
    if cfg is None:
        try:
            import st_config
            cfg = st_config.load()
        except Exception:
            cfg = {}
    eng = (cfg or {}).get("engines", {}) or {}
    keys = {
        "deepl_api": eng.get("deepl", ""), "bing_api": eng.get("azure", ""),
        "google_api": eng.get("google_api", ""),
        "azure_region": eng.get("azure_region", ""), "email": eng.get("mymemory_email", ""),
        "yandex_api": eng.get("yandex", ""), "yandex_folder": eng.get("yandex_folder", ""),
    }
    table: dict[str, tuple[object, int]] = {
        "google_free": (_google_any, 4500),
        "google_api": (lambda t, s, d: _google_api(t, s, d, keys["google_api"]), 4500),
        "bing_free": (_bing, 900),
        "bing_api": (lambda t, s, d: _azure(t, s, d, keys["bing_api"], keys["azure_region"]), 4000),
        "mymemory_free": (lambda t, s, d: _mymemory(t, s, d, keys["email"] or None), 450),
        "deepl_api": (lambda t, s, d: _deepl(t, s, d, keys["deepl_api"]), 4000),
        "yandex_api": (lambda t, s, d: _yandex(t, s, d, keys["yandex_api"], keys["yandex_folder"]), 5000),
    }
    order = [normalize_service(n) for n in (eng.get("order") or [s["name"] for s in SERVICES])]
    order = [n for n in order if n in table]
    primary = normalize_service(eng.get("primary"))
    if primary and primary in table:
        order = [primary] + [n for n in order if n != primary]
    out: list[tuple[str, object, int]] = []
    for name in order:
        if SERVICE_BY_NAME.get(name, {}).get("needs_key") and not keys.get(name):
            continue
        fn, limit = table[name]
        out.append((name, fn, limit))
    return out


# базовый список (совместимость)
ENGINES = [
    ("google_free", _google_any, 4500),
    ("bing_free", _bing, 900),
    ("mymemory_free", _mymemory, 450),
]


def translate(text: str, target: str | None = None, source: str = "auto",
              use_cache: bool = True, email: str | None = None,
              only_engine: str | None = None, cfg: dict | None = None) -> Translation:
    """Переводит текст первым доступным движком. target=None → авто (латиница→ru, кириллица→en)."""
    text = _clean(text)
    if not text:
        raise TranslateError("нечего переводить")
    src = detect_script(text) if source in ("auto", "") else source
    dst = target or ("ru" if src == "en" else "en")
    only_engine = normalize_service(only_engine) if only_engine else None

    if cfg is None:
        try:
            import st_config
            cfg = st_config.load()
        except Exception:
            cfg = {}
    engines = _engine_table(cfg)
    preferred = normalize_service(((cfg or {}).get("engines") or {}).get("primary"))
    route = "|".join(name for name, _fn, _limit in engines)
    if only_engine:
        route += f"|only:{only_engine}"
    key = _key(text, f"{source}:{src}", dst, route)
    cache = _load_cache() if use_cache else {}
    if use_cache and key in cache:
        return Translation(cache[key], "cache", src, dst, cached=True, ms=0,
                           preferred=preferred)

    errors: list[str] = []
    if preferred and preferred not in {n for n, _fn, _limit in engines}:
        # Выбран сервис с ключом, а ключа нет: он даже не попал в цепочку,
        # но пользователь должен увидеть, почему перевёл другой.
        errors.append(f"{preferred}: нужен ключ")
    started = time.monotonic()
    for name, fn, limit in engines:
        if only_engine and name != only_engine:
            continue
        if _cooldown.get(name, 0.0) > time.monotonic():
            errors.append(f"{name}: {_last_error.get(name) or 'временно отключён'}")
            continue
        try:
            pieces = split_chunks(text, limit)
            parts = []
            for piece in pieces:
                parts.append(fn(piece, src, dst))
            out = _clean("\n".join(parts))
            if not _looks_translated(text, out, dst):
                raise TranslateError("ответ не похож на перевод")
            ms = int((time.monotonic() - started) * 1000)
            if use_cache:
                cache[key] = out
                _save_cache(cache)
            _cooldown.pop(name, None)
            _last_error.pop(name, None)
            return Translation(out, name, src, dst, cached=False, ms=ms,
                               chunks=len(pieces), preferred=preferred,
                               errors=list(errors))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            _last_error[name] = str(exc)
            # 429 — блокировка по IP: повторять каждые 5 минут бессмысленно,
            # каждая неудачная попытка Google стоит почти 2 секунды.
            penalty = COOLDOWN_429_SEC if "429" in str(exc) else COOLDOWN_SEC
            _cooldown[name] = time.monotonic() + penalty
    raise TranslateError("; ".join(errors) if errors else "все движки недоступны")


def engine_status() -> dict[str, str]:
    """Кто сейчас в карантине (для GUI/диагностики)."""
    now = time.monotonic()
    return {name: (f"карантин ещё {int(_cooldown[name] - now)} с" if _cooldown.get(name, 0) > now else "готов")
            for name in list(SERVICE_BY_NAME)}


# --------------------------------------------------------------------------
# проверка «кто реально переводит»
# --------------------------------------------------------------------------

_HEALTH_TTL = 120.0
_health_cache: dict[tuple[str, ...], tuple[float, dict[str, dict]]] = {}


def short_error(err: str) -> str:
    """Короткая причина для меню: «лимит запросов (429)», «нет ответа»."""
    text = (err or "").strip()
    if not text:
        return "ошибка"
    m = re.search(r"\b(4\d\d|5\d\d)\b", text)
    if m:
        return {
            "429": "лимит запросов (429)",
            "401": "нужен ключ (401)",
            "403": "заблокировано (403)",
        }.get(m.group(1), f"HTTP {m.group(1)}")
    low = text.lower()
    if "карантин" in low:
        return "временно отключён после ошибки"
    if "timed out" in low or "timeout" in low:
        return "нет ответа"
    if "getaddrinfo" in low or "name resolution" in low:
        return "нет сети"
    if "нет ключа" in low:
        return "нужен ключ"
    return text[:48]


def probe_health(names: list[str], timeout: float = 4.0,
                 use_cache: bool = True) -> dict[str, dict]:
    """Проверить сервисы настоящим коротким переводом («Hello» → ru).

    Возвращает {name: {"ok": True|False|None, "ms": int|None, "info": str}}.
    ok=None — сервис не успел ответить за timeout. Результат кэшируется на
    две минуты, чтобы каждое открытие меню не дёргало сервисы заново.
    """
    names = [normalize_service(n) for n in names]
    key = tuple(names)
    now = time.monotonic()
    if use_cache:
        hit = _health_cache.get(key)
        if hit and now - hit[0] < _HEALTH_TTL:
            return {n: dict(v) for n, v in hit[1].items()}

    try:
        import st_config
        cfg = st_config.load()
    except Exception:
        cfg = {}
    table = {n: fn for n, fn, _limit in _engine_table(cfg)}

    boxes: dict[str, dict] = {}
    threads: dict[str, threading.Thread] = {}

    def check(name: str, fn):
        box = boxes[name]
        started = time.perf_counter()
        try:
            fn("Hello", "en", "ru")
            _cooldown.pop(name, None)      # сервис ожил — снимаем карантин
            _last_error.pop(name, None)
            box.update(ok=True, ms=max(1, int((time.perf_counter() - started) * 1000)))
        except Exception as exc:
            box.update(ok=False, info=short_error(str(exc)))
            _last_error[name] = str(exc)
            # 429/403 — блокировка по IP: сразу в карантин, чтобы перевод
            # не тратил на неё время. Остальные ошибки не карантиним:
            # проверка может быть разовой, а без движка хуже.
            if "429" in str(exc) or "403" in str(exc):
                _cooldown[name] = time.monotonic() + COOLDOWN_429_SEC

    for name in names:
        fn = table.get(name)
        if fn is None:
            svc = SERVICE_BY_NAME.get(name, {})
            boxes[name] = {"ok": False, "ms": None,
                           "info": "нужен ключ" if svc.get("needs_key") else "недоступен"}
            continue
        boxes[name] = {"ok": None, "ms": None, "info": ""}
        th = threading.Thread(target=check, args=(name, fn), daemon=True)
        threads[name] = th
        th.start()

    deadline = time.monotonic() + timeout
    for name, th in threads.items():
        th.join(max(0.0, deadline - time.monotonic()))
        if th.is_alive():
            boxes[name].update(ok=None, info="нет ответа")

    _health_cache[key] = (time.monotonic(), {n: dict(v) for n, v in boxes.items()})
    return boxes


if __name__ == "__main__":
    import sys

    sample = sys.argv[1] if len(sys.argv) > 1 else "The quick brown fox jumps over the lazy dog"
    print(translate(sample))
