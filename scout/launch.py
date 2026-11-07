"""Open the local workspace once the server is ready."""

import threading
import time
import urllib.request
import webbrowser

import uvicorn

URL = "http://127.0.0.1:8765/"


def open_browser():
    for _ in range(100):
        try:
            with urllib.request.urlopen(URL + "api/state", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(URL)
                    return
        except OSError:
            time.sleep(0.15)


if __name__ == "__main__":
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("scout.app:app", host="127.0.0.1", port=8765)
