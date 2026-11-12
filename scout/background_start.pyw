"""Start the local server quietly at Windows sign-in without opening a browser."""

import os
import shutil
import socket
import subprocess
from pathlib import Path


def main():
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1):
            return  # Another process already owns the port; never start a duplicate.
    except OSError:
        pass
    root = Path(__file__).resolve().parent.parent
    data = Path(os.environ.get("LOCALAPPDATA", root)) / "JobScout"
    data.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv.exe") or str(Path.home() / ".local" / "bin" / "uv.exe")
    venv_python = data / "venv" / "Scripts" / "pythonw.exe"
    if Path(uv).is_file():
        command = [uv, "run", "--offline", "--no-project", "--with-requirements", str(root / "requirements.txt"),
                   "python", "-m", "uvicorn", "scout.app:app", "--host", "127.0.0.1", "--port", "8765"]
    elif venv_python.is_file():
        command = [str(venv_python), "-m", "uvicorn", "scout.app:app", "--host", "127.0.0.1", "--port", "8765"]
    else:
        with (data / "startup.log").open("a", encoding="utf-8") as log:
            log.write("Job Scout could not find its runtime. Open Launch Job Scout.cmd to set it up.\n")
        return
    with (data / "startup.log").open("ab") as log:
        subprocess.Popen(command, cwd=str(root), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    main()
