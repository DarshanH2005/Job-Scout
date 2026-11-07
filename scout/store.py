"""SQLite persistence for a single local Job Scout workspace."""

import csv
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .feeds import EARLY_SCORE

DATA = Path(__file__).with_name("companies.csv")
EXPANDED_SOURCES = ("Databricks", "HP", "ServiceNow", "Snowflake", "Zscaler")

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    url TEXT NOT NULL, provider TEXT NOT NULL, board TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '', checked_at TEXT, successful_at TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    state TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'full', total INTEGER NOT NULL, checked INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0, unsupported INTEGER NOT NULL DEFAULT 0,
    fresh INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
    external_id TEXT NOT NULL, title TEXT NOT NULL, department TEXT NOT NULL,
    location TEXT NOT NULL, description TEXT NOT NULL, url TEXT NOT NULL, apply_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL, posted_at TEXT, region TEXT NOT NULL,
    evidence TEXT NOT NULL, score INTEGER NOT NULL, reason TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, new_scan INTEGER REFERENCES scans(id),
    misses INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
    manual INTEGER NOT NULL DEFAULT 0, saved INTEGER NOT NULL DEFAULT 0, applied_at TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    UNIQUE(company_id, external_id)
);
CREATE INDEX IF NOT EXISTS jobs_by_url ON jobs(company_id, canonical_url);
CREATE INDEX IF NOT EXISTS jobs_shortlist ON jobs(archived, region, score);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
    name TEXT NOT NULL COLLATE NOCASE, title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
    UNIQUE(company_id, name)
);
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id), channel TEXT NOT NULL,
    draft TEXT NOT NULL, follow_up_draft TEXT NOT NULL DEFAULT '', conversation_url TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
    sent_at TEXT, due_at TEXT, checked_at TEXT, thread_id TEXT UNIQUE, mail_account TEXT, mail_anchor INTEGER, mail_peer TEXT,
    UNIQUE(job_id, contact_id, channel)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, outreach_id INTEGER NOT NULL REFERENCES outreach(id),
    kind TEXT NOT NULL, at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mail_seen (
    account TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
    at TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', dismissed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, id)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

JOB_FIELDS = ("external_id", "title", "department", "location", "description", "url", "apply_url",
              "canonical_url", "posted_at", "region", "evidence", "score", "reason")
OUTREACH_SELECT = """
SELECT o.*, c.name AS person, c.source, j.title AS job_title, j.url AS job_url,
       j.applied_at, co.name AS company
FROM outreach o JOIN contacts c ON c.id=o.contact_id JOIN jobs j ON j.id=o.job_id
JOIN companies co ON co.id=j.company_id
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=12)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self, seed=True):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            if not any(row["name"] == "scope" for row in conn.execute("PRAGMA table_info(scans)")):
                conn.execute("ALTER TABLE scans ADD COLUMN scope TEXT NOT NULL DEFAULT 'full'")
            if not any(row["name"] == "hidden" for row in conn.execute("PRAGMA table_info(jobs)")):
                conn.execute("ALTER TABLE jobs ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            if not any(row["name"] == "follow_up_draft" for row in conn.execute("PRAGMA table_info(outreach)")):
                conn.execute("ALTER TABLE outreach ADD COLUMN follow_up_draft TEXT NOT NULL DEFAULT ''")
            if not any(row["name"] == "conversation_url" for row in conn.execute("PRAGMA table_info(outreach)")):
                conn.execute("ALTER TABLE outreach ADD COLUMN conversation_url TEXT NOT NULL DEFAULT ''")
            if not any(row["name"] == "message" for row in conn.execute("PRAGMA table_info(events)")):
                conn.execute("ALTER TABLE events ADD COLUMN message TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE scans SET state='interrupted', finished_at=? WHERE state='running'", (now(),))
            if seed and not conn.execute("SELECT 1 FROM settings WHERE key='seeded'").fetchone():
                with DATA.open(encoding="utf-8-sig", newline="") as handle:
                    conn.executemany(
                        "INSERT OR IGNORE INTO companies(name,url,provider,board,status) VALUES(?,?,?,?,?)",
                        ((row["name"], row["url"], row["provider"], row["board"],
                          "pending" if row["provider"] else "unsupported") for row in csv.DictReader(handle)),
                    )
                conn.execute("INSERT INTO settings VALUES('seeded','1')")
            if seed and not conn.execute("SELECT 1 FROM settings WHERE key='sources_2'").fetchone():
                with DATA.open(encoding="utf-8-sig", newline="") as handle:
                    conn.executemany(
                        "UPDATE companies SET provider=?,board=?,status='pending',note='' WHERE name=? AND url=? AND provider='' AND board=''",
                        ((row["provider"], row["board"], row["name"], row["url"])
                         for row in csv.DictReader(handle) if row["name"] in EXPANDED_SOURCES),
                    )
                conn.execute("INSERT INTO settings VALUES('sources_2','1')")

    def one(self, sql, params=()):
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def write(self, sql, params=()):
        with self.connect() as conn:
            return conn.execute(sql, params).lastrowid

    def setting(self, key, default=""):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.write("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def stats(self):
        return self.one("""
            SELECT
            (SELECT COUNT(*) FROM companies) AS companies,
            (SELECT COUNT(*) FROM companies WHERE enabled AND provider<>'') AS configured,
            (SELECT COUNT(*) FROM companies WHERE enabled AND provider<>'' AND status='failed') AS failed_feeds,
            (SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id=j.company_id
              WHERE j.archived=0 AND j.hidden=0 AND j.score>0 AND (c.enabled=1 OR j.manual=1)) AS matches,
            (SELECT COUNT(*) FROM jobs WHERE saved=1 OR applied_at IS NOT NULL) AS saved,
            (SELECT COUNT(*) FROM jobs WHERE hidden=1) AS hidden,
            (SELECT COUNT(*) FROM outreach WHERE state='waiting' AND due_at<=?) AS due
        """, (now(),))

    def last_full_scan(self):
        return self.one("SELECT * FROM scans WHERE scope='full' AND state='completed' ORDER BY id DESC LIMIT 1")

    def finish_source(self, scan_id, company, jobs, error, baseline_id):
        checked_at = now()
        company_id = company["id"]
        with self.connect() as conn:
            if error:
                conn.execute("UPDATE companies SET status='failed', note=?, checked_at=? WHERE id=?",
                             (error, checked_at, company_id))
                conn.execute("UPDATE scans SET failed=failed+1 WHERE id=?", (scan_id,))
                return
            conn.execute("UPDATE companies SET status='checked', note=?, checked_at=?, successful_at=? WHERE id=?",
                         (f"{len(jobs)} published openings", checked_at, checked_at, company_id))
            fresh = 0
            new_scan = baseline_id if company["successful_at"] else None
            for job in jobs:
                _, inserted = self.upsert_job(conn, company_id, job, checked_at, new_scan)
                fresh += bool(inserted and new_scan and job["score"])
            # Two successful snapshots must agree before a posting disappears.
            conn.execute("""UPDATE jobs SET misses=misses+1, archived=CASE WHEN misses>=1 THEN 1 ELSE archived END
                            WHERE company_id=? AND manual=0 AND last_seen<>? AND archived=0""",
                         (company_id, checked_at))
            conn.execute("UPDATE scans SET checked=checked+1, fresh=fresh+? WHERE id=?", (fresh, scan_id))

    @staticmethod
    def upsert_job(conn, company_id, job, observed, scan_id=None, manual=False):
        current = conn.execute("""SELECT id, manual FROM jobs WHERE company_id=? AND
                                   (external_id=? OR canonical_url=?) ORDER BY external_id=? DESC LIMIT 1""",
                               (company_id, job["external_id"], job["canonical_url"], job["external_id"])).fetchone()
        if current:
            if not manual:
                updates = ",".join(f"{field}=?" for field in JOB_FIELDS)
                conn.execute(f"UPDATE jobs SET {updates}, last_seen=?, misses=0, archived=0, manual=0 WHERE id=?",
                             (*[job[field] for field in JOB_FIELDS], observed, current["id"]))
            return current["id"], False
        fields = ",".join(JOB_FIELDS)
        values = ",".join("?" for _ in JOB_FIELDS)
        result = conn.execute(f"""INSERT INTO jobs(company_id,{fields},first_seen,last_seen,new_scan,manual)
                                  VALUES(?,{values},?,?,?,?)""",
                              (company_id, *[job[field] for field in JOB_FIELDS], observed, observed, scan_id, manual))
        return result.lastrowid, True

    def jobs(self, *, view, region, search, latest, all_levels, offset, early_only=False, recent_days=0):
        args = []
        clauses = []
        if view == "saved":
            clauses.append("(j.saved=1 OR j.applied_at IS NOT NULL)")
        elif view == "hidden":
            clauses.append("j.hidden=1")
        else:
            clauses.append("j.archived=0 AND j.hidden=0 AND (c.enabled=1 OR j.manual=1)")
            if early_only:
                clauses.append("j.score>=?")
                args.append(EARLY_SCORE)
            elif not all_levels:
                clauses.append("(j.score>0 OR j.manual=1)")
            if latest:
                clauses.append("j.new_scan=(SELECT MAX(id) FROM scans WHERE scope='full' AND state='completed')")
            if recent_days:
                clauses.append("coalesce(julianday(j.posted_at),julianday(j.first_seen))>=julianday('now')-?")
                args.append(recent_days)
        if region:
            clauses.append("j.region=?")
            args.append(region)
        if search:
            clauses.append("instr(lower(j.title || ' ' || c.name || ' ' || j.department), lower(?))>0")
            args.append(search)
        where = " AND ".join(clauses)
        order = """CASE j.region WHEN 'india_remote' THEN 0 WHEN 'india' THEN 0 WHEN 'us_remote' THEN 2
                   WHEN 'unclear' THEN 3 ELSE 4 END, c.status='failed', j.score DESC,
                   coalesce(julianday(j.posted_at),julianday(j.first_seen)) DESC, j.id DESC"""
        return self.all(f"""SELECT j.id,j.title,j.location,j.url,j.apply_url,j.posted_at,j.first_seen,j.last_seen,j.new_scan,
                                   j.region,j.evidence,j.reason,j.score,j.saved,j.applied_at,j.archived,j.manual,j.hidden,
                                   c.name AS company,c.provider,c.status AS source_status
                            FROM jobs j JOIN companies c ON c.id=j.company_id WHERE {where}
                            ORDER BY {order} LIMIT 31 OFFSET ?""", (*args, offset))

    def job(self, job_id):
        row = self.one("""SELECT j.*, c.name AS company, c.provider, c.status AS source_status
                          FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?""", (job_id,))
        if row:
            row["referrals"] = self.all("""SELECT o.id, o.contact_id, o.channel, o.state, o.due_at, c.name AS person
                FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE o.job_id=?
                ORDER BY o.state IN ('closed','submitted'), o.id DESC""", (job_id,))
        return row

    def inbox(self):
        return self.all(OUTREACH_SELECT + " ORDER BY CASE WHEN o.state='waiting' THEN o.due_at END ASC NULLS LAST, o.id DESC")

    def outreach(self, item_id):
        row = self.one(OUTREACH_SELECT + " WHERE o.id=?", (item_id,))
        if row:
            row["events"] = self.all("SELECT kind,at,note,message FROM events WHERE outreach_id=? ORDER BY id DESC", (item_id,))
        return row


