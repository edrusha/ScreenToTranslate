"""CLI-прогон всего конвейера без GUI: захват → OCR → перевод.

Примеры:
  python st_cli.py --full                 # весь экран, OCR всего
  python st_cli.py --monitor 2            # второй монитор целиком
  python st_cli.py --rect 100,100,700,260 # прямоугольник (физ. пиксели, от левого верхнего угла монитора)
  python st_cli.py --cursor --rect 0,0,800,300 --lang en,ru --no-aggressive
"""
from __future__ import annotations

import argparse
import sys
import time

import st_capture


def parse_rect(s: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in s.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ожидается x,y,w,h")
    return tuple(parts)  # type: ignore[return-value]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="весь виртуальный экран")
    ap.add_argument("--monitor", type=int, help="номер монитора (1..N) или -1 = под курсором")
    ap.add_argument("--rect", type=parse_rect, help="x,y,w,h внутри монитора (физ. пиксели)")
    ap.add_argument("--lang", default="ru,en", help="порядок движков OCR, напр. en,ru или ru")
    ap.add_argument("--target", default=None, help="язык перевода, по умолчанию авто")
    ap.add_argument("--no-translate", action="store_true")
    ap.add_argument("--adaptive", action="store_true", help="добавить адаптивный порог (медленнее)")
    ap.add_argument("--save", default=None, help="сохранить снятый регион в PNG")
    args = ap.parse_args()

    print("dpi:", st_capture.enable_per_monitor_dpi())
    monitors = st_capture.list_monitors()
    print("мониторы (физ. пиксели):", monitors)

    t0 = time.monotonic()
    shot, origin = st_capture.capture_virtual()
    print(f"снимок виртуального экрана: {shot.size}, origin={origin}, {int((time.monotonic()-t0)*1000)} мс")

    if args.full:
        box = (origin[0], origin[1], shot.width, shot.height)
    else:
        idx = args.monitor
        if idx is None:
            cx, cy = st_capture.cursor_pos()
            mon = st_capture.monitor_at(cx, cy) or monitors[0]
        elif idx == -1:
            cx, cy = st_capture.cursor_pos()
            mon = st_capture.monitor_at(cx, cy) or monitors[0]
        else:
            if not 1 <= idx <= len(monitors):
                print(f"нет монитора {idx}, есть 1..{len(monitors)}", file=sys.stderr)
                return 2
            mon = monitors[idx - 1]
        rx, ry, rw, rh = args.rect or (0, 0, mon[2], mon[3])
        box = (mon[0] + rx, mon[1] + ry, rw, rh)

    x, y, w, h = box
    crop = shot.crop((x - origin[0], y - origin[1], x - origin[0] + w, y - origin[1] + h))
    if args.save:
        crop.save(args.save)
        print("сохранено:", args.save)

    import st_ocr

    print("доступные OCR-языки:", st_ocr.available_languages())
    langs = [s.strip() for s in args.lang.split(",") if s.strip()]
    t1 = time.monotonic()
    res = st_ocr.recognize_sync(crop, languages=langs, use_adaptive=args.adaptive)
    print(f"OCR [{res.language}/{res.variant}] lines={res.lines} letters={res.letters} "
          f"conf={res.confidence:.1f} за {int((time.monotonic()-t1)*1000)} мс")
    print("---- распознано ----")
    print(res.text if res.text else "<пусто>")
    if res.paragraphs and len(res.paragraphs) != res.lines:
        print(f"---- абзацы ({len(res.paragraphs)}) ----")
        for p in res.paragraphs:
            print(p)
    if not res.text or args.no_translate:
        return 0

    import st_translate

    try:
        src = "auto" if res.language in ("mixed", "") else res.language.split("-")[0]
        # Переводим абзацы (склеенные строки), а не построчный текст.
        to_translate = "\n".join(res.paragraphs) if res.paragraphs else res.text
        tr = st_translate.translate(to_translate, target=args.target, source=src)
        print(f"---- перевод [{tr.engine} {tr.source_lang}->{tr.target_lang} {tr.ms} мс] ----")
        print(tr.text)
    except Exception as exc:
        print("перевод не удался:", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
