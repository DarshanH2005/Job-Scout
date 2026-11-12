import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from scout import autostart


class FakeRegistry:
    HKEY_CURRENT_USER = "user"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def OpenKey(self, *_):
        return self

    def CreateKeyEx(self, *_):
        return self

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, _key, name, _reserved, _kind, value):
        self.values[name] = value

    def DeleteValue(self, _key, name):
        del self.values[name]


def test_startup_entry_can_be_enabled_and_removed_without_touching_other_values(monkeypatch):
    registry = FakeRegistry()
    registry.values["AnotherApp"] = "leave untouched"
    monkeypatch.setattr(autostart, "winreg", registry)
    monkeypatch.setattr(autostart, "startup_command", lambda: '"pythonw.exe" "Job Scout.pyw"')
    assert autostart.status() == {"available":True, "enabled":False, "conflict":False}
    assert autostart.set_enabled(True)["enabled"]
    assert registry.values["AnotherApp"] == "leave untouched"
    assert not autostart.set_enabled(False)["enabled"]
    assert registry.values == {"AnotherApp":"leave untouched"}


def test_startup_never_overwrites_or_deletes_another_install(monkeypatch):
    registry = FakeRegistry()
    registry.values["JobScout"] = "a different installation"
    monkeypatch.setattr(autostart, "winreg", registry)
    monkeypatch.setattr(autostart, "startup_command", lambda: "this installation")
    assert autostart.status()["conflict"]
    for enabled in (True, False):
        with pytest.raises(ValueError, match="different|another|Another"):
            autostart.set_enabled(enabled)
    assert registry.values["JobScout"] == "a different installation"


def load_background_start():
    path = Path(autostart.__file__).with_name("background_start.pyw")
    loader = importlib.machinery.SourceFileLoader("background_start_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_background_start_uses_cached_dependencies_without_opening_a_browser(tmp_path, monkeypatch):
    module = load_background_start()
    uv = tmp_path / "uv.exe"
    uv.touch()
    launches = []
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(module.socket, "create_connection", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("not listening")))
    monkeypatch.setattr(module.shutil, "which", lambda _: str(uv))
    monkeypatch.setattr(module.subprocess, "Popen", lambda command, **kwargs: launches.append((command, kwargs)))
    module.main()
    assert len(launches) == 1
    command, options = launches[0]
    assert command[:4] == [str(uv), "run", "--offline", "--no-project"]
    assert "scout.app:app" in command and "127.0.0.1" in command and "8765" in command
    assert Path(options["cwd"]) == Path(autostart.__file__).resolve().parent.parent


def test_background_start_does_not_launch_when_port_is_already_in_use(monkeypatch):
    module = load_background_start()
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
    launches = []
    monkeypatch.setattr(module.socket, "create_connection", lambda *_args, **_kwargs: Connection())
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: launches.append((args, kwargs)))
    module.main()
    assert launches == []
