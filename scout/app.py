"""Local web server and workflow actions."""

import asyncio
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import credentials
from .feeds import PROVIDERS, REGIONS, board_from_job_url, build_job, canonical_url, fetch_board, public_url
from .gmail import Gmail
from .research import find_people, rewrite
from .store import Store, now

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / ".env")
APP_DATA = Path(os.getenv("LOCALAPPDATA") or ROOT.parent) / "JobScout"
CHANNELS = ("linkedin", "gmail", "community")


class CompanyInput(BaseModel):
    name: str = Field(min_length=1, max_length=130)
    url: str = Field(max_length=1800)
    provider: str = ""
    board: str = Field(default="", max_length=120)
    enabled: bool = True


class JobInput(BaseModel):
    url: str = Field(max_length=1800)
    company: str = Field(default="", max_length=130)
    title: str = Field(default="", max_length=220)
    location: str = Field(default="", max_length=350)
    description: str = Field(default="", max_length=30000)


class JobFlags(BaseModel):
    saved: bool | None = None
    applied: bool | None = None
    hidden: bool | None = None


class ContactInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    title: str = Field(default="", max_length=200)
    source: str = Field(default="", max_length=1800)
    notes: str = Field(default="", max_length=500)


class DraftInput(BaseModel):
    contact: ContactInput
    channel: str
    evidence: str = Field(default="", max_length=350)


class DraftText(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ConversationLink(BaseModel):
    url: str = Field(max_length=1800)


class OutreachEvent(BaseModel):
    action: str
    note: str = Field(default="", max_length=650)
    message: str | None = Field(default=None, max_length=4000)
    expected_due_at: str | None = Field(default=None, max_length=40)


class ThreadLink(BaseModel):
    thread_id: str = Field(pattern=r"^[0-9a-zA-Z_\-]{1,100}$")


class MailOptions(BaseModel):
    linkedin_hints: bool


class IntegrationInput(BaseModel):
    value: str = Field(max_length=16000)


def fail(message, status=400):
    raise HTTPException(status, message)


def after(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="microseconds")


def required_url(value):
    return public_url(value) or fail("Enter a valid http or https URL.")


def job_or_404(store, job_id):
    return store.job(job_id) or fail("Job not found.", 404)


def mail_failure(exc):
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 401, 403):
        return "Google rejected Gmail access. Check the Gmail API setup or reconnect from Settings."
    return "Gmail could not be checked. Try again from Settings."


class Scanner:
    def __init__(self, store):
        self.store = store
        self.task = None

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def start(self):
        if self.running:
            return self.store.one("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
        companies = self.store.all("SELECT * FROM companies WHERE enabled=1 ORDER BY id")
        scan_id = self.store.write("INSERT INTO scans(started_at,state,total) VALUES(?,?,?)",
                                   (now(), "running", len(companies)))
        self.task = asyncio.create_task(self.run(scan_id, companies))
        return self.store.one("SELECT * FROM scans WHERE id=?", (scan_id,))

    async def run(self, scan_id, companies):
        limits = {provider: asyncio.Semaphore(4) for provider in PROVIDERS}
        unsupported = len([row for row in companies if row["provider"] not in PROVIDERS])
        self.store.write("UPDATE scans SET unsupported=? WHERE id=?", (unsupported, scan_id))
        state, note = "completed", ""
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                async def check(company):
                    jobs, error = await fetch_board(client, company, limits[company["provider"]])
                    self.store.finish_source(scan_id, company, jobs, error)
                await asyncio.gather(*(check(row) for row in companies if row["provider"] in PROVIDERS))
        except asyncio.CancelledError:
            state, note = "interrupted", "The app closed before the scan finished."
            raise
        except Exception:
            state, note = "failed", "The scan stopped early. Check the local server log and retry."
            import logging
            logging.exception("Job scan %s stopped", scan_id)
        finally:
            self.store.write("UPDATE scans SET state=?,note=?,finished_at=? WHERE id=?", (state, note, now(), scan_id))


def create_app(db_path=None, seed=True):
    store = Store(db_path or os.getenv("SCOUT_DB") or APP_DATA / "scout.sqlite3")
    scanner, gmail = Scanner(store), Gmail(store)

    async def check_mail():
        try:
            return await gmail.check()
        except (ValueError, httpx.HTTPError, OSError, ImportError, RuntimeError) as exc:
            message = mail_failure(exc)
            store.set_setting("gmail_issue", message)
            fail(message, 502)

    async def mail_loop():
        while True:
            await asyncio.sleep(600)
            if store.setting("gmail_account"):
                with suppress(HTTPException):
                    await check_mail()

    @asynccontextmanager
    async def lifespan(_app):
        store.initialize(seed=seed)
        worker = asyncio.create_task(mail_loop())
        try:
            yield
        finally:
            for task in (worker, scanner.task):
                if task and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="Job Scout", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method in {"POST", "PATCH", "DELETE", "PUT"}:
            origin = request.headers.get("origin")
            if (origin and origin != f"{request.url.scheme}://{request.url.netloc}") or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Open Job Scout directly on this computer."}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/state")
    async def state():
        account = store.setting("gmail_account")
        return {"stats": store.stats(), "scan": store.one("SELECT * FROM scans ORDER BY id DESC LIMIT 1"),
                "search_configured": bool(credentials.brave_key()),
                "search_from_env": bool(os.getenv("SCOUT_BRAVE_KEY")),
                "rewrite_configured": bool(os.getenv("SCOUT_OPENAI_KEY") and os.getenv("SCOUT_OPENAI_MODEL")),
                "mail": {"account": account, "configured": credentials.google_configured(),
                         "from_env": bool(os.getenv("SCOUT_GOOGLE_CLIENT_FILE")),
                         "checked_at": store.setting("gmail_checked_at"),
                         "issue": store.setting("gmail_issue"),
                         "linkedin_hints": store.setting("linkedin_hints") == "1"}}

    @app.put("/api/integrations/{name}")
    async def configure_integration(name: str, data: IntegrationInput):
        environment = {"brave": "SCOUT_BRAVE_KEY", "google": "SCOUT_GOOGLE_CLIENT_FILE"}
        if name not in environment:
            fail("Unknown integration.", 404)
        if os.getenv(environment[name]):
            fail("This integration is managed through the local environment.", 409)
        if name == "google" and store.setting("gmail_account"):
            fail("Disconnect Gmail before changing the Google client.", 409)
        value = data.value.strip()
        try:
            if name == "google" and value:
                value = json.dumps({"installed": credentials.parse_google(value)})
            elif name == "brave" and value and not re.fullmatch(r"\S{8,512}", value):
                fail("Enter a Brave Search API key.")
            credentials.put(name, value)
        except ValueError as exc:
            fail(str(exc))
        return {"configured": bool(value)}

    @app.post("/api/scans", status_code=202)
    async def scan():
        return scanner.start()

    @app.get("/api/companies")
    async def companies():
        return store.all("SELECT * FROM companies ORDER BY name COLLATE NOCASE")

    def save_company(data, company_id=None):
        if scanner.running:
            fail("The current scan needs to finish before editing the watchlist.", 409)
        provider = data.provider
        board = data.board.strip()
        if provider and (provider not in PROVIDERS or not re.fullmatch(r"[A-Za-z0-9._-]+", board)):
            fail("Select a supported provider and enter its board identifier.")
        name = data.name.strip() or fail("Enter the company name.")
        url = required_url(data.url)
        try:
            with store.connect() as conn:
                if company_id is None:
                    company_id = conn.execute(
                        "INSERT INTO companies(name,url,provider,board,enabled,status) VALUES(?,?,?,?,?,?)",
                        (name, url, provider, board if provider else "", data.enabled, "pending" if provider else "unsupported"),
                    ).lastrowid
                else:
                    current = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
                    if not current:
                        fail("Company not found.", 404)
                    changed = (current["provider"], current["board"]) != (provider, board)
                    status = ("pending" if provider else "unsupported") if changed else current["status"]
                    conn.execute("""UPDATE companies SET name=?,url=?,provider=?,board=?,enabled=?,status=?,
                        note=?, checked_at=?, successful_at=? WHERE id=?""",
                        (name, url, provider, board if provider else "", data.enabled, status,
                         "" if changed else current["note"], None if changed else current["checked_at"],
                         None if changed else current["successful_at"], company_id))
            return store.one("SELECT * FROM companies WHERE id=?", (company_id,))
        except sqlite3.IntegrityError:
            fail("This company is already on your watchlist.", 409)

    @app.post("/api/companies", status_code=201)
    async def add_company(data: CompanyInput):
        return save_company(data)

    @app.patch("/api/companies/{company_id}")
    async def edit_company(company_id: int, data: CompanyInput):
        return save_company(data, company_id)

    @app.get("/api/jobs")
    async def jobs(view: str = "latest", region: str = "", search: str = Query("", max_length=130),
                   latest: bool = False, all_levels: bool = False, offset: int = Query(0, ge=0, le=50000),
                   early_only: bool = False, recent_days: int = Query(0, ge=0, le=365)):
        if region and region not in REGIONS:
            fail("Unknown location filter.")
        items = store.jobs(view=view, region=region, search=search.strip(), latest=latest,
                           all_levels=all_levels, offset=offset, early_only=early_only, recent_days=recent_days)
        return {"items": items[:30], "more": len(items) > 30}

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: int):
        return job_or_404(store, job_id)

    @app.post("/api/jobs", status_code=201)
    async def import_job(data: JobInput):
        url = required_url(data.url)
        hint = board_from_job_url(url)
        board = None
        details = None
        if hint:
            provider, token, external_id = hint
            board = store.one("SELECT * FROM companies WHERE provider=? AND board=? COLLATE NOCASE", (provider, token))
            if board:
                async with httpx.AsyncClient(timeout=12) as client:
                    records, _ = await fetch_board(client, board, asyncio.Semaphore(1))
                details = next((record for record in records if record["external_id"] == external_id), None)
        name = data.company.strip() or (board and board["name"])
        if not name or not (details or data.title.strip()):
            fail("Add the company and role title, or use a supported posting from the watchlist.", 422)
        if not details:
            details = build_job(hint[2] if board else canonical_url(url), data.title, "", data.location, "", data.description, url)
        with store.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO companies(name,url,provider,board,status) VALUES(?,?,'','','unsupported')",
                         (name, url))
            company_id = conn.execute("SELECT id FROM companies WHERE name=? COLLATE NOCASE", (name,)).fetchone()["id"]
            job_id, _ = store.upsert_job(conn, company_id, details, now(), manual=True)
            conn.execute("UPDATE jobs SET saved=1 WHERE id=?", (job_id,))
        return store.job(job_id)

    @app.patch("/api/jobs/{job_id}")
    async def flag_job(job_id: int, data: JobFlags):
        job_or_404(store, job_id)
        with store.connect() as conn:
            if data.saved is not None:
                conn.execute("UPDATE jobs SET saved=? WHERE id=?", (data.saved, job_id))
            if data.hidden is not None:
                conn.execute("UPDATE jobs SET hidden=? WHERE id=?", (data.hidden, job_id))
            if data.applied is not None:
                if data.applied:
                    conn.execute("UPDATE jobs SET applied_at=coalesce(applied_at,?), saved=1 WHERE id=?", (now(), job_id))
                else:
                    conn.execute("UPDATE jobs SET applied_at=NULL WHERE id=?", (job_id,))
        return store.job(job_id)

    @app.get("/api/jobs/{job_id}/people")
    async def people(job_id: int):
        return await find_people(store, job_or_404(store, job_id))

    @app.post("/api/jobs/{job_id}/draft", status_code=201)
    async def draft(job_id: int, data: DraftInput):
        job = job_or_404(store, job_id)
        if data.channel not in CHANNELS:
            fail("Select an outreach channel.")
        person = data.contact
        name = person.name.strip() or fail("Enter the contact name.")
        source = required_url(person.source) if person.source.strip() else ""
        first = name.split()[0]
        proof = data.evidence.strip().rstrip(".!?")
        text = (f"Hi {first}, I’m interested in the {job['title']} opening at {job['company']}. "
                + (f"{proof}. " if proof else "")
                + f"If you’re comfortable, would you consider referring me for this role? Here’s the posting: {job['url']}\n\nThank you!")
        with store.connect() as conn:
            conn.execute("""INSERT INTO contacts(company_id,name,title,source,notes) VALUES(?,?,?,?,?)
                         ON CONFLICT(company_id,name) DO NOTHING""",
                         (job["company_id"], name, person.title, source, person.notes))
            contact = conn.execute("SELECT id FROM contacts WHERE company_id=? AND name=?",
                                   (job["company_id"], name)).fetchone()
            stamp = now()
            conn.execute("""INSERT OR IGNORE INTO outreach(job_id,contact_id,channel,draft,created_at)
                            VALUES(?,?,?,?,?)""", (job_id, contact["id"], data.channel, text, stamp))
            item_id = conn.execute("SELECT id FROM outreach WHERE job_id=? AND contact_id=? AND channel=?",
                                   (job_id, contact["id"], data.channel)).fetchone()["id"]
            conn.execute("UPDATE jobs SET saved=1 WHERE id=?", (job_id,))
        return store.outreach(item_id)

    @app.get("/api/outreach")
    async def inbox():
        account = store.setting("gmail_account")
        return {"items": store.inbox(),
                "hints": store.all("SELECT id,at,subject FROM mail_seen WHERE account=? AND dismissed=0 ORDER BY at DESC LIMIT 25", (account,))}

    @app.get("/api/outreach/{item_id}")
    async def outreach(item_id: int):
        return store.outreach(item_id) or fail("Conversation not found.", 404)

    @app.patch("/api/outreach/{item_id}")
    async def edit_draft(item_id: int, data: DraftText):
        if not store.outreach(item_id):
            fail("Conversation not found.", 404)
        store.write("UPDATE outreach SET draft=? WHERE id=?", (data.text.strip() or fail("Enter a message."), item_id))
        return store.outreach(item_id)

    @app.patch("/api/outreach/{item_id}/follow-up")
    async def save_follow_up(item_id: int, data: DraftText):
        text = data.text.strip() or fail("Enter a follow-up message.")
        with store.connect() as conn:
            change = conn.execute("UPDATE outreach SET follow_up_draft=? WHERE id=? AND state='waiting'", (text, item_id))
            if not change.rowcount:
                fail("Follow-ups are available while waiting for a reply.", 409)
        return store.outreach(item_id)

    @app.patch("/api/outreach/{item_id}/conversation-link")
    async def save_conversation_link(item_id: int, data: ConversationLink):
        url = required_url(data.url) if data.url.strip() else ""
        with store.connect() as conn:
            change = conn.execute("UPDATE outreach SET conversation_url=? WHERE id=?", (url, item_id))
            if not change.rowcount:
                fail("Conversation not found.", 404)
        return store.outreach(item_id)

    @app.post("/api/outreach/{item_id}/events")
    async def event(item_id: int, data: OutreachEvent):
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM outreach WHERE id=?", (item_id,)).fetchone()
            if not current:
                fail("Conversation not found.", 404)
            stamp, action = now(), data.action
            previous = current["state"]
            if action == "sent" and previous == "waiting":
                return store.outreach(item_id)
            changes, message = {}, ""
            if action == "sent" and previous == "draft":
                message = current["draft"] if data.message is None else data.message.strip()
                if not message:
                    fail("Enter the request you sent.")
                changes = dict(state="waiting", sent_at=stamp, due_at=after(5), draft=message)
            elif action == "undo_send" and previous == "waiting":
                changes = dict(state="draft", sent_at=None, due_at=None, thread_id=None, follow_up_draft="",
                               mail_account=None, mail_anchor=None, mail_peer=None)
            elif action in {"checked", "snoozed", "followed_up"} and previous == "waiting":
                changes = dict(due_at=after(5 if action == "followed_up" else 3))
                if action != "snoozed":
                    changes["checked_at"] = stamp
                if action == "followed_up":
                    if data.expected_due_at is not None and data.expected_due_at != current["due_at"]:
                        fail("The next check date changed. Reopen the conversation and review its history.", 409)
                    message = current["follow_up_draft"] if data.message is None else data.message.strip()
                    if data.message is not None and not message:
                        fail("Enter the follow-up you sent.")
                    changes["follow_up_draft"] = ""
            elif action == "replied" and previous in {"waiting", "replied"}:
                changes = dict(state="replied", due_at=None, checked_at=stamp)
            elif action in {"offered", "submitted"} and previous in {"waiting", "replied", "offered"}:
                changes = dict(state=action, due_at=None)
            elif action == "closed" and previous != "closed":
                changes = dict(state="closed", due_at=None)
            else:
                fail("That action does not apply to this conversation.", 409)
            assignments = ",".join(f"{key}=?" for key in changes)
            conn.execute(f"UPDATE outreach SET {assignments} WHERE id=?", (*changes.values(), item_id))
            conn.execute("INSERT INTO events(outreach_id,kind,at,note,message) VALUES(?,?,?,?,?)",
                         (item_id, action, stamp, data.note.strip(), message))
        return store.outreach(item_id)

    @app.post("/api/rewrite")
    async def rewrite_draft(data: DraftText):
        try:
            return {"text": await rewrite(data.text)}
        except ValueError as exc:
            fail(str(exc))
        except (httpx.HTTPError, KeyError, TypeError):
            fail("The model could not rewrite this draft.", 502)

    @app.post("/api/gmail/authorize")
    async def authorize():
        try:
            return {"url": gmail.auth_url()}
        except ValueError as exc:
            fail(str(exc))

    @app.get("/oauth/google")
    async def oauth(state: str = "", code: str = "", error: str = ""):
        try:
            if error:
                raise ValueError("Google authorization was declined.")
            await gmail.connect(state, code)
        except (ValueError, httpx.HTTPError, OSError, ImportError, RuntimeError) as exc:
            store.set_setting("gmail_issue", mail_failure(exc))
        return RedirectResponse("/#settings", status_code=303)

    @app.post("/api/gmail/disconnect")
    async def disconnect():
        gmail.disconnect()
        store.set_setting("gmail_issue", "")
        return {"ok": True}

    @app.post("/api/gmail/check")
    async def gmail_check():
        return {"replies": await check_mail()}

    @app.get("/api/gmail/threads")
    async def gmail_threads():
        try:
            return await gmail.list_threads()
        except (ValueError, httpx.HTTPError, OSError, ImportError, RuntimeError) as exc:
            fail(mail_failure(exc), 502)

    @app.post("/api/outreach/{item_id}/gmail")
    async def link_thread(item_id: int, data: ThreadLink):
        try:
            await gmail.link(item_id, data.thread_id)
            return store.outreach(item_id)
        except sqlite3.IntegrityError:
            fail("That Gmail thread is already linked to a conversation.", 409)
        except (ValueError, httpx.HTTPError, OSError, ImportError, RuntimeError) as exc:
            fail(mail_failure(exc), 502)

    @app.patch("/api/gmail/options")
    async def gmail_options(data: MailOptions):
        store.set_setting("linkedin_hints", "1" if data.linkedin_hints else "0")
        return {"ok": True}

    @app.post("/api/gmail/hints/{hint_id}/dismiss")
    async def dismiss(hint_id: str):
        store.write("UPDATE mail_seen SET dismissed=1 WHERE account=? AND id=?",
                    (store.setting("gmail_account"), hint_id))
        return {"ok": True}

    return app


app = create_app()
