"""ScreenToTranslate — перевод с экрана: любой монитор, честный OCR, панель-перевод.

Запуск: python st_app.py  (трей + горячая клавиша «Ё»/«~»)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

# Пакеты (PySide6, winsdk, Pillow) лежат в pylibs рядом с программой (в исходниках —
# на уровень выше). Добавляем их в путь сами, чтобы приложение запускалось любым
# способом — в том числе из автозагрузки, где PYTHONPATH не выставлен.
HERE = os.path.dirname(os.path.abspath(__file__))
_ICON_PATH = os.path.join(HERE, "ScreenToTranslate.ico")
_PYLIBS = next((p for p in (os.path.join(HERE, "pylibs"),
                            os.path.abspath(os.path.join(HERE, "..", "pylibs")))
                if os.path.isdir(p)), None)
if _PYLIBS and _PYLIBS not in sys.path:
    sys.path.insert(0, _PYLIBS)

# PySide6/Pillow/winsdk могут быть не установлены (исходники без pylibs).
# Проверяем до их импорта: при нехватке предложим скачать через pip --user.
import st_bootstrap

st_bootstrap.ensure_dependencies()

import st_capture
import st_config
import st_translate

# DPI-awareness ставим ДО создания QApplication, иначе Qt выберет свой режим
DPI_MODE = st_capture.enable_per_monitor_dpi()

from PySide6.QtCore import (QAbstractNativeEventFilter, QEvent, QObject, QPoint, QPointF,
                            QRunnable, QRect, QRectF, Qt, QThread, QThreadPool, QTimer, Signal)
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QImage, QKeySequence,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                               QRadioButton, QSystemTrayIcon, QVBoxLayout, QWidget)


user32 = ctypes.windll.user32
WM_HOTKEY = 0x0312
VK_OEM_3 = 0xC0          # клавиша «Ё» / «~»
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# режим хоткея -> (модификаторы, человеческая подпись)
HOTKEY_MODES = {
    "alt": (MOD_ALT, "Alt+Ё"),
    "single": (0, "Ё"),
    "ctrl": (MOD_CONTROL, "Ctrl+Ё"),
}


def hotkey_of(cfg: dict) -> tuple[int, int, str]:
    """(модификаторы, виртуальный код клавиши, подпись) из настроек.

    Новая схема — произвольное сочетание в cfg["hotkey"]; старая (hotkey_mode)
    поддерживается для совместимости."""
    hk = (cfg or {}).get("hotkey") or {}
    if hk.get("vk"):
        return int(hk.get("mods", 0)), int(hk["vk"]), str(hk.get("text") or "…")
    mode = (cfg or {}).get("hotkey_mode", "alt")
    mods, label = HOTKEY_MODES.get(mode, HOTKEY_MODES["alt"])
    return mods, VK_OEM_3, label


def health_suffix(entry: dict | None) -> str:
    """Подпись в меню: реальный результат проверки, а не TCP-задержка."""
    if not entry:
        return ""
    if entry.get("ok") is True:
        ms = int(entry.get("ms") or 0)
        took = f"{ms / 1000:.1f} с" if ms >= 1000 else f"{ms} мс"
        return f"   ·   работает, {took}"
    return f"   ·   {entry.get('info') or 'нет ответа'}"


def probe_health(names: list[str]) -> dict[str, dict]:
    """Проверка сервисов коротким настоящим переводом (см. st_translate)."""
    if not names:
        return {}
    try:
        return st_translate.probe_health(names)
    except Exception:
        return {name: {"ok": None, "ms": None, "info": "нет ответа"} for name in names}


def pil_to_pixmap(im, dpr: float = 1.0) -> QPixmap:
    im = im.convert("RGB")
    data = im.tobytes("raw", "RGB")
    qimg = QImage(data, im.width, im.height, im.width * 3, QImage.Format.Format_RGB888).copy()
    pm = QPixmap.fromImage(qimg)
    pm.setDevicePixelRatio(dpr)
    return pm


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt_x", wt.LONG), ("pt_y", wt.LONG)]


# --------------------------------------------------------------------------
# выделение области
# --------------------------------------------------------------------------

class Overlay(QWidget):
    selected = Signal(QRect)      # логические пиксели внутри монитора
    cancelled = Signal()

    def __init__(self, screen, shot, origin, dpr: float, phys_rect: tuple[int, int, int, int] | None = None):
        """phys_rect — физический прямоугольник монитора (из Win32). Именно он берётся
        для вырезки из снимка: пересчёт через devicePixelRatio давал сдвиг на втором
        мониторе, потому что логические координаты Qt не равны физические/dpr."""
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._screen = screen
        self._dpr = dpr
        self._shot = shot
        self._origin = origin
        geo = screen.geometry()
        self.setGeometry(geo)
        if phys_rect:
            sx, sy, pw, ph = phys_rect
            px, py = sx - origin[0], sy - origin[1]
        else:  # запасной путь, если Win32 не дал прямоугольник
            px = int(round(geo.x() * dpr)) - origin[0]
            py = int(round(geo.y() * dpr)) - origin[1]
            pw = int(round(geo.width() * dpr))
            ph = int(round(geo.height() * dpr))
        self.region_pil = shot.crop((px, py, px + pw, py + ph))
        self._pixmap = pil_to_pixmap(self.region_pil, dpr)
        self._phys = (px + origin[0], py + origin[1], pw, ph)
        self._start: QPoint | None = None
        self._sel: QRect | None = None
        self._font = QFont("Segoe UI", 9)

    # --- события ---
    def showEvent(self, ev):
        super().showEvent(ev)
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
            self.close()
            return
        self._start = ev.position().toPoint()
        self._sel = QRect(self._start, self._start)
        self.update()

    def mouseMoveEvent(self, ev):
        if self._start is None:
            return
        self._sel = QRect(self._start, ev.position().toPoint()).normalized()
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._start is None:
            return
        sel = QRect(self._start, ev.position().toPoint()).normalized()
        self._start = None
        if sel.width() < 8 or sel.height() < 8:
            self.cancelled.emit()
            self.close()
            return
        self._sel = sel
        self.update()
        self.selected.emit(sel)
        self.close()

    # --- отрисовка ---
    def paintEvent(self, ev):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pixmap)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self._sel and self._sel.width() > 1:
            p.save()
            p.setClipRect(self._sel)
            p.drawPixmap(0, 0, self._pixmap)
            p.restore()
            pen = QPen(QColor("#4fc3f7"), 2)
            p.setPen(pen)
            p.drawRect(self._sel.adjusted(0, 0, -1, -1))
            # подсказка с размером
            label = f"{int(self._sel.width() * self._dpr)}×{int(self._sel.height() * self._dpr)} px"
            p.setFont(self._font)
            fm = p.fontMetrics()
            tw, th = fm.horizontalAdvance(label) + 12, fm.height() + 6
            bx = self._sel.left()
            by = self._sel.top() - th - 4
            if by < 0:
                by = self._sel.bottom() + 4
            p.fillRect(QRect(bx, by, tw, th), QColor(20, 22, 28, 210))
            p.setPen(QColor("#e6f4ff"))
            p.drawText(QRect(bx, by, tw, th), Qt.AlignmentFlag.AlignCenter, label)
        else:
            p.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(self.rect().adjusted(0, 40, 0, 0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       "Обведите текст  ·  Esc — отмена")
        p.end()


# --------------------------------------------------------------------------
# панель результата
# --------------------------------------------------------------------------

class SwapButton(QPushButton):
    """Кнопка смены направления: в покое одна стрелка, при наведении — двойная."""

    def __init__(self, text: str = "→"):
        super().__init__(text)
        self.setObjectName("chip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(42)

    def enterEvent(self, ev):
        self.setText("⇄")
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self.setText("→")
        super().leaveEvent(ev)


class Panel(QWidget):
    """Окно с переводом: появляется рядом с выделением, можно тащить мышью."""

    swap_requested = Signal()          # поменять направление перевода
    target_selected = Signal(str)      # выбран язык перевода ("ru" / "en" / …)
    engine_selected = Signal(str)      # выбран сервис перевода

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowOpacity(0.97)
        self._drag: QPoint | None = None
        self._translation = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = QWidget(self)
        self.card.setObjectName("card")
        outer.addWidget(self.card)

        cfg = st_config.load()
        panel_cfg = cfg.get("panel", {})
        dst_size = int(panel_cfg.get("font_size", 15))
        card_w = int(panel_cfg.get("width", 560))

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)

        # строка управления: языки · направление · сервис · скорость
        top = QHBoxLayout()
        top.setSpacing(6)
        self.chip_lang = QPushButton("…")
        self.chip_lang.setObjectName("chip")
        self.chip_lang.setToolTip("Выбрать язык перевода")
        self.chip_lang.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_swap = SwapButton("→")
        self.btn_swap.setToolTip("Поменять направление перевода")
        self.chip_engine = QPushButton("…")
        self.chip_engine.setObjectName("chip")
        self.chip_engine.setToolTip("Выбрать сервис перевода")
        self.chip_engine.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_ping = QLabel("")
        self.lbl_ping.setObjectName("ping")
        top.addWidget(self.chip_lang)
        top.addWidget(self.btn_swap)
        top.addWidget(self.chip_engine)
        top.addWidget(self.lbl_ping)
        top.addStretch(1)
        self.btn_copy = QPushButton("Копировать")
        self.btn_copy.setObjectName("ghost")
        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("ghost")
        self.btn_close.setFixedWidth(30)
        top.addWidget(self.btn_copy)
        top.addWidget(self.btn_close)
        lay.addLayout(top)

        # Пояснение, если выбранный сервис не ответил и перевод сделал другой.
        self.lbl_note = QLabel("")
        self.lbl_note.setObjectName("note")
        self.lbl_note.setWordWrap(True)
        self.lbl_note.hide()
        lay.addWidget(self.lbl_note)

        self.lbl_src = QLabel("")
        self.lbl_src.setObjectName("src")
        self.lbl_src.setWordWrap(True)
        self.lbl_src.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.lbl_src.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_src)

        self.lbl_dst = QLabel("Распознаю…")
        self.lbl_dst.setObjectName("dst")
        self.lbl_dst.setWordWrap(True)
        self.lbl_dst.setMinimumWidth(card_w)
        self.lbl_dst.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.lbl_dst.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_dst)

        self.setStyleSheet(f"""
            #card {{
                background: rgba(18, 20, 26, 240);
                border: 1px solid rgba(255,255,255,35);
                border-radius: 14px;
            }}
            QLabel {{ color: #e8eaed; }}
            QPushButton#chip {{
                color: #8ab4f8; background: rgba(138,180,248,26);
                border: 1px solid rgba(138,180,248,80); border-radius: 8px;
                padding: 4px 10px; font-size: 12px; font-weight: 600; min-height: 26px;
            }}
            QPushButton#chip:hover {{ background: rgba(138,180,248,70); color: #ffffff; }}
            QPushButton#chip:pressed {{ background: rgba(138,180,248,110); }}
            #ping  {{ color: #7f8590; font-size: 11px; }}
            #src   {{ color: #9aa0a6; font-size: 11px; }}
            #dst   {{ color: #ffffff; font-size: {dst_size}px; font-weight: 600; }}
            #note  {{ color: #ffb74d; font-size: 11px; }}
            QPushButton#ghost {{
                color: #cfd6e4; background: rgba(255,255,255,18);
                border: 1px solid rgba(255,255,255,30); border-radius: 7px;
                padding: 5px 10px; font-size: 11px; min-height: 26px;
            }}
            QPushButton#ghost:hover {{ background: rgba(255,255,255,40); }}
        """)
        self.setWindowOpacity(float(panel_cfg.get("opacity", 0.97)))
        self.btn_copy.clicked.connect(self.copy)
        self.btn_close.clicked.connect(self.close_panel)
        self.btn_swap.clicked.connect(self.swap_requested.emit)
        self.chip_lang.clicked.connect(self._lang_menu)
        self.chip_engine.clicked.connect(self._engine_menu)

        # заполняется приложением
        self.targets: list[tuple[str, str]] = [("ru", "Русский"), ("en", "English")]
        self.engines: list[tuple[str, str, bool]] = []
        self._cur_src = "EN"
        self._cur_dst = "RU"
        self._engine_health: dict[str, dict] = {}

    # --- меню на кнопках ---
    def _popup(self, button: QPushButton, menu: QMenu):
        return menu.exec(button.mapToGlobal(QPoint(0, button.height() + 6)))

    def _lang_menu(self):
        menu = QMenu(self)
        for code, title in self.targets:
            act = menu.addAction(f"{title}   ({code})")
            act.setData(code)
        act = self._popup(self.chip_lang, menu)
        if act is not None and act.data():
            self.target_selected.emit(str(act.data()))

    def _measure_health(self, names: list[str]):
        """Проверяем сервисы настоящим коротким переводом (а не TCP-пингом)."""
        try:
            self._engine_health = probe_health(names)
        except Exception:
            self._engine_health = {name: {"ok": None, "ms": None, "info": "нет ответа"}
                                   for name in names}

    def _engine_menu(self):
        enabled = [name for name, _label, ok in self.engines if ok]
        if enabled:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._measure_health(enabled)
            finally:
                QApplication.restoreOverrideCursor()
        menu = QMenu(self)
        for name, label, ok in self.engines:
            if not ok:
                suffix = "   (нужен ключ)"
            else:
                suffix = health_suffix(self._engine_health.get(name))
            act = menu.addAction(label + suffix)
            act.setData(name)
        act = self._popup(self.chip_engine, menu)
        if act is not None and act.data():
            self.engine_selected.emit(str(act.data()))

    # --- api ---
    def set_loading(self, text_error: str | None = None):
        self.lbl_note.clear()
        self.lbl_note.hide()
        if text_error:
            self.lbl_ping.setStyleSheet("color:#ff8a80; font-size:11px;")
            self.lbl_ping.setText("ошибка")
            self.lbl_dst.setText(text_error)
        else:
            self.lbl_ping.setStyleSheet("")
            self.lbl_ping.setText("распознаю…")
            self.lbl_dst.setText("…")
        self._fit()

    def _fit(self):
        """Подогнать размер панели под новый текст.

        QLabel с переносом слов пересчитывает высоту не сразу, поэтому
        adjustSize() без activate() отстаёт на один шаг: после длинного
        перевода короткий текст остаётся в панели старой высоты и висит
        посередине с пустотой сверху и снизу.
        """
        lay = self.card.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.adjustSize()

    def set_result(self, source: str, translation: str, meta: dict):
        """meta: source_lang, target_lang, engine, engine_label, ocr_ms."""
        self._translation = translation
        self._cur_src = str(meta.get("source_lang") or "").upper()
        self._cur_dst = str(meta.get("target_lang") or "").upper()
        self.chip_lang.setText(f"{self._cur_src} → {self._cur_dst}")
        engine = str(meta.get("engine") or "")
        label = str(meta.get("engine_label") or engine or "…")
        preferred = str(meta.get("preferred_engine") or "")
        errors = [str(e) for e in (meta.get("engine_errors") or [])]
        fallback = bool(preferred and engine and engine not in ("cache", preferred))
        self.chip_engine.setText(label + (" ⚠" if fallback else ""))
        self.chip_engine.setToolTip(
            f"Сервис перевода: {engine or '…'} — нажмите, чтобы сменить")
        if fallback:
            reason = ""
            for item in errors:
                if item.startswith(preferred + ":") and ":" in item:
                    reason = item.split(":", 1)[1].strip()
                    break
            who = (st_translate.SERVICE_BY_NAME.get(preferred) or {}).get("label", preferred)
            why = st_translate.short_error(reason) if reason else "не ответил"
            # Причина видна сразу, а не только в подсказке: иначе кажется,
            # что выбор сервиса не сохранился.
            self.lbl_note.setText(f"{who}: {why} — перевёл {label}")
            self.lbl_note.show()
            self.chip_engine.setToolTip(
                f"Выбран {who}, но он не ответил ({why}). Перевёл {label}. "
                f"Нажмите, чтобы сменить сервис.")
        else:
            self.lbl_note.clear()
            self.lbl_note.hide()
        ocr_ms = meta.get("ocr_ms")
        ms = meta.get("ms")
        if meta.get("cached"):
            self.lbl_ping.setText("из кэша")
            self.lbl_ping.setToolTip("")
        elif ms:
            self.lbl_ping.setText(f"перевод {ms} мс")
            self.lbl_ping.setToolTip(f"OCR: {ocr_ms} мс" if ocr_ms else "")
        elif ocr_ms:
            self.lbl_ping.setText(f"OCR {ocr_ms} мс")
            self.lbl_ping.setToolTip("")
        else:
            self.lbl_ping.setText("")
            self.lbl_ping.setToolTip("")
        self.lbl_src.setText(source if len(source) <= 400 else source[:400] + "…")
        self.lbl_dst.setText(translation)
        self._fit()

    def copy(self):
        QGuiApplication.clipboard().setText(self._translation or self.lbl_dst.text())
        self.btn_copy.setText("Скопировано")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1200, lambda: self.btn_copy.setText("Копировать"))

    def close_panel(self):
        self.hide()

    def event(self, ev):
        # клик мимо панели (в любое другое окно / на пустой рабочий стол) — убираем перевод
        if ev.type() == QEvent.Type.WindowDeactivate and self.isVisible():
            try:
                hide_away = bool(st_config.load().get("panel", {}).get("hide_on_click_away", True))
            except Exception:
                hide_away = True
            if hide_away:
                self.hide()
        return super().event(ev)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.hide()
        elif ev.matches(ev.StandardKey.Copy):
            self.copy()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def place_near(self, screen, rect: QRect):
        self._fit()
        w, h = self.width(), self.height()
        geo = screen.geometry()
        x = rect.left()
        y = rect.bottom() + 10
        if y + h > geo.bottom():
            y = rect.top() - h - 10
        x = max(geo.left() + 8, min(x, geo.right() - w - 8))
        y = max(geo.top() + 8, min(y, geo.bottom() - h - 8))
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()


# --------------------------------------------------------------------------
# фоновый конвейер
# --------------------------------------------------------------------------

class Worker(QThread):
    # source_text, translation, error, meta(dict)
    done = Signal(str, str, str, object)

    def __init__(self, crop=None, languages=None, target: str = "auto",
                 use_adaptive: bool = False, text: str | None = None,
                 source: str = "auto", only_engine: str | None = None,
                 use_cache: bool = True):
        super().__init__()
        self.crop = crop
        self.languages = languages or ["ru", "en"]
        self.target = target
        self.use_adaptive = use_adaptive
        self.text = text              # если задан — OCR не нужен (повторный перевод)
        self.source = source
        self.only_engine = only_engine
        self.use_cache = use_cache

    def run(self):
        import st_ocr
        import st_translate

        try:
            ocr_ms = 0
            if self.text is None:
                t0 = time.monotonic()
                res = st_ocr.recognize_sync(self.crop, languages=self.languages,
                                            use_adaptive=self.use_adaptive)
                ocr_ms = int((time.monotonic() - t0) * 1000)
                text = res.text
                # В переводчик уходят абзацы: перенесённые строки уже склеены,
                # иначе фраза рвётся на куски и переводится по частям.
                translate_text = "\n".join(res.paragraphs) if res.paragraphs else text
                src = "auto" if res.language in ("mixed", "") else res.language.split("-")[0]
            else:
                text = self.text
                translate_text = text
                src = self.source
            if not text.strip():
                self.done.emit("", "", "Текст не распознан — попробуйте обвести точнее", {})
                return
            target = None if self.target in ("auto", "", None) else self.target
            tr = st_translate.translate(translate_text, target=target, source=src,
                                        use_cache=self.use_cache,
                                        only_engine=self.only_engine)
            svc = st_translate.SERVICE_BY_NAME.get(tr.engine) or {}
            meta = {
                "source_lang": tr.source_lang.upper(),
                "target_lang": tr.target_lang.upper(),
                "engine": tr.engine,
                "engine_label": svc.get("label", tr.engine),
                "ocr_ms": ocr_ms,
                "ms": tr.ms,
                "cached": tr.cached,
                # Кто выбран и почему перевод сделал другой сервис.
                "preferred_engine": tr.preferred,
                "engine_errors": tr.errors,
                # Повторный перевод (смена языка/движка) должен идти по тому же
                # склеенному тексту, а не по исходным строкам OCR.
                "translate_text": translate_text,
            }
            self.done.emit(text, tr.text, "", meta)
        except Exception as exc:
            self.done.emit("", "", f"Ошибка перевода: {exc}", {})


class EngineTestSignals(QObject):
    done = Signal(str, str)       # error, успешный текст


class EngineTest(QRunnable):
    """Пробный перевод для кнопки в настройках — вне UI-потока."""

    def __init__(self, name: str, cfg: dict):
        super().__init__()
        self.name = name
        self.cfg = cfg
        self.signals = EngineTestSignals()

    def run(self):
        try:
            tr = st_translate.translate(
                "Hello, world! This is a test.", target="ru", source="en",
                use_cache=False, only_engine=self.name, cfg=self.cfg)
            self.signals.done.emit("", f"Работает: «{tr.text}» ({tr.ms} мс)")
        except Exception as exc:
            self.signals.done.emit(str(exc), "")


class HealthSignals(QObject):
    done = Signal(object)         # {name: {"ok", "ms", "info"}}


class HealthCheck(QRunnable):
    """Фоновая проверка сервисов: кто реально переводит, а кто отдаёт ошибку."""

    def __init__(self, names: list[str]):
        super().__init__()
        self.names = names
        self.signals = HealthSignals()

    def run(self):
        try:
            result = st_translate.probe_health(self.names, timeout=5.0)
        except Exception:
            result = {}
        self.signals.done.emit(result)


# --------------------------------------------------------------------------
# окно настроек
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# автозагрузка и иконки
# --------------------------------------------------------------------------

LEGACY_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_RUN_NAME = "ScreenToTranslate"
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
AUTOSTART_LNK = os.path.join(STARTUP_DIR, "ScreenToTranslate.lnk")


def _pythonw() -> str:
    exe_dir = os.path.dirname(sys.executable)
    pyw = os.path.join(exe_dir, "pythonw.exe")
    return pyw if os.path.exists(pyw) else sys.executable


def _remove_legacy_registry() -> None:
    """Убирает старую запись из HKCU\\...\\Run (раньше автозапуск жил там)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LEGACY_RUN_KEY,
                            access=winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, APP_RUN_NAME)
            except FileNotFoundError:
                pass
    except Exception:
        pass


def autostart_enabled() -> bool:
    return os.path.isfile(AUTOSTART_LNK)


def set_autostart(enable: bool) -> tuple[bool, str]:
    """Включает/выключает запуск при загрузке ПК ярлыком в папке «Автозагрузка»."""
    try:
        if enable:
            os.makedirs(STARTUP_DIR, exist_ok=True)
            script = os.path.join(HERE, "st_app.py")
            q = lambda s: "'" + str(s).replace("'", "''") + "'"
            ps = (
                "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s);"
                "$s.TargetPath=%s;"
                "$s.Arguments=%s;"
                "$s.WorkingDirectory=%s;"
                "$s.WindowStyle=7;"
                "$s.Description=%s;"
                "$s.Save()"
            ) % (q(AUTOSTART_LNK), q(_pythonw()), q(f'"{script}"'), q(HERE),
                 q("ScreenToTranslate — перевод с экрана по клавише Ё"))
            import subprocess
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                check=True, capture_output=True, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            try:
                os.remove(AUTOSTART_LNK)
            except FileNotFoundError:
                pass
        # На всякий случай убираем старую запись из реестра, чтобы не было двойного запуска.
        _remove_legacy_registry()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def make_gear_icon(size: int = 64) -> QIcon:
    """Синяя шестерёнка — иконка окна настроек (видно, что это настройки программы)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = size / 2.0
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#4fc3f7"))
    for i in range(8):
        p.save()
        p.translate(c, c)
        p.rotate(i * 45)
        p.drawRoundedRect(QRectF(-size * 0.10, -size * 0.47, size * 0.20, size * 0.22),
                          size * 0.05, size * 0.05)
        p.restore()
    p.drawEllipse(QPointF(c, c), size * 0.33, size * 0.33)
    p.setBrush(QColor("#0b1018"))
    p.drawEllipse(QPointF(c, c), size * 0.13, size * 0.13)
    p.end()
    return QIcon(pm)

class HotkeyCaptureDialog(QDialog):
    """Ловит нажатие клавиши/сочетания и возвращает его в виде Win32 (mods, vk)."""

    def __init__(self, parent=None, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Новая горячая клавиша")
        self.setWindowIcon(make_gear_icon())
        self.setModal(True)
        self.setFixedSize(440, 170)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        lay = QVBoxLayout(self)
        title = QLabel("Нажмите нужное сочетание клавиш")
        title.setStyleSheet("font-size:14px; font-weight:600;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        self.lbl = QLabel(current or "…")
        self.lbl.setStyleSheet(
            "font-size:20px; font-weight:700; color:#4fc3f7;"
            "background:rgba(79,195,247,26); border:1px solid rgba(79,195,247,90);"
            "border-radius:8px; padding:8px;")
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.lbl)
        hint = QLabel("Подойдёт любая клавиша или сочетание, например Alt+T, Ctrl+Shift+F8, Ё.\n"
                      "Esc — отмена.")
        hint.setStyleSheet("color:#8a8f98; font-size:11px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hint)
        self.mods = 0
        self.vk = 0
        self.text_value = ""

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        # сами модификаторы жать не считаем — ждём настоящую клавишу
        if ev.key() in (Qt.Key.Key_Alt, Qt.Key.Key_Control, Qt.Key.Key_Shift,
                        Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
            return
        mods = 0
        m = ev.modifiers()
        if m & Qt.KeyboardModifier.AltModifier:
            mods |= MOD_ALT
        if m & Qt.KeyboardModifier.ControlModifier:
            mods |= MOD_CONTROL
        if m & Qt.KeyboardModifier.ShiftModifier:
            mods |= MOD_SHIFT
        if m & Qt.KeyboardModifier.MetaModifier:
            mods |= MOD_WIN
        vk = int(ev.nativeVirtualKey() or 0)
        if not vk:
            return
        parts = []
        if mods & MOD_CONTROL:
            parts.append("Ctrl")
        if mods & MOD_ALT:
            parts.append("Alt")
        if mods & MOD_SHIFT:
            parts.append("Shift")
        if mods & MOD_WIN:
            parts.append("Win")
        name = QKeySequence(ev.key()).toString() or (ev.text().upper() if ev.text() else f"VK{vk:02X}")
        self.mods, self.vk = mods, vk
        self.text_value = "+".join(parts + [name])
        self.lbl.setText(self.text_value)
        self.accept()


class PingComboBox(QComboBox):
    """QComboBox, который сообщает перед показом списка — чтобы обновить пинг."""

    popup_about_to_show = Signal()

    def showPopup(self):
        self.popup_about_to_show.emit()
        super().showPopup()


class SettingsDialog(QDialog):
    """Настройки: горячая клавиша, автозапуск, направление перевода, сервис и ключи."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ScreenToTranslate — настройки")
        self.setWindowIcon(make_gear_icon())
        # Tool: окно не занимает место в панели задач
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setModal(True)
        self.setMinimumWidth(560)
        cfg = st_config.load()
        self._services = st_translate.SERVICES
        self._mods, self._vk, self._hk_text = hotkey_of(cfg)
        self._test_job = None
        self._health: dict[str, dict] = {}
        self._health_running = False

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # --- горячая клавиша и запуск ---
        box_hot = QGroupBox("Запуск и горячая клавиша")
        hot = QVBoxLayout(box_hot)
        row = QHBoxLayout()
        row.addWidget(QLabel("Включать перевод:"))
        self.btn_hotkey = QPushButton(self._hk_text)
        self.btn_hotkey.setObjectName("hotkey")
        self.btn_hotkey.setToolTip("Нажмите, чтобы задать любое сочетание клавиш")
        row.addWidget(self.btn_hotkey)
        row.addWidget(QLabel("— нажмите, чтобы сменить"))
        row.addStretch(1)
        hot.addLayout(row)

        self.chk_autostart = QCheckBox("Запускать при загрузке ПК (автозагрузка)")
        self.chk_autostart.setChecked(autostart_enabled())
        hot.addWidget(self.chk_autostart)

        self.chk_auto_dir = QCheckBox(
            "Автоперевод: определять направление по тексту (русский → английский, английский → русский)")
        self.chk_auto_dir.setChecked(bool(cfg.get("auto_direction", True)))
        self.chk_auto_dir.setToolTip(
            "Язык определяется по большинству букв в распознанном тексте")
        hot.addWidget(self.chk_auto_dir)

        self.chk_hide = QCheckBox("Убирать перевод при клике мимо панели")
        self.chk_hide.setChecked(bool(cfg.get("panel", {}).get("hide_on_click_away", True)))
        hot.addWidget(self.chk_hide)
        lay.addWidget(box_hot)

        # --- сервис перевода ---
        box_srv = QGroupBox("Сервис перевода")
        srv = QVBoxLayout(box_srv)
        self.combo = PingComboBox()
        for svc in self._services:
            self.combo.addItem(svc["label"], svc["name"])
        current = st_translate.normalize_service((cfg.get("engines") or {}).get("primary"))
        idx = self.combo.findData(current)
        self.combo.setCurrentIndex(max(0, idx))
        srv.addWidget(self.combo)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#8a8f98; font-size:11px;")
        srv.addWidget(self.hint)

        self.form = QFormLayout()
        self.lbl_key = QLabel("Ключ")
        self.ed_key = QLineEdit()
        self.ed_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.lbl_extra = QLabel("Регион")
        self.ed_extra = QLineEdit()
        self.form.addRow(self.lbl_key, self.ed_key)
        self.form.addRow(self.lbl_extra, self.ed_extra)
        srv.addLayout(self.form)

        self.btn_test = QPushButton("Проверить перевод")
        srv.addWidget(self.btn_test)
        self.lbl_test = QLabel("")
        self.lbl_test.setWordWrap(True)
        srv.addWidget(self.lbl_test)
        lay.addWidget(box_srv)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        author = QLabel("ScreenToTranslate · автор edrusha")
        author.setStyleSheet("color:#5f6670; font-size:10px;")
        author.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(author)

        self.combo.currentIndexChanged.connect(self._sync)
        self.combo.popup_about_to_show.connect(self._refresh_service_health)
        self.btn_test.clicked.connect(self._test)
        self.btn_hotkey.clicked.connect(self._pick_hotkey)
        self._sync()
        self._start_health_check()

    # --- helpers ---
    def _apply_health(self):
        for i, svc in enumerate(self._services):
            self.combo.setItemText(
                i, svc["label"] + health_suffix(self._health.get(svc["name"])))

    def _start_health_check(self):
        """Сразу при открытии настроек — кто работает, а кто отдаёт ошибку."""
        if self._health_running:
            return
        self._health_running = True
        job = HealthCheck([svc["name"] for svc in self._services])
        job.signals.done.connect(self._on_health_done)
        QThreadPool.globalInstance().start(job)

    def _on_health_done(self, result):
        self._health_running = False
        self._health = result or {}
        self._apply_health()

    def _refresh_service_health(self):
        """Свежая проверка при каждом открытии списка сервисов."""
        names = [svc["name"] for svc in self._services]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._health = probe_health(names)
        except Exception:
            self._health = {}
        finally:
            QApplication.restoreOverrideCursor()
        self._apply_health()

    def _service(self) -> dict:
        return self._services[max(0, self.combo.currentIndex())]

    def _key_field(self, name: str) -> str:
        """В каком поле конфига лежит ключ выбранного сервиса."""
        return {"google_api": "google_api", "bing_api": "azure", "deepl_api": "deepl",
                "yandex_api": "yandex"}.get(name, name)

    def _pick_hotkey(self):
        dlg = HotkeyCaptureDialog(self, self._hk_text)
        if dlg.exec() and dlg.vk:
            self._mods, self._vk, self._hk_text = dlg.mods, dlg.vk, dlg.text_value
            self.btn_hotkey.setText(self._hk_text)

    def _collect(self) -> dict:
        cfg = st_config.load()
        svc = self._service()
        name = svc["name"]
        eng = cfg.setdefault("engines", {})
        if self._vk:
            cfg["hotkey"] = {"mods": self._mods, "vk": self._vk, "text": self._hk_text}
            cfg.pop("hotkey_mode", None)
        cfg["auto_direction"] = self.chk_auto_dir.isChecked()
        cfg["autostart"] = self.chk_autostart.isChecked()
        cfg.setdefault("panel", {})["hide_on_click_away"] = self.chk_hide.isChecked()
        eng["primary"] = name
        eng["order"] = [name] + [n for n in (eng.get("order") or []) if n != name]
        if svc["needs_key"]:
            eng[self._key_field(name)] = self.ed_key.text().strip()
            extra = self.ed_extra.text().strip()
            if name == "bing_api":
                eng["azure_region"] = extra
            elif name == "yandex_api":
                eng["yandex_folder"] = extra
        return cfg

    def _sync(self):
        svc = self._service()
        name = svc["name"]
        needs = svc["needs_key"]
        if needs:
            self.hint.setText(svc.get("key_hint", ""))
        else:
            self.hint.setText(svc.get("hint", "") + " Ключ не нужен — сервис работает без регистрации.")
        self.lbl_key.setVisible(needs)
        self.ed_key.setVisible(needs)
        extra = needs and name in ("bing_api", "yandex_api")
        self.lbl_extra.setVisible(extra)
        self.ed_extra.setVisible(extra)
        self.lbl_extra.setText("folderId" if name == "yandex_api" else "Регион")
        eng = st_config.load().get("engines", {})
        self.ed_key.setText(eng.get(self._key_field(name), "") if needs else "")
        self.ed_extra.setText(eng.get("yandex_folder" if name == "yandex_api" else "azure_region", ""))
        self.btn_test.setVisible(needs)
        self.lbl_test.setText("")

    def _test(self):
        if self._test_job is not None:
            return
        cfg = self._collect()
        name = self._service()["name"]
        self.lbl_test.setText("Проверяю…")
        self.lbl_test.setStyleSheet("color:#8a8f98; font-size:12px;")
        self.btn_test.setEnabled(False)
        job = EngineTest(name, cfg)
        self._test_job = job
        job.signals.done.connect(self._on_test_done)
        QThreadPool.globalInstance().start(job)

    def _on_test_done(self, error: str, text: str):
        self._test_job = None
        self.btn_test.setEnabled(True)
        if error:
            self.lbl_test.setText(f"Не работает: {error}")
            self.lbl_test.setStyleSheet("color:#c62828; font-size:12px;")
        else:
            self.lbl_test.setText(text)
            self.lbl_test.setStyleSheet("color:#2e7d32; font-size:12px;")

    def save(self):
        cfg = self._collect()
        try:
            st_config.save(cfg)
        except Exception as exc:
            QMessageBox.warning(self, "Настройки",
                                f"Не удалось сохранить настройки: {exc}")
            return
        ok, err = set_autostart(bool(cfg.get("autostart")))
        if not ok and err:
            QMessageBox.warning(self, "Автозагрузка",
                                f"Не удалось изменить автозагрузку: {err}")
            return
        self.accept()


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, on_hotkey):
        super().__init__()
        self.on_hotkey = on_hotkey

    def nativeEventFilter(self, event_type, message):
        try:
            msg = MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self.on_hotkey(int(msg.wParam))
                return True, 0
        except Exception:
            pass
        return False, 0


class App(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.overlay: Overlay | None = None
        self.panel = Panel()
        self.worker: Worker | None = None
        self._workers: list[Worker] = []
        self._job_seq = 0
        self._current_job = 0
        self.last_region: tuple | None = None
        self.last_text: str | None = None
        self.last_translate_text: str | None = None   # склеенные абзацы для повтора
        self.last_screen = None
        self.last_rect: QRect | None = None
        self.last_meta: dict = {}
        self._shot = None
        self._origin = None

        self.panel.swap_requested.connect(self._on_swap)
        self.panel.target_selected.connect(self._on_target)
        self.panel.engine_selected.connect(self._on_engine)
        self._refresh_panel_lists()

        self._anchor = QWidget()
        self._anchor.setWindowFlag(Qt.WindowType.Tool)
        self._anchor.resize(1, 1)
        self._anchor.show()
        self._anchor.hide()
        self._hwnd = int(self._anchor.winId())

        self._filter = HotkeyFilter(self._on_hotkey)
        app.installNativeEventFilter(self._filter)
        self._registered = self._register_hotkeys()
        self._tray = self._make_tray()

        # Один раз после старта проверяем основной сервис в фоне: если он
        # заблокирован (429), первый перевод не потратит на него время.
        # Постоянного таймера нет — в простое приложение в сеть не ходит.
        QTimer.singleShot(2000, self._warm_translator)
        if 1 not in self._registered:
            self._tray.showMessage(
                "ScreenToTranslate",
                "Не удалось занять основной хоткей — его держит другая программа. "
                "Задайте другое сочетание в настройках.",
                QSystemTrayIcon.MessageIcon.Warning, 6000)

    # --- горячие клавиши ---
    def _register_hotkeys(self) -> list[int]:
        cfg = st_config.load()
        mods, vk, _label = hotkey_of(cfg)
        ok = []
        self._hotkey_fallback = None
        # id 1 — хоткей из настроек; id 3 — «повторить последнюю область» (Ctrl+Alt+Ё).
        # Если основной занят другой программой (Win32 1409), пробуем запасные:
        # на «Ё» часто сидит переводчик Яндекс.Браузера.
        hwnd = wt.HWND(self._hwnd)
        if user32.RegisterHotKey(hwnd, 1, mods | MOD_NOREPEAT, vk):
            ok.append(1)
        else:
            # Ctrl+Alt+Ё уже занят повтором последней области (id 3), поэтому первым
            # запасным вариантом идёт Alt+Shift+Ё.
            for m, k, label in ((MOD_ALT | MOD_SHIFT, vk, f"Alt+Shift+{_label.split('+')[-1]}"),
                                (MOD_CONTROL | MOD_ALT, vk, f"Ctrl+Alt+{_label.split('+')[-1]}")):
                if user32.RegisterHotKey(hwnd, 1, m | MOD_NOREPEAT, k):
                    ok.append(1)
                    self._hotkey_fallback = (m, k, label)
                    break
        if user32.RegisterHotKey(hwnd, 3, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_OEM_3):
            ok.append(3)
        return ok

    def hotkey_label(self) -> str:
        if getattr(self, "_hotkey_fallback", None):
            return self._hotkey_fallback[2]
        if 1 not in getattr(self, "_registered", []):
            return "хоткей занят"
        return hotkey_of(st_config.load())[2]

    def _unregister(self):
        for hid in self._registered:
            user32.UnregisterHotKey(wt.HWND(self._hwnd), hid)

    # --- трей ---
    def _make_tray(self) -> QSystemTrayIcon:
        icon = QIcon(_ICON_PATH) if os.path.isfile(_ICON_PATH) else QIcon()
        if icon.isNull():
            pm = QPixmap(64, 64)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor("#4fc3f7"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(4, 4, 56, 56, 14, 14)
            p.setPen(QColor("#0b1018"))
            p.setFont(QFont("Segoe UI", 26, QFont.Weight.Bold))
            p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "Ё")
            p.end()
            icon = QIcon(pm)
        self.app.setWindowIcon(icon)
        tray = QSystemTrayIcon(icon)
        menu = QMenu()
        self._act_now = QAction(f"Перевести область  ({self.hotkey_label()})", menu)
        self._act_now.triggered.connect(lambda: self.start_selection())
        self._act_repeat = QAction("Повторить последнюю область  (Ctrl+Alt+Ё)", menu)
        self._act_repeat.triggered.connect(self.repeat_last)
        self._act_settings = QAction("Настройки…", menu)
        self._act_settings.triggered.connect(self.open_settings)
        self._act_quit = QAction("Выход", menu)
        self._act_quit.triggered.connect(self.quit)
        menu.addAction(self._act_now)
        menu.addAction(self._act_repeat)
        menu.addSeparator()
        menu.addAction(self._act_settings)
        menu.addSeparator()
        menu.addAction(self._act_quit)
        tray.setContextMenu(menu)
        tray.setToolTip(f"ScreenToTranslate — {self.hotkey_label()}: перевод области экрана")
        tray.activated.connect(lambda reason: self.start_selection()
                               if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.show()
        return tray

    # --- настройки ---
    def open_settings(self):
        dlg = SettingsDialog(self._anchor)
        if dlg.exec():
            self._unregister()
            self._registered = self._register_hotkeys()
            label = self.hotkey_label()
            self._act_now.setText(f"Перевести область  ({label})")
            self._tray.setToolTip(f"ScreenToTranslate — {label}: перевод области экрана")
            if 1 not in self._registered:
                self._tray.showMessage(
                    "ScreenToTranslate",
                    "Новый хоткей занят другой программой. Задайте другое сочетание в настройках.",
                    QSystemTrayIcon.MessageIcon.Warning, 6000)
            else:
                self._tray.showMessage("ScreenToTranslate", f"Настройки сохранены. Хоткей: {label}",
                                       QSystemTrayIcon.MessageIcon.Information, 2500)

    # --- конвейер ---
    def _warm_translator(self):
        """Фоновая проверка основного сервиса при старте (без окон, без перевода текста)."""
        threading.Thread(target=st_translate.warmup, daemon=True).start()

    def _on_hotkey(self, hid: int):
        # Токен Bing нужен только перед переводом. Берём его сейчас, пока
        # пользователь выделяет область, — без фонового таймера в простое.
        st_translate.prewarm_async()
        if hid == 3:
            self.repeat_last()
        else:
            self.start_selection()

    def start_selection(self):
        if self.overlay is not None:
            return
        self.panel.hide()
        shot, origin = st_capture.capture_virtual()
        self._shot, self._origin = shot, origin
        screen, phys = self._screen_under_cursor()
        dpr = screen.devicePixelRatio()
        self.overlay = Overlay(screen, shot, origin, dpr, phys)
        self.overlay.selected.connect(lambda r: self._on_selected(screen, dpr, phys, r))
        self.overlay.cancelled.connect(self._on_cancelled)
        self.overlay.show()

    @staticmethod
    def _screen_under_cursor():
        """Экран под курсором + его физический прямоугольник из Win32.

        Раньше здесь делилось на devicePixelRatio основного монитора — на втором
        мониторе это давало неверную точку, Qt выбирал не тот экран, и оверлей
        «уезжал в бок». Теперь монитор определяется по Win32, а QScreen находится
        сопоставлением размеров: Qt отдаёт дружественные имена («BenQ GW2270»),
        а не «\\\\.\\DISPLAY2», поэтому сравнение по имени не работало."""
        cx, cy = st_capture.cursor_pos()
        info = st_capture.monitor_info_at_point(cx, cy)
        monitors = st_capture.list_monitors()
        screen = QGuiApplication.primaryScreen() or QGuiApplication.screens()[0]
        if not info:
            return screen, (monitors[0] if monitors else
                            (0, 0, int(screen.geometry().width() * screen.devicePixelRatio()),
                             int(screen.geometry().height() * screen.devicePixelRatio())))
        device, phys = info
        screens = QGuiApplication.screens()
        # 1) если Qt всё-таки отдал системное имя — берём точное совпадение
        for scr in screens:
            if scr.name().strip().lower() == device.strip().lower():
                return scr, phys
        # 2) иначе — по физическому размеру монитора (логический размер × dpr)
        cands = []
        for scr in screens:
            dpr = scr.devicePixelRatio() or 1.0
            g = scr.geometry()
            if (abs(round(g.width() * dpr) - phys[2]) <= 2
                    and abs(round(g.height() * dpr) - phys[3]) <= 2):
                cands.append(scr)
        if len(cands) > 1:
            # одинаковые мониторы различаем по положению: физическая точка / dpr должна
            # попадать в логический прямоугольник именно этого экрана
            def score(scr):
                dpr = scr.devicePixelRatio() or 1.0
                g = scr.geometry()
                lx = phys[0] / dpr + phys[2] / dpr / 2
                ly = phys[1] / dpr + phys[3] / dpr / 2
                return abs((g.x() + g.width() / 2) - lx) + abs((g.y() + g.height() / 2) - ly)
            cands.sort(key=score)
        if cands:
            return cands[0], phys
        return screen, phys

    def _on_cancelled(self):
        self.overlay = None

    def _on_selected(self, screen, dpr: float, phys, rect: QRect):
        overlay = self.overlay
        self.overlay = None
        if overlay is None:
            return
        region = overlay.region_pil
        # логические координаты виджета -> физические пиксели снимка
        scale_x = phys[2] / max(1, overlay.width())
        scale_y = phys[3] / max(1, overlay.height())
        box = (int(rect.x() * scale_x), int(rect.y() * scale_y),
               max(1, int(rect.width() * scale_x)), max(1, int(rect.height() * scale_y)))
        crop = region.crop((box[0], box[1], box[0] + box[2], box[1] + box[3]))
        self.last_region = (screen, dpr, phys, box)
        self._run(crop, screen, rect)

    def repeat_last(self):
        if not self.last_region:
            self.start_selection()
            return
        screen, dpr, phys, box = self.last_region
        shot, origin = st_capture.capture_virtual()
        px, py = phys[0] - origin[0], phys[1] - origin[1]
        region = shot.crop((px, py, px + phys[2], py + phys[3]))
        crop = region.crop((box[0], box[1], box[0] + box[2], box[1] + box[3]))
        geo = screen.geometry()
        sx = phys[2] / max(1, geo.width())
        sy = phys[3] / max(1, geo.height())
        rect = QRect(int(box[0] / sx), int(box[1] / sy), int(box[2] / sx), int(box[3] / sy))
        self._run(crop, screen, rect)

    def _launch_worker(self, worker: Worker) -> None:
        """Запускает Worker и держит ссылку на него до завершения.

        Без этого быстрый повторный запуск оставлял предыдущий QThread без ссылки,
        и приложение падало с «QThread: Destroyed while thread is still running».
        """
        self._job_seq += 1
        job = self._job_seq
        self._current_job = job
        self._workers.append(worker)
        self.worker = worker
        worker.done.connect(
            lambda src, tr, err, meta, j=job: self._on_done(src, tr, err, meta, j))
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        worker.start()

    def _worker_finished(self, worker: Worker):
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        if self.worker is worker:
            self.worker = None

    def _run(self, crop, screen, rect: QRect):
        self.panel.set_loading()
        self.panel.place_near(screen, rect)
        self.panel.btn_copy.setText("Копировать")
        self.last_screen, self.last_rect, self.last_text, self.last_meta = screen, rect, None, {}
        self.last_translate_text = None
        cfg = st_config.load()
        worker = Worker(crop, cfg.get("ocr_languages", ["ru", "en"]),
                        self._auto_target(cfg),
                        bool(cfg.get("use_adaptive_ocr", False)))
        self._launch_worker(worker)

    @staticmethod
    def _auto_target(cfg: dict) -> str:
        """Автоперевод: направление определяется по большинству букв (RU↔EN)."""
        if cfg.get("auto_direction", True):
            return "auto"
        return cfg.get("target_lang", "auto")

    # --- действия на панели ---
    def _refresh_panel_lists(self):
        cfg = st_config.load()
        have = {n for n, _f, _l in st_translate._engine_table(cfg)}
        self.panel.targets = [
            ("ru", "Русский"), ("en", "English"), ("de", "Deutsch"), ("fr", "Français"),
            ("es", "Español"), ("it", "Italiano"), ("pl", "Polski"), ("tr", "Türkçe"),
            ("uk", "Українська"), ("ja", "日本語"), ("zh", "中文"), ("ko", "한국어"),
        ]
        self.panel.engines = [(s["name"], s["label"], s["name"] in have)
                              for s in st_translate.SERVICES]

    def _on_swap(self):
        if not self.last_text:
            return
        src = (self.last_meta.get("source_lang") or "EN").lower()
        dst = (self.last_meta.get("target_lang") or "RU").lower()
        self._retranslate(target=src, source=dst)

    def _on_target(self, code: str):
        self._retranslate(target=code)

    def _on_engine(self, name: str):
        cfg = st_config.load()
        eng = cfg.setdefault("engines", {})
        eng["primary"] = name
        eng["order"] = [name] + [n for n in (eng.get("order") or []) if n != name]
        try:
            st_config.save(cfg)
        except Exception as exc:
            QMessageBox.warning(self.panel, "ScreenToTranslate",
                                f"Не удалось сохранить выбор движка: {exc}")
            return
        self._refresh_panel_lists()
        self._retranslate()

    def _retranslate(self, target: str | None = None, source: str | None = None):
        """Повторный перевод того же текста — без нового OCR."""
        if not self.last_text or any(w.isRunning() for w in self._workers):
            return
        cfg = st_config.load()
        self.panel.set_loading()
        worker = Worker(text=self.last_translate_text or self.last_text,
                        source=source or "auto",
                        target=target or self._auto_target(cfg))
        self._launch_worker(worker)

    def _on_done(self, source: str, translation: str, error: str, meta: dict, job: int | None = None):
        # Результат устаревшего запроса не должен перетирать свежий.
        if job is not None and job != self._current_job:
            return
        if error:
            self.panel.set_loading(error)
        else:
            self.last_text = source
            self.last_meta = meta or {}
            self.last_translate_text = self.last_meta.get("translate_text") or source
            self.panel.set_result(source, translation, self.last_meta)

    def quit(self):
        self._unregister()
        self._tray.hide()
        # Даём активным запросам шанс завершиться, чтобы QThread не уничтожился
        # на ходу. Сетевые таймауты могут быть долгими, поэтому ждём не бесконечно.
        for worker in list(self._workers):
            if worker.isRunning() and not worker.wait(3000):
                worker.terminate()
                worker.wait(1000)
        self.app.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    instance = App(app)
    print("DPI:", DPI_MODE, "| hotkeys registered:", instance._registered)
    if 1 not in instance._registered:
        print("ВНИМАНИЕ: основной хоткей занят другой программой — "
              "задайте другое сочетание в настройках.")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
