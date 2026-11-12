"""Optional, per-user Windows sign-in startup for the local server."""

import shutil
import subprocess
from pathlib import Path

try:
    import winreg
except ImportError:  # Other platforms can still run Job Scout manually.
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "JobScout"


def startup_command():
    launcher = shutil.which("pyw.exe") or shutil.which("pythonw.exe")
    script = Path(__file__).with_name("background_start.pyw")
    if not launcher or not script.is_file():
        return None
    arguments = [launcher]
    if Path(launcher).name.casefold() == "pyw.exe":
        arguments.append("-3")
    return subprocess.list2cmdline([*arguments, str(script)])


def current_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)[0]
    except FileNotFoundError:
        return None


def status():
    if not winreg:
        return {"available": False, "enabled": False, "conflict": False}
    command = startup_command()
    try:
        current = current_value()
    except OSError:
        return {"available": False, "enabled": False, "conflict": False}
    return {"available": bool(command), "enabled": bool(command and current == command),
            "conflict": bool(current is not None and current != command)}


def set_enabled(enabled):
    if not winreg:
        raise ValueError("Windows sign-in startup is not available on this system.")
    command = startup_command()
    if not command:
        raise ValueError("Python's Windows launcher is unavailable. Open Job Scout manually.")
    current = current_value()
    if current is not None and current != command:
        raise ValueError("Another Job Scout startup entry already exists. Review it in Windows startup settings.")
    if enabled:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    elif current is not None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    return status()
