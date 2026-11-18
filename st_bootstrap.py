"""Проверка зависимостей ScreenToTranslate и их установка через pip.

Программа распространяется как исходники, а PySide6/Pillow/winsdk — тяжёлые
бинарные пакеты, которые незачем хранить в git. Если их нет, показываем
понятный вопрос и по согласию ставим в профиль пользователя (pip --user),
без прав администратора.

Никаких собственных загрузчиков: только штатный pip и PyPI по HTTPS.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

# Версии, с которыми программа проверена. Менять вместе с requirements.txt.
REQUIREMENTS = (
    "PySide6-Essentials==6.12.0",
    "Pillow==12.3.0",
    "winsdk==1.0.0b10",
)
MODULES = ("PySide6", "PIL", "winsdk")

MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 12)  # для 3.13+ у winsdk пока нет колёс

_SKIP_ENV = "ST_SKIP_BOOTSTRAP"
_LOG_NAME = "bootstrap.log"


def missing_modules() -> list[str]:
    """Какие из MODULES не видны текущему Python (без импорта самих пакетов)."""
    return [name for name in MODULES if importlib.util.find_spec(name) is None]


def _python_exe() -> str:
    """pip лучше запускать парным python.exe: pythonw.exe не пишет в консоль."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        candidate = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.isfile(candidate):
            return candidate
    return exe


def _log_path() -> str:
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "ScreenToTranslate")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), _LOG_NAME)
    return os.path.join(base, _LOG_NAME)


def _write_log(text: str) -> None:
    try:
        with open(_log_path(), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def _message(text: str, *, yes_no: bool = False, error: bool = False) -> bool:
    """Родное окно Windows: не нужен ни консольный, ни tkinter."""
    import ctypes

    flags = 0x00040000 | 0x00010000  # MB_TOPMOST | MB_SETFOREGROUND
    if yes_no:
        flags |= 0x00000004 | 0x00000020  # MB_YESNO | MB_ICONQUESTION
    elif error:
        flags |= 0x00000010  # MB_ICONERROR
    else:
        flags |= 0x00000040  # MB_ICONINFORMATION
    result = ctypes.windll.user32.MessageBoxW(None, text, "ScreenToTranslate", flags)
    return result == 6  # IDYES


def _install() -> tuple[bool, str]:
    cmd = [
        _python_exe(), "-m", "pip", "install",
        "--user", "--no-input", "--disable-pip-version-check",
        "--no-warn-script-location", "--only-binary=:all:",
        *REQUIREMENTS,
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=flags,
        )
    except OSError as exc:
        return False, str(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    _write_log("$ " + " ".join(cmd) + "\n\n" + output)
    return proc.returncode == 0, output


def ensure_dependencies() -> None:
    """Вызывается до импорта PySide6/Pillow/winsdk."""
    missing = missing_modules()
    if not missing or os.environ.get(_SKIP_ENV) == "1":
        return

    version = ".".join(str(x) for x in sys.version_info[:3])
    if not (MIN_PYTHON <= sys.version_info[:2] <= MAX_PYTHON):
        _message(
            "Для ScreenToTranslate нужен Python 3.10–3.12 (64-бит).\n"
            f"Сейчас запущен Python {version}.\n\n"
            "Поставьте Python 3.10 с python.org и запустите setup.cmd.",
            error=True,
        )
        raise SystemExit(1)

    if importlib.util.find_spec("pip") is None:
        _message(
            "В этом Python нет pip, поэтому зависимости не установить.\n\n"
            "Запустите:\n"
            f"{_python_exe()} -m ensurepip --upgrade\n\n"
            "или поставьте Python 3.10 с python.org (там pip уже есть).",
            error=True,
        )
        raise SystemExit(1)

    names = ", ".join(missing)
    agreed = _message(
        "Для ScreenToTranslate не хватает библиотек:\n"
        f"{names}\n\n"
        "Скачать их сейчас? Около 250 МБ, интернет нужен один раз.\n"
        "Установка идёт в профиль пользователя, права администратора не нужны.\n\n"
        "«Да» — скачать и продолжить (1–2 минуты).\n"
        "«Нет» — закрыть программу; позже можно запустить setup.cmd.",
        yes_no=True,
    )
    if not agreed:
        raise SystemExit(0)

    ok, output = _install()
    if not ok:
        tail = "\n".join(output.strip().splitlines()[-8:])
        _message(
            "Не удалось установить библиотеки.\n\n"
            "Проверьте интернет и запустите setup.cmd.\n"
            "Или выполните вручную:\n"
            f"{_python_exe()} -m pip install --user "
            + " ".join(REQUIREMENTS)
            + ("\n\nПоследние строки:\n" + tail if tail else ""),
            error=True,
        )
        raise SystemExit(1)

    # pip только что создал user site; добавляем его в текущий процесс,
    # чтобы не перезапускать программу.
    import site

    if site.ENABLE_USER_SITE:
        user_site = site.getusersitepackages()
        if os.path.isdir(user_site):
            site.addsitedir(user_site)
    importlib.invalidate_caches()

    still = missing_modules()
    if still:
        _message(
            "Библиотеки установлены, но этот запуск их не видит.\n"
            "Закройте программу и запустите её снова.\n\n"
            f"Лог установки: {_log_path()}",
            error=True,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    print("missing:", missing_modules())
    print("python:", _python_exe())
    print("log:", _log_path())
