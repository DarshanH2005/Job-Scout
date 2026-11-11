"""Local web server and workflow actions."""

import asyncio
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import credentials
from .feeds import PROVIDERS, REGIONS, assess, board_from_job_url, build_job, canonical_url, clean, eligibility_evidence, fetch_board, fetch_json, geography, graduation_warning, public_url, work_authorization_evidence, workday_base, workday_detail, workday_path
from .gmail import Gmail, headers, incoming_reply
from .research import ResearchCache, find_people, rewrite
from .store import OUTREACH_SELECT, Store, now

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / ".env")
APP_DATA = Path(os.getenv("LOCALAPPDATA") or ROOT.parent) / "JobScout"
CHANNELS = ("linkedin", "gmail", "community")
GMAIL_SCHEDULE_INTERVAL = 30


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
    application_target: date | None = None


class CandidateProfile(BaseModel):
    graduation_year: int | None = Field(default=None, ge=2000, le=2100)


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


class ContactHandoff(BaseModel):
    profile_url: str | None = Field(default=None, max_length=1800)
    email: str | None = Field(default=None, max_length=254)


class OutreachEvent(BaseModel):
    action: str
    note: str = Field(default="", max_length=650)
    message: str | None = Field(default=None, max_length=4000)
    expected_due_at: str | None = Field(default=None, max_length=40)


class ThreadLink(BaseModel):
    thread_id: str = Field(pattern=r"^[0-9a-zA-Z_\-]{1,100}$")


class MailOptions(BaseModel):
    linkedin_hints: bool


class MailAuthorization(BaseModel):
    send: bool = False


class GmailSend(DraftText):
    account: str = Field(max_length=254)
    recipient: str = Field(max_length=254)
    expected_due_at: str | None = Field(default=None, max_length=40)


class GmailSchedule(DraftText):
    send_at: AwareDatetime
    thread_id: str = Field(pattern=r"^[0-9a-zA-Z_\-]{1,100}$")
    account: str = Field(max_length=254)
    recipient: str = Field(max_length=254)


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

    def start(self, scope="full"):
        if self.running:
            if scope == "retry":
                fail("A scan is already running.", 409)
            return self.store.one("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
        query = "SELECT * FROM companies WHERE enabled=1"
        if scope == "retry":
            query += " AND status='failed' AND provider<>''"
        companies = self.store.all(query + " ORDER BY id")
        if scope == "retry" and not companies:
            fail("There are no failed feeds to retry.", 409)
        scan_id = self.store.write("INSERT INTO scans(started_at,state,scope,total) VALUES(?,?,?,?)",
                                   (now(), "running", scope, len(companies)))
        full = self.store.last_full_scan() if scope == "retry" else None
        baseline_id = scan_id if scope == "full" else full and full["id"]
        self.task = asyncio.create_task(self.run(scan_id, companies, baseline_id))
        return self.store.one("SELECT * FROM scans WHERE id=?", (scan_id,))

    async def run(self, scan_id, companies, baseline_id):
        limits = {provider: asyncio.Semaphore(2 if provider == "workday" else 4) for provider in PROVIDERS}
        unsupported = len([row for row in companies if row["provider"] not in PROVIDERS])
        self.store.write("UPDATE scans SET unsupported=? WHERE id=?", (unsupported, scan_id))
        state, note = "completed", ""
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                async def check(company):
                    jobs, error = await fetch_board(client, company, limits[company["provider"]])
                    self.store.finish_source(scan_id, company, jobs, error, baseline_id)
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
    research_cache = ResearchCache()

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

    async def follow_up_loop():
        while True:
            await asyncio.sleep(GMAIL_SCHEDULE_INTERVAL)
            try:
                due = store.all("""SELECT f.*, o.due_at FROM gmail_follow_up_schedule f
                    JOIN outreach o ON o.id=f.outreach_id
                    WHERE f.send_at<=? AND f.issue='' AND o.state='waiting' AND o.gmail_send_pending=''
                    ORDER BY f.send_at LIMIT 10""", (now(),))
            except Exception:
                import logging
                logging.exception("Could not read scheduled Gmail follow-ups")
                continue
            for schedule in due:
                try:
                    data = GmailSend(text=schedule["message"], account=schedule["account"],
                                     recipient=schedule["recipient"], expected_due_at=schedule["due_at"])
                    await deliver_gmail(schedule["outreach_id"], data, schedule)
                except Exception as exc:
                    import logging
                    if not isinstance(exc, HTTPException):
                        logging.exception("Scheduled Gmail follow-up failed")
                    issue = exc.detail if isinstance(exc, HTTPException) else "An unexpected error occurred. Check Gmail Sent before rescheduling."
                    try:
                        store.write("UPDATE gmail_follow_up_schedule SET issue=? WHERE id=?", (issue, schedule["id"]))
                    except Exception:
                        logging.exception("Could not mark scheduled Gmail follow-up for review")

    @asynccontextmanager
    async def lifespan(_app):
        store.initialize(seed=seed)
        worker = asyncio.create_task(mail_loop())
        follow_ups = asyncio.create_task(follow_up_loop())
        _app.state.follow_ups = follow_ups
        try:
            yield
        finally:
            for task in (worker, follow_ups, scanner.task):
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
                "full_scan": store.last_full_scan(),
                "graduation_year": store.graduation_year(),
                "search_configured": bool(credentials.brave_key()),
                "search_from_env": bool(os.getenv("SCOUT_BRAVE_KEY")),
                "rewrite_configured": bool(os.getenv("SCOUT_OPENAI_KEY") and os.getenv("SCOUT_OPENAI_MODEL")),
                "mail": {"account": account, "configured": credentials.google_configured(),
                         "send_enabled": bool(account and store.setting("gmail_send_enabled") == "1"),
                         "scheduling": bool(getattr(app.state, "follow_ups", None) and not app.state.follow_ups.done()),
                         "from_env": bool(os.getenv("SCOUT_GOOGLE_CLIENT_FILE")),
                         "checked_at": store.setting("gmail_checked_at"),
                         "issue": store.setting("gmail_issue"),
                         "linkedin_hints": store.setting("linkedin_hints") == "1"}}

    @app.put("/api/profile")
    async def update_profile(data: CandidateProfile):
        store.set_setting("graduation_year", str(data.graduation_year) if data.graduation_year is not None else "")
        return {"graduation_year": store.graduation_year()}

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

    @app.post("/api/scans/retry-failed", status_code=202)
    async def retry_failed():
        return scanner.start("retry")

    @app.get("/api/companies")
    async def companies():
        return store.all("SELECT * FROM companies ORDER BY name COLLATE NOCASE")

    @app.get("/api/next-actions")
    async def next_actions(today: date):
        return store.next_actions(today.isoformat())

    def save_company(data, company_id=None):
        if scanner.running:
            fail("The current scan needs to finish before editing the watchlist.", 409)
        provider = data.provider
        board = data.board.strip()
        if provider and (provider not in PROVIDERS or not re.fullmatch(r"[A-Za-z0-9._-]+", board)):
            fail("Select a supported provider and enter its board identifier.")
        if provider == "workday":
            try:
                workday_base(board)
            except ValueError as exc:
                fail(str(exc))
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
                   early_only: bool = False, recent_days: int = Query(0, ge=0, le=365),
                   date_basis: Literal["either", "posted", "seen"] = "either"):
        if region and region not in REGIONS:
            fail("Unknown location filter.")
        items = store.jobs(view=view, region=region, search=search.strip(), latest=latest,
                           all_levels=all_levels, offset=offset, early_only=early_only, recent_days=recent_days,
                           date_basis=date_basis)
        return {"items": items[:30], "more": len(items) > 30}

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: int):
        item = job_or_404(store, job_id)
        if item["provider"] not in {"smartrecruiters", "workday"} or item["description"]:
            return item
        company = store.one("SELECT board FROM companies WHERE id=?", (item["company_id"],))
        if not company or not re.fullmatch(r"[A-Za-z0-9._-]+", company["board"]):
            item["details_status"] = "unavailable"
            return item
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                if item["provider"] == "workday":
                    description, detail_location, workplace = await workday_detail(client, company["board"], item["external_id"])
                else:
                    url = PROVIDERS["smartrecruiters"].format(board=quote(company["board"], safe=""))
                    url += "/" + quote(item["external_id"], safe="")
                    detail = await fetch_json(client, url)
            if item["provider"] == "smartrecruiters":
                detail_location = item["location"]
                if (str(detail["id"]) != item["external_id"] or detail.get("active") is False
                        or detail["company"]["identifier"].casefold() != company["board"].casefold()):
                    raise ValueError("The employer posting did not match.")
                sections = detail["jobAd"]["sections"]
                parts = []
                for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
                    section = sections.get(key) or {}
                    title, text = clean(section.get("title")), clean(section.get("text"))
                    if text:
                        parts.append("\n".join(filter(None, (title, text))))
                description = "\n".join(parts)[:30000]
                location = detail.get("location") or {}
                workplace = "Hybrid" if location.get("hybrid") else "Remote" if location.get("remote") else ""
            if not description:
                raise ValueError("The employer posting contained no description.")
            item["description"] = description
            item["eligibility"] = eligibility_evidence(description)
            item["graduation_warning"] = graduation_warning(description, store.graduation_year())
            item["work_authorization"] = work_authorization_evidence(description)
            if not workplace:
                workplace = next((value for value in ("Remote", "Hybrid")
                                  if item["evidence"] == f'{item["location"]} · {value}'), "")
            region, evidence = geography(detail_location or item["location"], workplace, description)
            if region != item["region"] or evidence != item["evidence"]:
                item["detail_location"] = {"region": region, "evidence": evidence}
            detailed_score, detailed_reason = assess(item["title"], item["department"], description, item["region"])
            if detailed_score < item["score"]:
                item["detail_assessment"] = detailed_reason or "The full posting indicates greater experience requirements. Review the qualifications."
            item["details_status"] = "loaded"
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            item["details_status"] = "unavailable"
        return item

    @app.post("/api/jobs/{job_id}/verify")
    async def verify_job(job_id: int):
        saved = job_or_404(store, job_id)
        company = store.one("SELECT * FROM companies WHERE id=?", (saved["company_id"],))
        if saved["manual"] or company["provider"] not in PROVIDERS:
            return {"status": "manual", "checked_at": None, "detail": "Open the posting to verify this manually added role."}
        if company["provider"] == "workday":
            stamp = now()
            try:
                base, public_base = workday_base(company["board"])
                path = workday_path(saved["external_id"])
                async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                    detail = await fetch_json(client, base + path)
                info = detail["jobPostingInfo"]
                if canonical_url(info["externalUrl"]) != canonical_url(public_base + path):
                    raise ValueError("The employer posting did not match.")
                if info.get("canApply") is not True:
                    return {"status":"unavailable", "checked_at":stamp, "detail":"The employer did not confirm that applications are open."}
                return {"status":"listed", "checked_at":stamp, "detail":"The employer's posting is accepting applications."}
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return {"status":"not_listed", "checked_at":stamp, "detail":"The employer posting was not found."}
                return {"status":"unavailable", "checked_at":stamp, "detail":"The employer posting could not be checked."}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                return {"status":"unavailable", "checked_at":stamp, "detail":"The employer posting could not be checked."}
        async with httpx.AsyncClient(timeout=12) as client:
            postings, error = await fetch_board(client, company, asyncio.Semaphore(1))
        stamp = now()
        if error:
            return {"status": "unavailable", "checked_at": stamp, "detail": error}
        listed = any(posting["external_id"] == saved["external_id"] or posting["canonical_url"] == saved["canonical_url"]
                     for posting in postings)
        return {"status": "listed" if listed else "not_listed", "checked_at": stamp, "detail": ""}

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
            if "application_target" in data.model_fields_set:
                target = data.application_target
                conn.execute("UPDATE jobs SET application_target=?, saved=CASE WHEN ? THEN 1 ELSE saved END WHERE id=?",
                             (target.isoformat() if target else None, target is not None, job_id))
            if data.applied is not None:
                if data.applied:
                    conn.execute("UPDATE jobs SET applied_at=coalesce(applied_at,?), saved=1 WHERE id=?", (now(), job_id))
                else:
                    conn.execute("UPDATE jobs SET applied_at=NULL WHERE id=?", (job_id,))
        return store.job(job_id)

    @app.get("/api/jobs/{job_id}/people")
    async def people(job_id: int):
        return await find_people(store, job_or_404(store, job_id), research_cache)

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

    @app.patch("/api/outreach/{item_id}/contact-handoff")
    async def save_contact_handoff(item_id: int, data: ContactHandoff):
        item = store.outreach(item_id) or fail("Conversation not found.", 404)
        profile = None if data.profile_url is None else data.profile_url.strip()
        email = None if data.email is None else data.email.strip()
        if profile:
            required_url(profile)
            parsed = urlsplit(profile)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or not (host == "linkedin.com" or host.endswith(".linkedin.com"))
                    or not re.fullmatch(r"/in/[^/]+/?", parsed.path)):
                fail("Enter a LinkedIn profile URL, such as https://www.linkedin.com/in/name/.")
        if email and not re.fullmatch(r"[^\s@<>,;]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email):
            fail("Enter one valid email address.")
        store.write(
            "UPDATE contacts SET profile_url=COALESCE(?,profile_url), email=COALESCE(?,email) WHERE id=?",
            (profile, email, item["contact_id"]),
        )
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
            if current["gmail_send_pending"]:
                expired = datetime.fromisoformat(current["gmail_send_pending"]) + timedelta(minutes=1) <= datetime.now(timezone.utc)
                recovery = "sent" if previous == "draft" else "followed_up"
                if not expired or action not in {recovery, "closed"}:
                    fail("Check Gmail Sent to verify the delivery before changing this conversation.", 409)
            if action == "sent" and previous == "waiting":
                return store.outreach(item_id)
            changes, message = {}, ""
            if current["gmail_send_pending"]:
                changes["gmail_send_pending"] = ""
            if action == "sent" and previous == "draft":
                message = current["draft"] if data.message is None else data.message.strip()
                if not message:
                    fail("Enter the request you sent.")
                changes = dict(state="waiting", sent_at=stamp, due_at=after(5), draft=message)
            elif action == "undo_send" and previous == "waiting":
                if current["gmail_sent"]:
                    fail("Gmail has already sent this request.", 409)
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
            if changes.get("state", previous) != "waiting" or action in {"followed_up", "snoozed"}:
                conn.execute("DELETE FROM gmail_follow_up_schedule WHERE outreach_id=?", (item_id,))
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
    async def authorize(data: MailAuthorization | None = None):
        try:
            return {"url": gmail.auth_url(send=bool(data and data.send))}
        except ValueError as exc:
            fail(str(exc))

    @app.post("/api/outreach/{item_id}/gmail/follow-up-schedule")
    async def schedule_follow_up(item_id: int, data: GmailSchedule):
        text = data.text.strip() or fail("Enter a follow-up message.")
        send_at = data.send_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute(OUTREACH_SELECT + " WHERE o.id=?", (item_id,)).fetchone()
            if not item:
                fail("Conversation not found.", 404)
            if (item["channel"] != "gmail" or item["state"] != "waiting" or item["gmail_send_pending"]
                    or item["thread_id"] != data.thread_id or not item["mail_peer"]
                    or item["mail_peer"] != data.recipient or item["mail_account"] != data.account
                    or item["mail_account"] != store.setting("gmail_account")
                    or store.setting("gmail_send_enabled") != "1"):
                fail("Connect Gmail for sending and reopen the linked conversation before scheduling.", 409)
            if item["schedule_id"]:
                fail("Cancel the existing schedule before setting a new one.", 409)
            if send_at <= now():
                fail("Choose a date and time in the future.")
            conn.execute("""INSERT INTO gmail_follow_up_schedule(id,outreach_id,send_at,message,account,recipient,thread_id)
                VALUES(?,?,?,?,?,?,?)""", (uuid4().hex, item_id, send_at, text, item["mail_account"],
                                         item["mail_peer"], item["thread_id"]))
            conn.execute("UPDATE outreach SET follow_up_draft=? WHERE id=?", (text, item_id))
        return store.outreach(item_id)

    @app.delete("/api/outreach/{item_id}/gmail/follow-up-schedule/{schedule_id}")
    async def cancel_follow_up(item_id: int, schedule_id: str):
        with store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT gmail_send_pending FROM outreach WHERE id=?", (item_id,)).fetchone()
            if not item:
                fail("Conversation not found.", 404)
            if item["gmail_send_pending"]:
                fail("Check Gmail Sent before changing this conversation.", 409)
            deleted = conn.execute("DELETE FROM gmail_follow_up_schedule WHERE id=? AND outreach_id=?",
                                   (schedule_id, item_id))
            if not deleted.rowcount:
                fail("This schedule changed. Reopen the conversation to review it.", 409)
        return store.outreach(item_id)

    @app.post("/api/outreach/{item_id}/gmail/send")
    async def send_gmail(item_id: int, data: GmailSend):
        return await deliver_gmail(item_id, data)

    async def deliver_gmail(item_id: int, data: GmailSend, schedule=None):
        text = data.text.strip() or fail("Enter a message.")
        follow_up = schedule is not None or data.expected_due_at is not None
        async with gmail.lock:
            item = store.outreach(item_id) or fail("Conversation not found.", 404)
            if item["channel"] != "gmail" or item["state"] != ("waiting" if follow_up else "draft"):
                fail("This conversation is no longer ready to send.", 409)
            if item["gmail_send_pending"]:
                fail("A Gmail send is unconfirmed. Check Gmail Sent before recording it here.", 409)
            thread_id = item["thread_id"] if follow_up else None
            if schedule is None and item["schedule_id"]:
                fail("Cancel the scheduled follow-up before sending manually.", 409)
            if schedule is not None and (item["schedule_id"] != schedule["id"] or item["scheduled_issue"]
                                         or thread_id != schedule["thread_id"]):
                fail("The scheduled Gmail conversation changed.", 409)
            recipient = item["mail_peer"] if follow_up else item["email"]
            if not recipient or recipient != data.recipient:
                fail("Review the saved recipient before sending.", 409)
            if follow_up and (not thread_id or item["due_at"] != data.expected_due_at
                              or item["mail_account"] != data.account):
                fail("Reopen the linked Gmail conversation and review its history.", 409)
            try:
                account, token = await gmail.token(send=True)
                if account != data.account:
                    fail("The connected Gmail account changed. Review the draft again.", 409)
                subject = f"Referral request for {item['job_title']}"
                if follow_up:
                    messages = (await gmail.thread(token, thread_id)).get("messages", [])
                    if any(incoming_reply(message, item["mail_anchor"], recipient) for message in messages):
                        gmail.record_reply(item)
                        return store.outreach(item_id)
                    sent = [message for message in messages if "SENT" in message.get("labelIds", [])]
                    previous = headers(max(sent, key=lambda message: int(message.get("internalDate", 0)))) if sent else {}
                    reply_to, subject = previous.get("message-id"), previous.get("subject")
                    if not reply_to or not subject:
                        fail("Gmail did not return the headers needed to send within this thread.", 409)
                message = gmail.message(account, recipient, subject, text)
                if follow_up:
                    message["In-Reply-To"] = reply_to
                    message["References"] = " ".join(filter(None, (previous.get("references"), reply_to)))
            except (ValueError, httpx.HTTPError, OSError, ImportError, RuntimeError) as exc:
                fail(mail_failure(exc), 502)

            started = datetime.now(timezone.utc)
            stamp = started.isoformat(timespec="microseconds")
            draft_field = "follow_up_draft" if follow_up else "draft"
            with store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(OUTREACH_SELECT + " WHERE o.id=?", (item_id,)).fetchone()
                if any(current[key] != item[key] for key in ("state", "gmail_send_pending", "due_at", "email",
                                                             "thread_id", "mail_peer", "mail_account", "schedule_id",
                                                             "scheduled_issue", draft_field)):
                    fail("This conversation changed. Reopen it before sending.", 409)
                if schedule is not None and (store.setting("gmail_account") != account
                                             or store.setting("gmail_send_enabled") != "1"):
                    fail("Reconnect Gmail and schedule the follow-up again.", 409)
                conn.execute(f"UPDATE outreach SET gmail_send_pending=?,{draft_field}=? WHERE id=?", (stamp, text, item_id))

            try:
                delivery = await gmail.send_message(token, message, thread_id)
                delivered_thread = delivery.get("threadId", "")
                if (not delivery.get("id") or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", delivered_thread)
                        or (thread_id and delivered_thread != thread_id)):
                    raise ValueError("Gmail did not confirm the expected thread.")
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if 400 <= status < 500 and status != 408:
                    store.write("UPDATE outreach SET gmail_send_pending='' WHERE id=? AND gmail_send_pending=?", (item_id, stamp))
                    fail("Gmail rejected the send. Check the recipient and Gmail sending permission before trying again.", 502)
                fail("Gmail did not confirm delivery. Check Gmail Sent before recording it here.", 502)
            except (httpx.RequestError, ValueError, TypeError, AttributeError):
                fail("Gmail did not confirm delivery. Check Gmail Sent before recording it here.", 502)

            with store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT id FROM outreach WHERE thread_id=? AND id<>?",
                                        (delivered_thread, item_id)).fetchone()
                if existing:
                    fail("Gmail used a thread already tracked by another referral. Check Gmail Sent before recording it here.", 409)
                if follow_up:
                    conn.execute("""UPDATE outreach SET checked_at=?,due_at=?,follow_up_draft='',gmail_send_pending=''
                        WHERE id=?""", (now(), after(5), item_id))
                    conn.execute("DELETE FROM gmail_follow_up_schedule WHERE outreach_id=?", (item_id,))
                else:
                    conn.execute("""UPDATE outreach SET state='waiting',sent_at=?,due_at=?,thread_id=?,mail_account=?,
                        mail_anchor=?,mail_peer=?,gmail_send_pending='',gmail_sent=1 WHERE id=?""",
                        (now(), after(5), delivered_thread, account, int(started.timestamp() * 1000) - 1,
                         recipient.lower(), item_id))
                conn.execute("INSERT INTO events(outreach_id,kind,at,note,message) VALUES(?,?,?,?,?)",
                             (item_id, "followed_up" if follow_up else "sent", now(),
                              "Scheduled follow-up sent via Gmail." if schedule else "Sent via Gmail.", text))
            return store.outreach(item_id)

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
        async with gmail.lock:
            account = store.setting("gmail_account")
            gmail.disconnect()
            store.write("UPDATE gmail_follow_up_schedule SET issue=? WHERE account=? AND issue=''",
                        ("Gmail was disconnected. Reconnect, then cancel and schedule again.", account))
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
            async with gmail.lock:
                await gmail.link(item_id, data.thread_id)
                return store.outreach(item_id)
        except sqlite3.IntegrityError:
            fail("That Gmail thread is already linked to a conversation.", 409)
        except ValueError as exc:
            fail(str(exc), 409)
        except (httpx.HTTPError, OSError, ImportError, RuntimeError) as exc:
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
