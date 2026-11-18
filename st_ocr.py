"""OCR: Windows.Media.Ocr + предобработка, которая не теряет цветной текст.

Ключевые решения:
  * оба движка (en и ru) — а не только английский;
  * увеличение x2 для мелкого текста;
  * несколько вариантов подготовки картинки (как есть, «лучший канал»,
    адаптивный порог) — и НЕ выбор одного лучшего варианта, а построчное
    слияние: каждая строка берётся из того варианта/движка, где она читается
    лучше. Это и вытаскивает цветной текст (красный на зелёном, синий на
    тёмном), и не даёт русскому движку калечить латиницу («key» → «Кеу»).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

try:  # winsdk ставится в tools/pylibs
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    _HAVE_WINSDK = True
except Exception:  # pragma: no cover
    _HAVE_WINSDK = False


@dataclass
class OcrResult:
    text: str
    language: str
    variant: str
    letters: int
    lines: int
    confidence: float = 0.0
    words: list[str] = field(default_factory=list)
    per_line: list[tuple[str, str]] = field(default_factory=list)   # (текст, движок/вариант)
    # Склеенные абзацы (перенесённые строки соединены) — их и надо переводить.
    paragraphs: list[str] = field(default_factory=list)
    boxes: list[tuple[int, int, int, int]] = field(default_factory=list)


@dataclass
class _Line:
    text: str
    lang: str
    variant: str
    x: int
    y: int
    w: int
    h: int
    score: float = 0.0
    cyr: int = 0

    @property
    def yc(self) -> float:
        return self.y + self.h / 2


def available_languages() -> list[str]:
    if not _HAVE_WINSDK:
        return []
    return [lang.language_tag for lang in OcrEngine.available_recognizer_languages]


async def _ocr_png(png: bytes, lang: str):
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = OcrEngine.try_create_from_language(Language(lang))
    if engine is None:
        return None
    return await engine.recognize_async(bitmap)


# --------------------------------------------------------------------------
# метрики качества строки
# --------------------------------------------------------------------------

_ALLOWED_PUNCT = set(" .,:;!?'\"-—–()[]/%№+&@#$*_\\/|~=<>^`{}«»…\n\t")


def _metrics(text: str) -> dict:
    import re

    letters = sum(1 for ch in text if ch.isalnum())
    junk = sum(1 for ch in text if not ch.isalnum() and ch not in _ALLOWED_PUNCT)
    cyr = sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")
    lat = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    mixed = 0
    for word in re.findall(r"[^\W\d_]{2,}", text):
        has_cyr = any("\u0400" <= c <= "\u04FF" for c in word)
        has_lat = any(c.isascii() and c.isalpha() for c in word)
        if has_cyr and has_lat:
            mixed += len(word)
    total = cyr + lat
    return {
        "letters": letters, "junk": junk, "cyr": cyr, "lat": lat,
        "cyr_share": (cyr / total) if total else 0.0, "mixed": mixed,
        "score": letters - 2.5 * junk - 2.0 * mixed,
    }


# --------------------------------------------------------------------------
# варианты предобработки
# --------------------------------------------------------------------------

def _best_channel(im):
    """Канал с максимальным разбросом: цветной текст на цветном фоне не теряется."""
    bands = [list(b.getdata()) for b in im.split()]
    spreads = [max(b) - min(b) for b in bands]
    ch = im.split()[sorted(range(3), key=lambda i: -spreads[i])[0]]
    lo, hi = ch.getextrema()
    if hi - lo < 40:
        return im.convert("L").convert("RGB")
    thr = (lo + hi) / 2
    if sum(1 for p in ch.resize((80, 80)).getdata() if p < thr) > 3200:
        return ch.point(lambda v: 255 if v < thr else 0).convert("RGB")
    return ch.point(lambda v: 0 if v < thr else 255).convert("RGB")


def _adaptive(im, block: int = 31, c: int = 12):
    """Адаптивный порог по локальному среднему (грубая замена Sauvola)."""
    from PIL import Image, ImageFilter

    gray = im.convert("L")
    blur = gray.filter(ImageFilter.BoxBlur(max(3, block // 2)))
    g, b = gray.load(), blur.load()
    out = Image.new("L", gray.size, 255)
    o = out.load()
    w, h = gray.size
    for y in range(h):
        for x in range(w):
            o[x, y] = 0 if g[x, y] + c < b[x, y] else 255
    return out.convert("RGB")


def _levels(im):
    """Растягивает уровни до полного диапазона: спасает текст с контрастом почти 1:1
    (тёмно-серый на чёрном), который не читается ни глазом, ни обычным порогом."""
    from PIL import ImageOps

    gray = ImageOps.autocontrast(im.convert("L"), cutoff=0)
    return gray.point(lambda v: 0 if v < 128 else 255).convert("RGB")


def _contrast_range(im) -> int:
    lo, hi = im.convert("L").getextrema()
    return hi - lo


def _variants(im, use_adaptive: bool = False):
    from PIL import Image

    out = []
    scale = 2 if im.height < 160 else 1
    for name, prep in (("raw", lambda g: g), ("best", _best_channel)):
        base = prep(im)
        if scale != 1:
            base = base.resize((base.width * scale, base.height * scale), Image.LANCZOS)
        out.append((f"{name}_x{scale}" if scale != 1 else name, base))
    # низкий общий контраст области — добавляем вариант с растяжкой уровней
    if _contrast_range(im) < 90:
        base = _levels(im)
        if scale != 1:
            base = base.resize((base.width * scale, base.height * scale), Image.LANCZOS)
        out.append((f"levels_x{scale}" if scale != 1 else "levels", base))
    # adaptive — чистый Python-цикл по пикселям (медленно) и на практике даёт
    # обрывки строк; держим только как опцию.
    if use_adaptive and im.width * im.height <= 1_400_000:
        small = im if im.height < 160 else im.resize((max(1, im.width // 2), max(1, im.height // 2)), Image.LANCZOS)
        out.append(("adaptive", _adaptive(small)))
    return out


def _lines_with_boxes(result, lang: str, variant: str) -> list[_Line]:
    out: list[_Line] = []
    for line in result.lines:
        text = (line.text or "").strip()
        if not text:
            continue
        rects = [w.bounding_rect for w in line.words if getattr(w, "bounding_rect", None) is not None]
        if rects:
            x0 = int(min(r.x for r in rects)); y0 = int(min(r.y for r in rects))
            x1 = int(max(r.x + r.width for r in rects)); y1 = int(max(r.y + r.height for r in rects))
        else:
            x0 = y0 = 0; x1 = y1 = 1
        m = _metrics(text)
        out.append(_Line(text, lang, variant, x0, y0, max(1, x1 - x0), max(1, y1 - y0),
                         m["score"], m["cyr"]))
    return out


def _v_overlap_ratio(a: _Line, b: _Line) -> float:
    top = max(a.y, b.y)
    bot = min(a.y + a.h, b.y + b.h)
    ov = max(0, bot - top)
    return ov / max(1, min(a.h, b.h))


def _is_russian_line(line: _Line) -> bool:
    """Строка русская, если кириллица в ней не «случайная» (ru-движок пишет 'key' как 'Кеу')."""
    if line.cyr < 2:
        return False
    return line.cyr / max(1, len(line.text)) >= 0.3


def _fuse(results: list[tuple[str, str, list[_Line]]]) -> list[_Line]:
    """Базовый вариант + дозаполнение пропущенных строк + замена строк, где видно лучше."""
    if not results:
        return []
    ranked = sorted(results, key=lambda r: sum(l.score for l in r[2]), reverse=True)
    base: list[_Line] = sorted(ranked[0][2], key=lambda l: (l.yc, l.x))
    for _lang, _variant, lines in ranked[1:]:
        for line in sorted(lines, key=lambda l: (l.yc, l.x)):
            if not base:
                base.append(line)
                continue
            match = max(base, key=lambda b: _v_overlap_ratio(line, b))
            if _v_overlap_ratio(line, match) < 0.5:
                base.append(line)          # строки в базовом варианте не было — добавляем
            else:
                new_ru, old_ru = _is_russian_line(line), _is_russian_line(match)
                # Настоящая кириллица (доля >= 0.3) — сильный аргумент: заменяем
                # латинский транслит. Обратное (транслит «Кеу» вместо «key») не проходит:
                # там доля кириллицы мала и _is_russian_line вернёт False.
                if new_ru and not old_ru:
                    base[base.index(match)] = line
                elif old_ru and not new_ru:
                    # Настоящую кириллицу транслитом не заменяем: en-движок
                    # читает русский текст как «3T0 AJIVIHHOe», и букв у него
                    # больше, поэтому по score он обгонял правильную строку.
                    pass
                elif not new_ru and not old_ru:
                    # латинская строка: английский движок точнее («on» вместо «оп»)
                    new_en = line.lang.startswith("en")
                    old_en = match.lang.startswith("en")
                    if new_en and not old_en:
                        base[base.index(match)] = line
                    elif new_en == old_en and line.score > match.score + 2:
                        base[base.index(match)] = line
                elif line.score > match.score + 2:
                    base[base.index(match)] = line
    base.sort(key=lambda l: (l.yc, l.x))
    return base


# --------------------------------------------------------------------------
# абзацы: склейка перенесённых строк
# --------------------------------------------------------------------------

# Пункт списка: «• текст», «- текст», «1. текст», «а) текст».
_ITEM_RE = re.compile(r"^\s*(?:[•·▪◦‣∙*»]|[-–—]\s|\d{1,2}[.)]\s|[a-zA-Zа-яА-Я][.)]\s)")


def _h_overlap(a: _Line, b: _Line) -> float:
    """Доля перекрытия по горизонтали (1.0 — строки друг под другом)."""
    left = max(a.x, b.x)
    right = min(a.x + a.w, b.x + b.w)
    return max(0, right - left) / max(1, min(a.w, b.w))


def _col_width(line: _Line, lines: list[_Line]) -> int:
    """Ширина самой длинной строки рядом (та же колонка, ±6 строк)."""
    reach = 6 * max(line.h, 1)
    near = [l.w for l in lines
            if abs(l.yc - line.yc) <= reach
            and abs(l.x - line.x) <= max(40, line.w // 5)]
    return max(near) if near else line.w


def _merge_words(left: str, right: str) -> str:
    """Склеить две строки: перенос по дефису или через пробел."""
    if left.endswith("-") and len(left) >= 2 and left[-2].isalpha():
        last, first = left[-2], right[:1]
        # Английский перенос: transla- + tion -> translation.
        # Настоящий дефис (кто- + то, Нью- + Йорк) сохраняем.
        if last.isascii() and first.isascii() and first.islower():
            return left[:-1] + right
        return left + right
    return f"{left} {right}"


def _group_paragraphs(lines: list[_Line]) -> list[str]:
    """Собрать строки OCR в абзацы, чтобы переводчик видел целые фразы.

    Строки склеиваются, если они в одной колонке, стоят вплотную и предыдущая
    заполнена до края колонки либо следующая начинается со строчной буквы.
    Пункты списков, строки после двоеточия и новые предложения с заглавной
    остаются отдельными абзацами.
    """
    ordered = sorted(lines, key=lambda l: (l.yc, l.x))
    paras: list[str] = []
    cur = ""
    prev: _Line | None = None

    for ln in ordered:
        text = ln.text.strip()
        if not text:
            continue
        if prev is None:
            cur, prev = text, ln
            continue

        line_h = max(prev.h, ln.h, 1)
        gap = ln.y - (prev.y + prev.h)
        same_col = _h_overlap(prev, ln) >= 0.35
        filled = prev.w >= 0.85 * max(1, _col_width(prev, ordered))
        prev_tail = prev.text.rstrip()
        ends_sentence = prev_tail.endswith((".", "!", "?", "…"))
        starts_upper = text[:1].isupper()
        continues = (text[:1].islower()
                     or prev_tail.endswith((",", ";", "—", "–", "-"))
                     or filled)

        separate = (
            not same_col
            or gap > line_h * 1.2
            or bool(_ITEM_RE.match(text))
            or (ends_sentence and starts_upper)
            or prev_tail.endswith(":")
            or not continues
        )
        if separate:
            paras.append(cur)
            cur = text
        else:
            cur = _merge_words(cur, text)
        prev = ln

    if cur:
        paras.append(cur)
    return paras


async def recognize(im, languages: list[str] | None = None, use_adaptive: bool = False) -> OcrResult:
    """Распознаёт картинку: оба движка × все варианты предобработки, затем слияние построчно."""
    if not _HAVE_WINSDK:
        raise RuntimeError("winsdk не установлен")
    languages = languages or ["ru", "en"]
    results: list[tuple[str, str, list[_Line]]] = []
    for lang in languages:
        for variant, prepared in _variants(im, use_adaptive):
            buf = io.BytesIO()
            prepared.save(buf, format="PNG")
            try:
                result = await _ocr_png(buf.getvalue(), lang)
            except Exception:
                continue
            if result is None:
                continue
            lines = _lines_with_boxes(result, lang, variant)
            if lines:
                results.append((lang, variant, lines))
    if not results:
        return OcrResult("", "", "", 0, 0, 0.0, [])

    chosen = _fuse(results)
    text = "\n".join(ln.text for ln in chosen)
    letters = sum(_metrics(ln.text)["letters"] for ln in chosen)
    langs = {ln.lang.split("-")[0] for ln in chosen}
    language = "mixed" if len(langs) > 1 else next(iter(langs))
    return OcrResult(
        text=text, language=language, variant="fused", letters=letters, lines=len(chosen),
        confidence=float(sum(ln.score for ln in chosen)),
        words=[w for ln in chosen for w in ln.text.split()],
        per_line=[(ln.text, f"{ln.lang}/{ln.variant}") for ln in chosen],
        paragraphs=_group_paragraphs(chosen),
        boxes=[(ln.x, ln.y, ln.w, ln.h) for ln in chosen],
    )


def recognize_sync(im, languages: list[str] | None = None, use_adaptive: bool = False) -> OcrResult:
    import asyncio

    return asyncio.run(recognize(im, languages, use_adaptive))
