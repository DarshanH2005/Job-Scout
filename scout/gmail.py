"""Opt-in Gmail sending and reply checks. Credentials stay on this machine."""

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses, parseaddr
from urllib.parse import urlencode

import httpx

from .store import now
from .credentials import google_client

SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN = "https://oauth2.googleapis.com/token"
REDIRECT = "http://127.0.0.1:8765/oauth/google"
KEYRING_SERVICE = "Job Scout Gmail"
HEADERS = ["From", "To", "Cc", "Bcc", "Subject", "Auto-Submitted", "Precedence", "Content-Type", "In-Reply-To",
           "Message-ID", "References"]


def headers(message):
    return {item["name"].lower(): item["value"] for item in message.get("payload", {}).get("headers", [])}


def incoming_reply(message, anchor, peer):
    labels = message.get("labelIds", [])
    if not isinstance(labels, list) or not isinstance(anchor, int):
        return False
    try:
        received = int(message.get("internalDate", 0))
    except (TypeError, ValueError):
        return False
    if "SENT" in labels or received <= anchor:
        return False
    data = headers(message)
    address = parseaddr(data.get("from", ""))[1].lower()
    subject = data.get("subject", "")
    return (bool(address) and address == peer
            and not re.search(r"\b(?:automatic reply|out of office|delivery status notification|undeliverable)\b", subject, re.I)
            and data.get("auto-submitted", "no").lower() == "no"
            and data.get("precedence", "").lower() not in {"bulk", "list", "junk"}
            and "delivery-status" not in data.get("content-type", "").lower())


def thread_review_reason(messages, anchor, account, peer):
    """Stop a follow-up when the thread contains a reply that needs human review."""
    if not isinstance(messages, list) or not messages:
        return "Gmail did not return a complete thread. Review it before following up."
    if not isinstance(anchor, int):
        return "Gmail did not return the original send date. Review the thread before following up."
    for message in messages:
        if not isinstance(message, dict):
            return "Gmail did not return a complete thread. Review it before following up."
        labels = message.get("labelIds", [])
        if not isinstance(labels, list):
            return "Gmail did not return complete message labels. Review the thread before following up."
        if "SENT" in labels:
            continue
        try:
            received = int(message["internalDate"])
        except (KeyError, TypeError, ValueError):
            return "Gmail did not return a message date. Review the thread before following up."
        if received <= anchor:
            continue
        address = parseaddr(headers(message).get("from", ""))[1].lower()
        if address == account.lower():
            continue
        if not address:
            return "A message has no recognizable sender. Review the thread before following up."
        if address == peer:
            if not incoming_reply(message, anchor, peer):
                return "The contact sent an automatic or nonstandard reply. Review the thread before following up."
        else:
            return "Another sender replied in the thread. Review it before following up."
    return ""


class Gmail:
    def __init__(self, store):
        self.store = store
        self.pending = None
        self.lock = asyncio.Lock()

    def auth_url(self, send=False):
        config = google_client()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        scopes = (SCOPE, SEND_SCOPE) if send else (SCOPE,)
        self.pending = (state, verifier, time.monotonic() + 600, scopes)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            dict(client_id=config["client_id"], response_type="code", scope=" ".join(scopes), redirect_uri=REDIRECT,
                 state=state, code_challenge=challenge, code_challenge_method="S256", access_type="offline", prompt="consent"))

    @staticmethod
    def _vault():
        import keyring
        return keyring

    def _save(self, account, token):
        self._vault().set_password(KEYRING_SERVICE, account, json.dumps(token))

    async def _exchange(self, data):
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(TOKEN, data={**google_client(), **data})
            if (data["grant_type"] == "refresh_token" and response.status_code == 400
                    and response.headers.get("content-type", "").startswith("application/json")
                    and response.json().get("error") == "invalid_grant"):
                raise ValueError("Gmail authorization expired or was revoked. Select Reconnect Gmail in Settings.")
            response.raise_for_status()
        result = response.json()
        result["expires_at"] = time.time() + result.get("expires_in", 3600) - 60
        return result

    async def connect(self, state, code):
        pending, self.pending = self.pending, None
        if not pending or not secrets.compare_digest(state, pending[0]) or time.monotonic() > pending[2]:
            raise ValueError("The Google authorization request expired. Start again from Settings.")
        token = await self._exchange(dict(code=code, grant_type="authorization_code",
                                          redirect_uri=REDIRECT, code_verifier=pending[1]))
        token.setdefault("scope", " ".join(pending[3]))
        if not token.get("refresh_token") or SCOPE not in token["scope"].split():
            raise ValueError("Google did not grant persistent Gmail metadata access.")
        profile = await self._get("/profile", token["access_token"])
        account = profile["emailAddress"]
        self._save(account, token)
        self.store.set_setting("gmail_account", account)
        self.store.set_setting("gmail_send_enabled", "1" if SEND_SCOPE in token["scope"].split() else "")
        self.store.set_setting("gmail_issue", "")

    async def token(self, send=False):
        account = self.store.setting("gmail_account")
        if not account:
            raise ValueError("Connect Gmail in Settings first.")
        value = self._vault().get_password(KEYRING_SERVICE, account)
        if not value:
            raise ValueError("Reconnect Gmail in Settings.")
        token = json.loads(value)
        if token["expires_at"] <= time.time():
            refreshed = await self._exchange(dict(refresh_token=token["refresh_token"], grant_type="refresh_token"))
            token.update(refreshed)
            self._save(account, token)
        if send and SEND_SCOPE not in token.get("scope", "").split():
            raise ValueError("Enable Gmail sending in Job Scout Settings first.")
        return account, token["access_token"]

    def disconnect(self):
        account = self.store.setting("gmail_account")
        if account:
            vault = self._vault()
            if vault.get_password(KEYRING_SERVICE, account):
                vault.delete_password(KEYRING_SERVICE, account)
            self.store.set_setting("gmail_account", "")
            self.store.set_setting("gmail_send_enabled", "")

    @staticmethod
    def message(account, recipient, subject, text):
        message = EmailMessage(policy=SMTP)
        try:
            message["From"] = Address(addr_spec=account)
            message["To"] = Address(addr_spec=recipient)
        except ValueError as exc:
            raise ValueError("Save a valid Gmail recipient before sending.") from exc
        message["Subject"] = " ".join(subject.split())
        message.set_content(text)
        return message

    @staticmethod
    async def send_message(token, message, thread_id=None):
        data = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
        if thread_id:
            data["threadId"] = thread_id
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(API + "/messages/send", json=data,
                                         headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
        return response.json()

    @staticmethod
    async def _get(path, token, params=None):
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(API + path, headers={"Authorization": f"Bearer {token}"}, params=params)
            response.raise_for_status()
        return response.json()

    async def _label(self, token, name):
        data = await self._get("/labels", token)
        return next((label["id"] for label in data.get("labels", []) if label["name"] == name), None)

    async def thread(self, token, thread_id):
        return await self._get(f"/threads/{thread_id}", token,
                               [("format", "metadata"), *(("metadataHeaders", h) for h in HEADERS)])

    def record_reply(self, item):
        with self.store.connect() as conn:
            stamp = now()
            update = conn.execute("""UPDATE outreach SET state='replied',due_at=NULL,checked_at=?
                WHERE id=? AND state='waiting' AND thread_id=? AND gmail_send_pending=''""",
                (stamp, item["id"], item["thread_id"]))
            if update.rowcount:
                conn.execute("DELETE FROM gmail_follow_up_schedule WHERE outreach_id=?", (item["id"],))
                conn.execute("INSERT INTO events(outreach_id,kind,at,note) VALUES(?,?,?,?)",
                             (item["id"], "reply_detected", stamp, "Gmail reply from the linked recipient."))
            return update.rowcount

    async def list_threads(self):
        _, token = await self.token()
        label = await self._label(token, "Job Scout")
        if not label:
            return []
        data = await self._get("/threads", token, {"labelIds": label, "maxResults": 15})
        semaphore = asyncio.Semaphore(4)

        async def describe(row):
            async with semaphore:
                thread = await self.thread(token, row["id"])
                sent = [message for message in thread.get("messages", []) if "SENT" in message.get("labelIds", [])]
                if not sent:
                    return None
                last = max(sent, key=lambda message: int(message.get("internalDate", 0)))
                data = headers(last)
                return {"id": row["id"], "subject": data.get("subject", "Untitled"), "to": data.get("to", "")}
        return [item for item in await asyncio.gather(*(describe(row) for row in data.get("threads", []))) if item]

    async def link(self, item_id, thread_id):
        account, token = await self.token()
        label = await self._label(token, "Job Scout")
        thread = await self.thread(token, thread_id)
        messages = thread.get("messages", [])
        if not label or not any(label in message.get("labelIds", []) for message in messages):
            raise ValueError("Apply the Job Scout label in Gmail before linking the thread.")
        outgoing = [message for message in messages if "SENT" in message.get("labelIds", [])]
        if not outgoing:
            raise ValueError("Choose a Gmail conversation with a sent message.")
        last = max(outgoing, key=lambda message: int(message.get("internalDate", 0)))
        outgoing_headers = last.get("payload", {}).get("headers", [])
        def addresses(fields):
            values = [header.get("value", "") for header in outgoing_headers
                      if header.get("name", "").lower() in fields]
            return [address.lower() for _, address in getaddresses(values) if address]

        recipients = addresses({"to"})
        if len(recipients) != 1 or addresses({"cc", "bcc"}):
            raise ValueError("Link a conversation with one recipient and no additional Cc or Bcc recipients.")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            contact = conn.execute("""SELECT c.email FROM outreach o JOIN contacts c ON c.id=o.contact_id
                                      WHERE o.id=?""", (item_id,)).fetchone()
            if not contact or not contact["email"]:
                raise ValueError("Save the contact's email before linking a Gmail thread.")
            if contact["email"].strip().lower() != recipients[0]:
                raise ValueError("The thread recipient does not match the saved contact email. Review both before linking.")
            changed = conn.execute("""UPDATE outreach SET thread_id=?,mail_account=?,mail_anchor=?,mail_peer=?
                WHERE id=? AND channel='gmail' AND state='waiting' AND gmail_send_pending='' AND gmail_sent=0 AND thread_id IS NULL""",
                (thread_id, account, int(last["internalDate"]), recipients[0], item_id))
            if not changed.rowcount:
                raise ValueError("Record the Gmail send before linking a conversation.")
            conn.execute("DELETE FROM gmail_follow_up_schedule WHERE outreach_id=? AND (thread_id<>? OR account<>? OR recipient<>?)",
                         (item_id, thread_id, account, recipients[0]))

    async def check(self):
        async with self.lock:
            account, token = await self.token()
            items = self.store.all("""SELECT id,thread_id,mail_anchor,mail_peer FROM outreach
                WHERE mail_account=? AND thread_id IS NOT NULL AND state='waiting'""", (account,))
            replies, issues = 0, 0
            for item in items:
                try:
                    thread = await self.thread(token, item["thread_id"])
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    issues += 1
                    self.store.write("UPDATE gmail_follow_up_schedule SET issue=? WHERE outreach_id=? AND issue=''",
                                     ("The Gmail thread could not be found. Review it before following up.", item["id"]))
                    continue
                messages = thread.get("messages")
                if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
                    issues += 1
                    self.store.write("UPDATE gmail_follow_up_schedule SET issue=? WHERE outreach_id=? AND issue=''",
                                     ("Gmail did not return a complete thread. Review it before following up.", item["id"]))
                    continue
                reply = next((message for message in messages or []
                              if incoming_reply(message, item["mail_anchor"], item["mail_peer"])), None)
                if reply:
                    replies += self.record_reply(item)
                else:
                    reason = thread_review_reason(messages, item["mail_anchor"], account, item["mail_peer"])
                    if reason:
                        self.store.write("UPDATE gmail_follow_up_schedule SET issue=? WHERE outreach_id=? AND issue=''",
                                         (reason, item["id"]))
            if self.store.setting("linkedin_hints") == "1":
                await self._linkedin_hints(account, token)
            self.store.set_setting("gmail_checked_at", now())
            self.store.set_setting("gmail_issue", f"{issues} linked Gmail threads could not be read." if issues else "")
            return replies

    async def _linkedin_hints(self, account, token):
        label = await self._label(token, "Job Scout LinkedIn")
        if not label:
            return
        data = await self._get("/messages", token, {"labelIds": label, "maxResults": 25})
        for row in data.get("messages", []):
            if self.store.one("SELECT 1 FROM mail_seen WHERE account=? AND id=?", (account, row["id"])):
                continue
            message = await self._get(f'/messages/{row["id"]}', token,
                                      [("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject")])
            info = headers(message)
            sender = parseaddr(info.get("from", ""))[1].lower().rpartition("@")[2]
            subject = info.get("subject", "")[:180]
            if (sender == "linkedin.com" or sender.endswith(".linkedin.com")) and re.search(r"\b(message|replied|inmail)\b", subject, re.I):
                self.store.write("INSERT OR IGNORE INTO mail_seen(account,id,kind,at,subject) VALUES(?,?,?,?,?)",
                                 (account, row["id"], "linkedin_hint", now(), subject))
