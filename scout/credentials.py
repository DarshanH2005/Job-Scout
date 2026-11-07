"""Local integration settings in the system credential vault."""

import json
import os
from pathlib import Path

SERVICE = "Job Scout integrations"


def get(name):
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return ""
    try:
        return keyring.get_password(SERVICE, name) or ""
    except KeyringError:
        return ""


def put(name, value):
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError as exc:
        raise ValueError("The local credential vault is unavailable.") from exc
    try:
        if value:
            keyring.set_password(SERVICE, name, value)
        elif keyring.get_password(SERVICE, name):
            keyring.delete_password(SERVICE, name)
    except KeyringError as exc:
        raise ValueError("Job Scout could not save settings in the local credential vault.") from exc


def brave_key():
    return os.getenv("SCOUT_BRAVE_KEY") or get("brave")


def google_configured():
    return bool(os.getenv("SCOUT_GOOGLE_CLIENT_FILE") or get("google"))


def parse_google(raw):
    try:
        installed = json.loads(raw)["installed"]
        client_id, secret = installed["client_id"], installed["client_secret"]
        if not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com"):
            raise ValueError
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError
        return {"client_id": client_id, "client_secret": secret}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Choose the JSON file for a Google Desktop OAuth client.") from exc


def google_client():
    filename = os.getenv("SCOUT_GOOGLE_CLIENT_FILE")
    if filename:
        try:
            return parse_google(Path(filename).read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise ValueError("Job Scout could not read the Google Desktop OAuth client file.") from exc
    saved = get("google")
    if saved:
        return parse_google(saved)
    raise ValueError("Add a Google Desktop OAuth client file to enable Gmail.")
