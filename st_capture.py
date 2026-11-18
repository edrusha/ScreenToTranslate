"""Захват экрана: все мониторы, физические пиксели, DPI per-monitor v2.

Работает через GDI BitBlt по всему виртуальному экрану (включая мониторы
с отрицательными координатами — например, второй экран слева с x=-1920).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SRCCOPY = 0x00CC0020


def enable_per_monitor_dpi() -> str:
    """DPI awareness v2 — без него координаты 5-мониторных конфигураций разъезжаются."""
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    user32.SetProcessDPIAware()
    return "system"


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]


def monitor_info_at_point(x: int, y: int) -> tuple[str, tuple[int, int, int, int]] | None:
    """Монитор под точкой (физ. пиксели) вместе с его системным именем.

    Имя вида '\\\\.\\DISPLAY1' совпадает с QScreen.name() в Qt — по нему
    находим нужный экран без пересчёта координат через devicePixelRatio
    (именно этот пересчёт и сдвигал оверлей на втором мониторе)."""
    MONITOR_DEFAULTTONEAREST = 2
    pt = wt.POINT(int(x), int(y))
    user32.MonitorFromPoint.restype = wt.HMONITOR
    hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    mi = MONITORINFOEXW()
    mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    r = mi.rcMonitor
    return mi.szDevice, (r.left, r.top, r.right - r.left, r.bottom - r.top)


def virtual_screen_rect() -> tuple[int, int, int, int]:
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def list_monitors() -> list[tuple[int, int, int, int]]:
    """Список прямоугольников мониторов в физических пикселях."""
    result: list[tuple[int, int, int, int]] = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM)

    def cb(hmon, hdc, lprect, lparam):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            result.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(cb), 0)
    return result


def list_monitors_named() -> list[tuple[str, tuple[int, int, int, int]]]:
    """Список (имя устройства, прямоугольник) в физических пикселях.

    Имя вида '\\\\.\\DISPLAY1' совпадает с DXGI_OUTPUT_DESC.DeviceName,
    поэтому HDR-захват находит по нему нужный DXGI-выход без догадок.
    """
    result: list[tuple[str, tuple[int, int, int, int]]] = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM)

    def cb(hmon, hdc, lprect, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            result.append((mi.szDevice,
                           (r.left, r.top, r.right - r.left, r.bottom - r.top)))
        return True

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(cb), 0)
    return result


def capture_rect(x: int, y: int, w: int, h: int):
    """Возвращает PIL.Image (RGB) области экрана в физических пикселях."""
    from PIL import Image

    hdc = user32.GetDC(0)
    if not hdc:
        raise OSError("GetDC(0) failed")
    mem = gdi32.CreateCompatibleDC(hdc)
    if not mem:
        user32.ReleaseDC(0, hdc)
        raise OSError("CreateCompatibleDC failed")
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    if not bmp:
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, hdc)
        raise OSError("CreateCompatibleBitmap failed")
    old = gdi32.SelectObject(mem, bmp)
    try:
        ok = gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, SRCCOPY)
        if not ok:
            raise OSError(f"BitBlt failed: {ctypes.GetLastError()}")
        # По MSDN bitmap не должен быть выбран в DC во время GetDIBits.
        gdi32.SelectObject(mem, old)
        old = None
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        lines = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        if lines != h:
            raise OSError(f"GetDIBits returned {lines} of {h} lines")
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        if old is not None:
            gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, hdc)


def capture_virtual():
    """Снимок всего виртуального экрана. Возвращает (image, (x, y)).

    HDR-мониторы перекрываются DXGI-кадром с нормальной экспозицией:
    GDI на них отдаёт выбеленную картинку (scRGB, 1.0 = 80 нит), из-за
    чего текст и фон выглядят засвеченными. Координаты не меняются.
    """
    x, y, w, h = virtual_screen_rect()
    image = capture_rect(x, y, w, h)
    if os.environ.get("ST_HDR", "1") != "0":
        try:
            import st_hdr
            st_hdr.patch_virtual(image, (x, y))
        except Exception:
            # HDR-путь не должен ломать обычный захват: остаёмся на GDI.
            if os.environ.get("ST_HDR_DEBUG"):
                import traceback
                traceback.print_exc()
    return image, (x, y)


def cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def monitor_at(x: int, y: int) -> tuple[int, int, int, int] | None:
    for m in list_monitors():
        if m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3]:
            return m
    return None


if __name__ == "__main__":  # быстрая самопроверка
    print("dpi:", enable_per_monitor_dpi())
    print("virtual:", virtual_screen_rect())
    print("monitors:", list_monitors())
    img, origin = capture_virtual()
    print("captured:", img.size, "origin", origin)
