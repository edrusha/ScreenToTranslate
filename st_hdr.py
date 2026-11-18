"""HDR-захват монитора для ScreenToTranslate.

Зачем: на HDR-мониторе обычный GDI BitBlt отдаёт «выбеленную» картинку —
Windows компонует рабочий стол в линейном scRGB (1.0 = 80 нит), а SDR-белый
у монитора настроен, например, на 280 нит. В 8-битном GDI-снимке всё, что
ярче 80 нит, обрезается, поэтому текст и фон выглядят засвеченными.

Здесь используется тот же путь, что и в HDR Snip: DXGI Desktop Duplication
с запросом R16G16B16A16_FLOAT, затем линейный scRGB пересчитывается в sRGB
с делением на SDR-белый уровень монитора (80/white). Это возвращает картинке
обычный SDR-вид: без засветов, пригодный и для оверлея, и для OCR.

Если DXGI недоступен (занят другой программой, старый драйвер, RDP), вызывающий
код просто остаётся на GDI-снимке.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import os
import struct
import sys
import time
import uuid
from functools import lru_cache

# --------------------------------------------------------------------------
# COM / Win32
# --------------------------------------------------------------------------

HRESULT = ctypes.c_long
DXGI_ERROR_WAIT_TIMEOUT = ctypes.c_long(0x887A0027).value
DXGI_ERROR_ACCESS_LOST = ctypes.c_long(0x887A0026).value
DXGI_ERROR_INVALID_CALL = ctypes.c_long(0x887A0001).value

user32 = ctypes.windll.user32
dxgi = ctypes.WinDLL("dxgi.dll")
d3d11 = ctypes.WinDLL("d3d11.dll")


_trace_t0 = 0.0


def _trace(*parts) -> None:
    """ST_HDR_TRACE=1 — печать шагов захвата (для отладки падений)."""
    if os.environ.get("ST_HDR_TRACE"):
        ms = int((time.monotonic() - _trace_t0) * 1000) if _trace_t0 else 0
        print(f"[hdr {ms:4d}ms]", *parts, file=sys.stderr, flush=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    g = GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
    return g


IID_IDXGIFactory1 = "770aae78-f26f-4dba-a829-253c83d1b387"
IID_IDXGIAdapter1 = "29038f61-3839-4626-91fd-086879011a05"
IID_IDXGIOutput1 = "00cddea8-939b-4b83-a340-a685226666cc"
IID_IDXGIOutput5 = "80a07424-ab52-42eb-833c-0c42fd282d98"
IID_IDXGIOutput6 = "068346e8-aaec-4b84-add7-137f513f77a1"
IID_ID3D11Texture2D = "6f15aaf2-d208-4e89-9ab4-489535d34f9c"

DXGI_FORMAT_R16G16B16A16_FLOAT = 10
DXGI_FORMAT_R10G10B10A2_UNORM = 24
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_FORMAT_B8G8R8A8_UNORM_SRGB = 91

D3D11_DRIVER_TYPE_UNKNOWN = 0
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_SDK_VERSION = 7
D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_MAP_READ = 1
D3D_FEATURE_LEVEL_11_1 = 0xB100
D3D_FEATURE_LEVEL_11_0 = 0xB000


def _vtbl_entry(ptr, index):
    table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return table[index]


def _call(ptr, index, restype, argtypes, *args):
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(_vtbl_entry(ptr, index))(ptr, *args)


def _release(ptr) -> None:
    if ptr is None:
        return
    if isinstance(ptr, ctypes.c_void_p):
        if not ptr.value:
            return
    try:
        _call(ptr, 2, ctypes.c_ulong, ())
    except Exception:
        pass
    if isinstance(ptr, ctypes.c_void_p):
        ptr.value = None


def _qi(ptr, iid: str):
    out = ctypes.c_void_p()
    g = _guid(iid)
    hr = _call(ptr, 0, HRESULT,
               (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
               ctypes.byref(g), ctypes.byref(out))
    if hr < 0 or not out.value:
        return None
    return out


# --------------------------------------------------------------------------
# DisplayConfig: HDR-флаг и уровень SDR-белого
# --------------------------------------------------------------------------

QDC_ONLY_ACTIVE_PATHS = 2
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9
DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL = 11


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32),
                ("adapterId", LUID), ("id", ctypes.c_uint32)]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshRate", DISPLAYCONFIG_RATIONAL),
                ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", wt.BOOL),
                ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
                ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
                ("flags", ctypes.c_uint32)]


class DISPLAYCONFIG_2DREGION(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_uint32), ("cy", ctypes.c_uint32)]


class DISPLAYCONFIG_VIDEO_SIGNAL_INFO(ctypes.Structure):
    _fields_ = [("pixelRate", ctypes.c_uint64),
                ("hSyncFreq", DISPLAYCONFIG_RATIONAL),
                ("vSyncFreq", DISPLAYCONFIG_RATIONAL),
                ("activeSize", DISPLAYCONFIG_2DREGION),
                ("totalSize", DISPLAYCONFIG_2DREGION),
                ("videoStandard", ctypes.c_uint32),
                ("scanLineOrdering", ctypes.c_uint32)]


class DISPLAYCONFIG_TARGET_MODE(ctypes.Structure):
    _fields_ = [("targetVideoSignalInfo", DISPLAYCONFIG_VIDEO_SIGNAL_INFO)]


class DISPLAYCONFIG_SOURCE_MODE(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
                ("pixelFormat", ctypes.c_uint32), ("position", wt.POINT)]


class DISPLAYCONFIG_DESKTOP_IMAGE_INFO(ctypes.Structure):
    _fields_ = [("PathSourceSize", wt.POINT), ("DesktopImageRegion", wt.RECT),
                ("DesktopImageClip", wt.RECT)]


class _MODE_UNION(ctypes.Union):
    _fields_ = [("sourceMode", DISPLAYCONFIG_SOURCE_MODE),
                ("targetMode", DISPLAYCONFIG_TARGET_MODE),
                ("desktopImageInfo", DISPLAYCONFIG_DESKTOP_IMAGE_INFO)]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32),
                ("adapterId", LUID), ("mode", _MODE_UNION)]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("viewGdiDeviceName", wt.WCHAR * 32)]


class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("value", ctypes.c_uint32), ("colorEncoding", ctypes.c_uint32),
                ("colorBitDepth", ctypes.c_uint32)]


class DISPLAYCONFIG_SDR_WHITE_LEVEL(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
                ("SDRWhiteLevel", wt.ULONG)]


user32.GetDisplayConfigBufferSizes.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
user32.GetDisplayConfigBufferSizes.restype = ctypes.c_long
user32.QueryDisplayConfig.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(DISPLAYCONFIG_PATH_INFO), ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(DISPLAYCONFIG_MODE_INFO), ctypes.c_void_p]
user32.QueryDisplayConfig.restype = ctypes.c_long
user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
user32.DisplayConfigGetDeviceInfo.restype = ctypes.c_long


_display_cache: tuple[float, dict[str, dict]] | None = None


def display_info() -> dict[str, dict]:
    """{\\\.\\DISPLAY1: {hdr, white}} по данным DisplayConfig.

    white — уровень SDR-белого в нитах. На HDR-мониторе Windows пишет его
    в SDRWhiteLevel в тысячных долях от 80 нит: 3500 -> 280 нит.

    Кэш на 2 секунды: HDR можно включить/выключить на ходу, но дёргать
    DisplayConfig на каждый пиксель тоже незачем.
    """
    global _display_cache
    now = time.monotonic()
    if _display_cache is not None and now - _display_cache[0] < 2.0:
        return _display_cache[1]

    result: dict[str, dict] = {}
    try:
        paths_n = ctypes.c_uint32(0)
        modes_n = ctypes.c_uint32(0)
        if user32.GetDisplayConfigBufferSizes(
                QDC_ONLY_ACTIVE_PATHS, ctypes.byref(paths_n), ctypes.byref(modes_n)) != 0:
            return result
        paths = (DISPLAYCONFIG_PATH_INFO * max(1, paths_n.value))()
        modes = (DISPLAYCONFIG_MODE_INFO * max(1, modes_n.value))()
        if user32.QueryDisplayConfig(
                QDC_ONLY_ACTIVE_PATHS, ctypes.byref(paths_n), paths,
                ctypes.byref(modes_n), modes, None) != 0:
            return result

        for i in range(paths_n.value):
            path = paths[i]
            source = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
            source.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
            source.header.size = ctypes.sizeof(source)
            source.header.adapterId = path.sourceInfo.adapterId
            source.header.id = path.sourceInfo.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(source)) != 0:
                continue
            device = source.viewGdiDeviceName
            if not device:
                continue

            advanced = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
            advanced.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
            advanced.header.size = ctypes.sizeof(advanced)
            advanced.header.adapterId = path.targetInfo.adapterId
            advanced.header.id = path.targetInfo.id
            hdr = False
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(advanced)) == 0:
                # бит 1 = advancedColorEnabled (HDR включён в параметрах экрана)
                hdr = bool(advanced.value & 0x2)

            white = 80.0
            white_info = DISPLAYCONFIG_SDR_WHITE_LEVEL()
            white_info.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL
            white_info.header.size = ctypes.sizeof(white_info)
            white_info.header.adapterId = path.targetInfo.adapterId
            white_info.header.id = path.targetInfo.id
            if (user32.DisplayConfigGetDeviceInfo(ctypes.byref(white_info)) == 0
                    and white_info.SDRWhiteLevel > 0):
                white = white_info.SDRWhiteLevel / 1000.0 * 80.0
            white = max(50.0, min(2000.0, white))

            result[device] = {"hdr": hdr, "white": white}
    except Exception:
        pass
    _display_cache = (now, result)
    return result


def clear_display_cache() -> None:
    """Сбросить кэш DisplayConfig (например, после смены режима HDR)."""
    global _display_cache
    _display_cache = None


# --------------------------------------------------------------------------
# Цвет: линейный scRGB -> sRGB
# --------------------------------------------------------------------------

def _linear_to_srgb(value: float) -> float:
    if value <= 0.0 or value != value:
        return 0.0
    if value >= 1.0:
        return 1.0
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * (value ** (1.0 / 2.4)) - 0.055


def _srgb_to_linear(value: float) -> float:
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


@lru_cache(maxsize=1)
def _half_to_float() -> tuple[float, ...]:
    """Все 65536 значений half float -> float. Считается один раз."""
    values = []
    unpack = struct.unpack
    pack = struct.pack
    for bits in range(65536):
        try:
            values.append(unpack("<e", pack("<H", bits))[0])
        except Exception:
            values.append(0.0)
    return tuple(values)


@lru_cache(maxsize=8)
def _half_lut(white_nits: float) -> list[int]:
    """LUT: 16-битный half -> 8-битный sRGB с учётом SDR-белого.

    В scRGB 1.0 = 80 нит. Чтобы SDR-белый (например, 280 нит) стал белым,
    линейные значения умножаются на 80/280. Всё, что выше, обрезается.
    """
    scale = 80.0 / max(80.0, float(white_nits))
    values = _half_to_float()
    lut = [0] * 65536
    for bits, value in enumerate(values):
        if value <= 0.0 or value != value:
            continue
        x = value * scale
        if x >= 1.0:
            lut[bits] = 255
        else:
            lut[bits] = int(_linear_to_srgb(x) * 255.0 + 0.5)
    return lut


@lru_cache(maxsize=8)
def _bgra_lut(white_nits: float) -> list[int]:
    """LUT для 8-битного BGRA-кадра HDR-монитора (запасной путь драйвера)."""
    scale = 80.0 / max(80.0, float(white_nits))
    lut = []
    for i in range(256):
        x = _srgb_to_linear(i / 255.0) * scale
        lut.append(int(_linear_to_srgb(x) * 255.0 + 0.5))
    return lut


def _convert_half_frame(data: bytes, width: int, height: int,
                        row_pitch: int, white_nits: float):
    """R16G16B16A16_FLOAT (scRGB) -> PIL RGB без засветов."""
    from PIL import Image

    row_bytes = width * 8
    if row_pitch != row_bytes:
        # У staging-текстуры RowPitch может быть больше ширины: убираем хвосты.
        parts = [data[y * row_pitch:y * row_pitch + row_bytes] for y in range(height)]
        data = b"".join(parts)
    # Пиксель = R,G,B,A (по 2 байта). memoryview режет каналы на скорости C.
    half = memoryview(data).cast("H")
    lut = _half_lut(float(white_nits))
    channels = []
    for offset in (0, 1, 2):
        plane = bytes(half[offset::4])
        img = Image.frombuffer("I;16", (width, height), plane, "raw", "I;16", 0, 1)
        # I;16 -> I -> L: point с таблицей 65536 работает только для mode "I".
        channels.append(img.convert("I").point(lut, mode="L"))
    return Image.merge("RGB", channels)


def _convert_bgra_frame(data: bytes, width: int, height: int,
                        row_pitch: int, white_nits: float, hdr: bool):
    """8-битный BGRA-кадр -> PIL RGB. Для HDR — с поправкой на SDR-белый."""
    from PIL import Image

    img = Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA", row_pitch, 1)
    img = img.convert("RGB")
    if hdr:
        img = img.point(_bgra_lut(float(white_nits)))
    return img


# --------------------------------------------------------------------------
# DXGI Desktop Duplication
# --------------------------------------------------------------------------

class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [("DeviceName", wt.WCHAR * 32),
                ("DesktopCoordinates", RECT),
                ("AttachedToDesktop", wt.BOOL),
                ("Rotation", ctypes.c_uint32),
                ("Monitor", wt.HMONITOR)]


class DXGI_OUTPUT_DESC1(ctypes.Structure):
    _fields_ = [("DeviceName", wt.WCHAR * 32),
                ("DesktopCoordinates", RECT),
                ("AttachedToDesktop", wt.BOOL),
                ("Rotation", ctypes.c_uint32),
                ("Monitor", wt.HMONITOR),
                ("BitsPerColor", ctypes.c_uint32),
                ("ColorSpace", ctypes.c_uint32),
                ("RedPrimary", ctypes.c_float * 2),
                ("GreenPrimary", ctypes.c_float * 2),
                ("BluePrimary", ctypes.c_float * 2),
                ("WhitePoint", ctypes.c_float * 2),
                ("MinLuminance", ctypes.c_float),
                ("MaxLuminance", ctypes.c_float),
                ("MaxFullFrameLuminance", ctypes.c_float)]


class DXGI_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class DXGI_MODE_DESC(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_uint32), ("Height", ctypes.c_uint32),
                ("RefreshRate", DXGI_RATIONAL), ("Format", ctypes.c_uint32),
                ("ScanlineOrdering", ctypes.c_uint32), ("Scaling", ctypes.c_uint32)]


class DXGI_OUTDUPL_DESC(ctypes.Structure):
    _fields_ = [("ModeDesc", DXGI_MODE_DESC), ("Rotation", ctypes.c_uint32),
                ("DesktopImageInSystemMemory", wt.BOOL)]


class DXGI_OUTDUPL_POINTER_POSITION(ctypes.Structure):
    _fields_ = [("Position", wt.POINT), ("Visible", wt.BOOL)]


class DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [("LastPresentTime", ctypes.c_longlong),
                ("LastMouseUpdateTime", ctypes.c_longlong),
                ("AccumulatedFrames", ctypes.c_uint32),
                ("RectsCoalesced", wt.BOOL),
                ("ProtectedContentMaskedOut", wt.BOOL),
                ("PointerPosition", DXGI_OUTDUPL_POINTER_POSITION),
                ("TotalMetadataBufferSize", ctypes.c_uint32),
                ("PointerShapeBufferSize", ctypes.c_uint32)]


class D3D11_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Quality", ctypes.c_uint32)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_uint32), ("Height", ctypes.c_uint32),
                ("MipLevels", ctypes.c_uint32), ("ArraySize", ctypes.c_uint32),
                ("Format", ctypes.c_uint32), ("SampleDesc", D3D11_SAMPLE_DESC),
                ("Usage", ctypes.c_uint32), ("BindFlags", ctypes.c_uint32),
                ("CPUAccessFlags", ctypes.c_uint32), ("MiscFlags", ctypes.c_uint32)]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", ctypes.c_uint32),
                ("DepthPitch", ctypes.c_uint32)]


dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
dxgi.CreateDXGIFactory1.restype = HRESULT

d3d11.D3D11CreateDevice.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_void_p)]
d3d11.D3D11CreateDevice.restype = HRESULT


def _dxgi_factory():
    factory = ctypes.c_void_p()
    iid = _guid(IID_IDXGIFactory1)
    hr = dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory))
    if hr < 0 or not factory.value:
        raise OSError(f"CreateDXGIFactory1: 0x{hr & 0xFFFFFFFF:08X}")
    return factory


def _find_output(factory, device_name: str | None, rect: tuple[int, int, int, int] | None):
    """Найти адаптер и DXGI-выход по имени GDI (\\\\.\\DISPLAY1) или координатам."""
    adapter_index = 0
    while True:
        adapter = ctypes.c_void_p()
        hr = _call(factory, 12, HRESULT,
                   (ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)),
                   adapter_index, ctypes.byref(adapter))
        if hr < 0 or not adapter.value:
            break
        output_index = 0
        while True:
            output = ctypes.c_void_p()
            hr = _call(adapter, 7, HRESULT,
                       (ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)),
                       output_index, ctypes.byref(output))
            if hr < 0 or not output.value:
                break
            desc = DXGI_OUTPUT_DESC()
            ok = _call(output, 7, HRESULT,
                       (ctypes.POINTER(DXGI_OUTPUT_DESC),), ctypes.byref(desc)) >= 0
            matched = False
            if ok:
                if device_name and desc.DeviceName.strip().lower() == device_name.strip().lower():
                    matched = True
                elif rect:
                    r = desc.DesktopCoordinates
                    matched = (r.left == rect[0] and r.top == rect[1]
                               and r.right - r.left == rect[2]
                               and r.bottom - r.top == rect[3])
            if matched:
                return adapter, output
            _release(output)
            output_index += 1
        _release(adapter)
        adapter_index += 1
    return None, None


def _create_device(adapter):
    levels = (ctypes.c_uint32 * 2)(D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0)
    device = ctypes.c_void_p()
    context = ctypes.c_void_p()
    level = ctypes.c_uint32(0)
    hr = d3d11.D3D11CreateDevice(
        adapter, D3D11_DRIVER_TYPE_UNKNOWN, None,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels, 2, D3D11_SDK_VERSION,
        ctypes.byref(device), ctypes.byref(level), ctypes.byref(context))
    if hr < 0 or not device.value:
        raise OSError(f"D3D11CreateDevice: 0x{hr & 0xFFFFFFFF:08X}")
    return device, context


# D3D11CreateDevice занимает ~130 мс — это половина времени снимка. Держим
# устройство между снимками и пересоздаём, только если драйвер его потерял.
_device_cache: dict[str, tuple] = {}


def _device_alive(device) -> bool:
    """GetDeviceRemovedReason (ID3D11Device, индекс 39) == S_OK."""
    try:
        hr = _call(device, 39, HRESULT, ())
        return hr == 0
    except Exception:
        return False


def _get_device(adapter, key: str):
    cached = _device_cache.get(key)
    if cached is not None:
        device, context = cached
        if _device_alive(device):
            return device, context
        drop_device(key)
    device, context = _create_device(adapter)
    _device_cache[key] = (device, context)
    return device, context


def drop_device(key: str | None = None) -> None:
    """Забыть кэшированное устройство (все или одно)."""
    keys = list(_device_cache) if key is None else [key]
    for k in keys:
        cached = _device_cache.pop(k, None)
        if cached is None:
            continue
        device, context = cached
        _release(context)
        _release(device)


def _is_blank(data: bytes, width: int, height: int, bpp: int, row_pitch: int) -> bool:
    """Быстрая проверка «пустого» первого кадра DXGI (как в HDR Snip)."""
    if not data or width <= 1 or height <= 1:
        return True
    step_x = max(1, width // 48)
    step_y = max(1, height // 48)
    limit = len(data)
    nonzero = 0
    for y in range(0, height, step_y):
        base = y * row_pitch
        for x in range(0, width, step_x):
            off = base + x * bpp
            if off + bpp <= limit and any(data[off:off + bpp]):
                nonzero += 1
                if nonzero >= 2:
                    return False
    return True


def _nudge_cursor() -> None:
    """Сдвинуть курсор на 1 px и вернуть назад.

    Некоторые драйверы не отдают первый кадр Desktop Duplication, пока не
    изменится содержимое экрана. HDR Snip делает то же самое.
    """
    if os.environ.get("ST_HDR_NUDGE", "1") == "0":
        return
    try:
        pt = wt.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return
        user32.SetCursorPos(pt.x + 1, pt.y)
        time.sleep(0.03)
        user32.SetCursorPos(pt.x, pt.y)
    except Exception:
        pass


def _duplicate_output(output, device, hdr: bool):
    """Создать IDXGIOutputDuplication, по возможности в float-формате."""
    formats = (ctypes.c_uint32 * 2)(
        DXGI_FORMAT_R16G16B16A16_FLOAT,
        DXGI_FORMAT_B8G8R8A8_UNORM,
    )
    if hdr:
        output5 = _qi(output, IID_IDXGIOutput5)
        if output5 is not None:
            dup = ctypes.c_void_p()
            hr = _call(output5, 26, HRESULT,
                       (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)),
                       device, 0, 2, formats, ctypes.byref(dup))
            _release(output5)
            if hr >= 0 and dup.value:
                return dup
    output1 = _qi(output, IID_IDXGIOutput1)
    if output1 is None:
        raise OSError("IDXGIOutput1 недоступен")
    dup = ctypes.c_void_p()
    hr = _call(output1, 22, HRESULT,
               (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
               device, ctypes.byref(dup))
    _release(output1)
    if hr < 0 or not dup.value:
        raise OSError(f"DuplicateOutput: 0x{hr & 0xFFFFFFFF:08X}")
    return dup


def _capture_once(duplication, device, context, hdr: bool,
                  white_nits: float, timeout_ms: int):
    """AcquireNextFrame -> staging -> PIL. Возвращает (image, blank)."""
    frame_info = DXGI_OUTDUPL_FRAME_INFO()
    resource = ctypes.c_void_p()
    hr = _call(duplication, 8, HRESULT,
               (ctypes.c_uint32, ctypes.POINTER(DXGI_OUTDUPL_FRAME_INFO),
                ctypes.POINTER(ctypes.c_void_p)),
               timeout_ms, ctypes.byref(frame_info), ctypes.byref(resource))
    _trace("acquire hr", hex(hr & 0xFFFFFFFF))
    if hr == DXGI_ERROR_WAIT_TIMEOUT:
        return None, True
    if hr < 0 or not resource.value:
        raise OSError(f"AcquireNextFrame: 0x{hr & 0xFFFFFFFF:08X}")

    texture = None
    staging = None
    try:
        texture = _qi(resource, IID_ID3D11Texture2D)
        _trace("texture", bool(texture))
        if texture is None:
            raise OSError("IDXGIResource -> ID3D11Texture2D не удался")
        desc = D3D11_TEXTURE2D_DESC()
        _call(texture, 10, None,
              (ctypes.POINTER(D3D11_TEXTURE2D_DESC),), ctypes.byref(desc))
        _trace("texture desc", desc.Width, desc.Height, "format", desc.Format)
        if desc.Width == 0 or desc.Height == 0:
            raise OSError("DXGI вернул пустой кадр")

        # staging-текстура того же формата и размера
        staging_desc = D3D11_TEXTURE2D_DESC()
        staging_desc.Width = desc.Width
        staging_desc.Height = desc.Height
        staging_desc.MipLevels = 1
        staging_desc.ArraySize = 1
        staging_desc.Format = desc.Format
        staging_desc.SampleDesc.Count = 1
        staging_desc.SampleDesc.Quality = 0
        staging_desc.Usage = D3D11_USAGE_STAGING
        staging_desc.BindFlags = 0
        staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ
        staging_desc.MiscFlags = 0
        staging = ctypes.c_void_p()
        hr = _call(device, 5, HRESULT,
                   (ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p)),
                   ctypes.byref(staging_desc), None, ctypes.byref(staging))
        if hr < 0 or not staging.value:
            raise OSError(f"CreateTexture2D(staging): 0x{hr & 0xFFFFFFFF:08X}")

        _trace("staging created")
        _call(context, 47, None,
              (ctypes.c_void_p, ctypes.c_void_p), staging, texture)
        _trace("copied")
        mapped = D3D11_MAPPED_SUBRESOURCE()
        hr = _call(context, 14, HRESULT,
                   (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                    ctypes.c_uint32, ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)),
                   staging, 0, D3D11_MAP_READ, 0, ctypes.byref(mapped))
        _trace("map hr", hex(hr & 0xFFFFFFFF), "pitch", mapped.RowPitch)
        if hr < 0 or not mapped.pData:
            raise OSError(f"Map(staging): 0x{hr & 0xFFFFFFFF:08X}")
        try:
            w = int(desc.Width)
            h = int(desc.Height)
            pitch = int(mapped.RowPitch)
            bpp = 8 if desc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT else 4
            data = ctypes.string_at(mapped.pData, pitch * h)
            _trace("read", len(data), "bytes")
            blank = _is_blank(data, w, h, bpp, pitch)
            _trace("blank", blank)
            if desc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT:
                img = _convert_half_frame(data, w, h, pitch, white_nits)
                _trace("converted")
            elif desc.Format in (DXGI_FORMAT_B8G8R8A8_UNORM,
                                 DXGI_FORMAT_B8G8R8A8_UNORM_SRGB):
                img = _convert_bgra_frame(data, w, h, pitch, white_nits, hdr)
            elif desc.Format == DXGI_FORMAT_R10G10B10A2_UNORM:
                # 10-бит: редкий запасной формат. Не поддерживаем — пусть GDI.
                raise OSError("DXGI вернул R10G10B10A2_UNORM, нужен float/BGRA")
            else:
                raise OSError(f"неподдерживаемый формат DXGI: {desc.Format}")
            return img, blank
        finally:
            _call(context, 15, None,
                  (ctypes.c_void_p, ctypes.c_uint32), staging, 0)
    finally:
        _release(staging)
        _release(texture)
        _release(resource)
        _call(duplication, 14, HRESULT, ())


def capture_monitor(device_name: str | None = None,
                    rect: tuple[int, int, int, int] | None = None,
                    info: dict | None = None):
    """Снять HDR-монитор через DXGI. Возвращает PIL RGB или None.

    device_name — '\\\\.\\DISPLAY1', rect — (x, y, w, h) в физических пикселях.
    None означает «это не HDR / DXGI недоступен, используйте GDI».
    """
    if os.environ.get("ST_HDR", "1") == "0":
        return None
    if info is None:
        info = display_info().get(device_name or "", {})
    if not info.get("hdr"):
        return None
    white = float(info.get("white", 80.0))
    key = device_name or f"rect:{rect}"

    # Одна повторная попытка: драйвер мог потерять кэшированное устройство
    # (смена режима, обновление драйвера, перезапуск GPU).
    for attempt in (0, 1):
        try:
            return _capture_monitor_once(device_name, rect, white, key,
                                         bool(info.get("hdr")))
        except Exception:
            drop_device(key)
            if attempt == 1:
                if os.environ.get("ST_HDR_DEBUG"):
                    import traceback
                    traceback.print_exc()
                return None
    return None


def _capture_monitor_once(device_name, rect, white: float, key: str,
                          fallback_hdr: bool):
    factory = None
    adapter = None
    output = None
    output6 = None
    duplication = None
    global _trace_t0
    _trace_t0 = time.monotonic()
    try:
        factory = _dxgi_factory()
        _trace("factory ok")
        adapter, output = _find_output(factory, device_name, rect)
        if adapter is None or output is None:
            _trace("output not found")
            return None
        _trace("output found")

        color_space = 0
        output6 = _qi(output, IID_IDXGIOutput6)
        _trace("output6", bool(output6))
        if output6 is not None:
            desc1 = DXGI_OUTPUT_DESC1()
            if _call(output6, 27, HRESULT,
                     (ctypes.POINTER(DXGI_OUTPUT_DESC1),), ctypes.byref(desc1)) >= 0:
                color_space = desc1.ColorSpace
                _trace("color_space", color_space, "rotation", desc1.Rotation,
                       "bits", desc1.BitsPerColor)
                # DXGI_MODE_ROTATION: 0 = unspecified, 1 = identity.
                # Всё остальное (2/3/4) — реальный поворот, его пока не умеем.
                if desc1.Rotation not in (0, 1):
                    return None

        # G10 = линейный (scRGB), G2084 = PQ, GHLG = HLG — всё это HDR-вывод.
        # Если GetDesc1 недоступен (color_space == 0), верим DisplayConfig.
        hdr = color_space in (1, 12, 13, 14, 18, 19, 25) if color_space else fallback_hdr
        device, context = _get_device(adapter, key)
        _trace("device ok", hex(device.value or 0), hex(context.value or 0))
        duplication = _duplicate_output(output, device, hdr)
        _trace("duplication ok")

        desc = DXGI_OUTDUPL_DESC()
        _call(duplication, 7, None, (ctypes.POINTER(DXGI_OUTDUPL_DESC),), ctypes.byref(desc))
        w, h = int(desc.ModeDesc.Width), int(desc.ModeDesc.Height)
        _trace("mode", w, "x", h, "format", desc.ModeDesc.Format)
        if w <= 0 or h <= 0:
            return None

        nudged = False
        for attempt in range(3):
            _trace("attempt", attempt)
            image, blank = _capture_once(
                duplication, device, context, hdr, white,
                800 if attempt == 0 else 500)
            _trace("attempt", attempt, "->", None if image is None else image.size,
                   "blank", blank)
            if image is not None and not blank:
                return image
            # Первый кадр у части драйверов пустой, а на статичном рабочем
            # столе нового кадра вообще нет. Сдвигаем курсор на 1 px и обратно
            # (как HDR Snip): DWM считает это изменением и отдаёт кадр.
            if not nudged:
                _nudge_cursor()
                nudged = True
            time.sleep(0.05)
        # Все кадры пустые — пусть остаётся GDI-снимок, он не хуже чёрного.
        _trace("all frames blank")
        return None
    finally:
        _release(duplication)
        _release(output6)
        _release(output)
        _release(adapter)
        _release(factory)


def patch_virtual(image, origin: tuple[int, int]) -> None:
    """Заменить в общем GDI-снимке области HDR-мониторов на DXGI-кадры.

    Координаты не меняются: изображение остаётся снимком всего виртуального
    экрана, поэтому вся логика оверлея/выбора региона продолжает работать.
    """
    import st_capture

    infos = display_info()
    for device, (mx, my, mw, mh) in st_capture.list_monitors_named():
        meta = infos.get(device)
        if not meta or not meta.get("hdr"):
            continue
        mon = capture_monitor(device, (mx, my, mw, mh), meta)
        if mon is None:
            continue
        try:
            if mon.size != (mw, mh):
                mon = mon.resize((mw, mh))
            image.paste(mon, (mx - origin[0], my - origin[1]))
        except Exception:
            continue


if __name__ == "__main__":
    # Диагностика без GUI: python st_hdr.py
    import st_capture

    print("DisplayConfig:")
    for name, meta in display_info().items():
        print(f"  {name}: HDR={meta['hdr']} SDR-white={meta['white']:.0f} нит")
    print("Мониторы:")
    for device, rect in st_capture.list_monitors_named():
        print(f"  {device}: {rect}")
    print("Пробую DXGI-захват HDR-монитора...")
    for device, rect in st_capture.list_monitors_named():
        meta = display_info().get(device, {})
        if not meta.get("hdr"):
            continue
        t0 = time.monotonic()
        img = capture_monitor(device, rect, meta)
        if img is None:
            print(f"  {device}: DXGI не дал кадр (останется GDI)")
        else:
            print(f"  {device}: {img.size}, {int((time.monotonic()-t0)*1000)} мс")
