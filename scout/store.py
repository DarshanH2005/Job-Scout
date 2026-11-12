"""SQLite persistence for a single local Job Scout workspace."""

import csv
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .feeds import EARLY_SCORE, SENIOR, assess, eligibility_evidence, graduation_warning, required_timezone, work_authorization_evidence

DATA = Path(__file__).with_name("companies.csv")
EXPANDED_SOURCES = ("Databricks", "HP", "ServiceNow", "Snowflake", "Zscaler")
WORKDAY_SOURCES = ("Adobe", "Intel", "Salesforce", "Workday")

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
    canonical_url TEXT NOT NULL, posted_at TEXT, deadline_at TEXT, region TEXT NOT NULL,
    evidence TEXT NOT NULL, score INTEGER NOT NULL, reason TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, new_scan INTEGER REFERENCES scans(id),
    misses INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
    manual INTEGER NOT NULL DEFAULT 0, saved INTEGER NOT NULL DEFAULT 0, applied_at TEXT,
    hidden INTEGER NOT NULL DEFAULT 0, application_target TEXT,
    UNIQUE(company_id, external_id)
);
CREATE INDEX IF NOT EXISTS jobs_by_url ON jobs(company_id, canonical_url);
CREATE INDEX IF NOT EXISTS jobs_shortlist ON jobs(archived, region, score);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
    name TEXT NOT NULL COLLATE NOCASE, title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
    profile_url TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
    UNIQUE(company_id, name)
);
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id), channel TEXT NOT NULL,
    draft TEXT NOT NULL, follow_up_draft TEXT NOT NULL DEFAULT '', conversation_url TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
    sent_at TEXT, due_at TEXT, checked_at TEXT, thread_id TEXT UNIQUE, mail_account TEXT, mail_anchor INTEGER, mail_peer TEXT,
    gmail_send_pending TEXT NOT NULL DEFAULT '', gmail_sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE(job_id, contact_id, channel)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, outreach_id INTEGER NOT NULL REFERENCES outreach(id),
    kind TEXT NOT NULL, at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS gmail_follow_up_schedule (
    id TEXT PRIMARY KEY, outreach_id INTEGER NOT NULL UNIQUE REFERENCES outreach(id),
    send_at TEXT NOT NULL, message TEXT NOT NULL, account TEXT NOT NULL, recipient TEXT NOT NULL,
    thread_id TEXT NOT NULL, issue TEXT NOT NULL DEFAULT '', reviewed_message_ids TEXT NOT NULL DEFAULT '[]',
    review_pending_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS gmail_initial_schedule (
    id TEXT PRIMARY KEY, outreach_id INTEGER NOT NULL UNIQUE REFERENCES outreach(id),
    send_at TEXT NOT NULL, message TEXT NOT NULL, account TEXT NOT NULL, recipient TEXT NOT NULL,
    job_title TEXT NOT NULL, job_url TEXT NOT NULL, auto_text TEXT NOT NULL DEFAULT '',
    auto_days INTEGER NOT NULL DEFAULT 0, issue TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mail_seen (
    account TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
    at TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', dismissed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, id)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

JOB_FIELDS = ("external_id", "title", "department", "location", "description", "url", "apply_url",
              "canonical_url", "posted_at", "deadline_at", "region", "evidence", "score", "reason")
OUTREACH_SELECT = """
SELECT o.*, c.name AS person, c.source, c.profile_url, c.email, j.title AS job_title, j.url AS job_url,
       j.applied_at, co.name AS company, f.id AS schedule_id, f.send_at AS scheduled_send_at,
       f.message AS scheduled_message, f.issue AS scheduled_issue, f.reviewed_message_ids AS scheduled_reviewed_message_ids,
       f.review_pending_ids AS scheduled_review_pending_ids,
       s.id AS initial_schedule_id, s.send_at AS initial_scheduled_send_at, s.message AS initial_scheduled_message,
       s.account AS initial_scheduled_account, s.recipient AS initial_scheduled_recipient,
       s.job_title AS initial_scheduled_job_title, s.job_url AS initial_scheduled_job_url,
       s.auto_text AS initial_scheduled_auto_text, s.auto_days AS initial_scheduled_auto_days,
       s.issue AS initial_scheduled_issue, j.archived AS job_archived, co.status AS job_source_status,
       CASE WHEN o.state IN ('closed','submitted') THEN 'completed'
            WHEN o.gmail_send_pending<>'' THEN 'delivery'
            WHEN coalesce(s.issue,'')<>'' OR coalesce(f.issue,'')<>'' THEN 'schedule_issue'
            WHEN o.state='replied' THEN 'reply'
            WHEN o.state='offered' THEN 'offer'
            WHEN s.id IS NOT NULL OR f.id IS NOT NULL THEN 'scheduled'
            WHEN o.state='draft' THEN 'draft'
            WHEN o.state='waiting' AND o.due_at IS NOT NULL AND julianday(o.due_at)<=julianday('now') THEN 'due'
            ELSE 'waiting' END AS inbox_category
FROM outreach o JOIN contacts c ON c.id=o.contact_id JOIN jobs j ON j.id=o.job_id
JOIN companies co ON co.id=j.company_id
LEFT JOIN gmail_follow_up_schedule f ON f.outreach_id=o.id
LEFT JOIN gmail_initial_schedule s ON s.outreach_id=o.id
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
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "hidden" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            if "deadline_at" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN deadline_at TEXT")
            if "application_target" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN application_target TEXT")
            if not any(row["name"] == "follow_up_draft" for row in conn.execute("PRAGMA table_info(outreach)")):
                conn.execute("ALTER TABLE outreach ADD COLUMN follow_up_draft TEXT NOT NULL DEFAULT ''")
            if not any(row["name"] == "conversation_url" for row in conn.execute("PRAGMA table_info(outreach)")):
                conn.execute("ALTER TABLE outreach ADD COLUMN conversation_url TEXT NOT NULL DEFAULT ''")
            outreach_columns = {row["name"] for row in conn.execute("PRAGMA table_info(outreach)")}
            if "gmail_send_pending" not in outreach_columns:
                conn.execute("ALTER TABLE outreach ADD COLUMN gmail_send_pending TEXT NOT NULL DEFAULT ''")
            if "gmail_sent" not in outreach_columns:
                conn.execute("ALTER TABLE outreach ADD COLUMN gmail_sent INTEGER NOT NULL DEFAULT 0")
            if not any(row["name"] == "reviewed_message_ids" for row in conn.execute("PRAGMA table_info(gmail_follow_up_schedule)")):
                conn.execute("ALTER TABLE gmail_follow_up_schedule ADD COLUMN reviewed_message_ids TEXT NOT NULL DEFAULT '[]'")
            if not any(row["name"] == "review_pending_ids" for row in conn.execute("PRAGMA table_info(gmail_follow_up_schedule)")):
                conn.execute("ALTER TABLE gmail_follow_up_schedule ADD COLUMN review_pending_ids TEXT NOT NULL DEFAULT '[]'")
            contact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(contacts)")}
            if "profile_url" not in contact_columns:
                conn.execute("ALTER TABLE contacts ADD COLUMN profile_url TEXT NOT NULL DEFAULT ''")
            if "email" not in contact_columns:
                conn.execute("ALTER TABLE contacts ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            if not any(row["name"] == "message" for row in conn.execute("PRAGMA table_info(events)")):
                conn.execute("ALTER TABLE events ADD COLUMN message TEXT NOT NULL DEFAULT ''")
            if not conn.execute("SELECT 1 FROM settings WHERE key='experience_ranking_1'").fetchone():
                rows = conn.execute("SELECT id,title,department,description,region FROM jobs WHERE score=?", (EARLY_SCORE,)).fetchall()
                for row in rows:
                    score, reason = assess(row["title"], row["department"], row["description"], row["region"])
                    if 0 < score < EARLY_SCORE:
                        conn.execute("UPDATE jobs SET score=?,reason=? WHERE id=?", (score, reason, row["id"]))
                conn.execute("INSERT INTO settings VALUES('experience_ranking_1','1')")
            if not conn.execute("SELECT 1 FROM settings WHERE key='experience_ranking_2'").fetchone():
                rows = conn.execute("""SELECT j.id,j.title,j.department,j.description,j.region,j.score
                    FROM jobs j JOIN companies c ON c.id=j.company_id
                    WHERE j.score=? OR (j.score=0 AND (c.provider IN ('greenhouse','lever','ashby') OR (c.provider='' AND j.manual=1)))""",
                    (EARLY_SCORE,)).fetchall()
                for row in rows:
                    score, reason = assess(row["title"], row["department"], row["description"], row["region"])
                    if (row["score"] == 0 and score > 0) or (row["score"] == EARLY_SCORE and 0 < score < EARLY_SCORE):
                        conn.execute("UPDATE jobs SET score=?,reason=? WHERE id=?", (score, reason, row["id"]))
                conn.execute("INSERT INTO settings VALUES('experience_ranking_2','1')")
            if not conn.execute("SELECT 1 FROM settings WHERE key='senior_title_filter_1'").fetchone():
                rows = conn.execute("SELECT id,title FROM jobs WHERE score>0").fetchall()
                for row in rows:
                    if SENIOR.search(row["title"]):
                        conn.execute("UPDATE jobs SET score=0,reason='' WHERE id=?", (row["id"],))
                conn.execute("INSERT INTO settings VALUES('senior_title_filter_1','1')")
            if not conn.execute("SELECT 1 FROM settings WHERE key='experience_ranking_3'").fetchone():
                rows = conn.execute("SELECT id,title,department,description,region,score FROM jobs WHERE score>0 AND description<>''").fetchall()
                for row in rows:
                    score, reason = assess(row["title"], row["department"], row["description"], row["region"])
                    if score < row["score"]:
                        conn.execute("UPDATE jobs SET score=?,reason=? WHERE id=?", (score, reason, row["id"]))
                conn.execute("INSERT INTO settings VALUES('experience_ranking_3','1')")
            if not conn.execute("SELECT 1 FROM settings WHERE key='timezone_evidence_1'").fetchone():
                rows = conn.execute("SELECT id,evidence,description FROM jobs WHERE description<>''").fetchall()
                for row in rows:
                    timezone = required_timezone(row["description"])
                    if timezone and timezone.casefold() not in row["evidence"].casefold():
                        prefix = row["evidence"][:max(0, 497 - len(timezone))]
                        evidence = f"{prefix} · {timezone}" if prefix else timezone
                        conn.execute("UPDATE jobs SET evidence=? WHERE id=?", (evidence, row["id"]))
                conn.execute("INSERT INTO settings VALUES('timezone_evidence_1','1')")
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
            if seed and not conn.execute("SELECT 1 FROM settings WHERE key='sources_workday_1'").fetchone():
                with DATA.open(encoding="utf-8-sig", newline="") as handle:
                    conn.executemany(
                        "UPDATE companies SET provider=?,board=?,status='pending',note='' WHERE name=? AND url=? AND provider='' AND board=''",
                        ((row["provider"], row["board"], row["name"], row["url"])
                         for row in csv.DictReader(handle) if row["name"] in WORKDAY_SOURCES),
                    )
                conn.execute("INSERT INTO settings VALUES('sources_workday_1','1')")
            if seed and not conn.execute("SELECT 1 FROM settings WHERE key='sources_workday_2'").fetchone():
                with DATA.open(encoding="utf-8-sig", newline="") as handle:
                    conn.executemany(
                        "UPDATE companies SET provider=?,board=?,status='pending',note='' WHERE name=? AND url=? AND provider='' AND board=''",
                        ((row["provider"], row["board"], row["name"], row["url"])
                         for row in csv.DictReader(handle) if row["name"] in {"CrowdStrike", "Mastercard"}),
                    )
                conn.execute("INSERT INTO settings VALUES('sources_workday_2','1')")

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

    def graduation_year(self):
        value = self.setting("graduation_year")
        return int(value) if value.isdecimal() and 2000 <= int(value) <= 2100 else None

    def stats(self):
        stats = self.one("""
            SELECT
            (SELECT COUNT(*) FROM companies) AS companies,
            (SELECT COUNT(*) FROM companies WHERE enabled AND provider<>'') AS configured,
            (SELECT COUNT(*) FROM companies WHERE enabled AND provider<>'' AND status='failed') AS failed_feeds,
            (SELECT COUNT(*) FROM companies WHERE enabled AND provider<>'' AND status='pending') AS pending_feeds,
            (SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id=j.company_id
              WHERE j.archived=0 AND j.hidden=0 AND j.score>0 AND (c.enabled=1 OR j.manual=1)) AS matches,
            (SELECT COUNT(*) FROM jobs WHERE saved=1 OR applied_at IS NOT NULL) AS saved,
            (SELECT COUNT(*) FROM jobs WHERE hidden=1) AS hidden
        """)
        stats["due"] = self.one(f"SELECT count(*) AS count FROM ({OUTREACH_SELECT}) WHERE inbox_category IN ('delivery','schedule_issue','due')")["count"]
        return stats

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

    def jobs(self, *, view, region, search, latest, all_levels, offset, early_only=False, recent_days=0, date_basis="either"):
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
                date_field = {"either": "coalesce(julianday(j.posted_at),julianday(j.first_seen))",
                              "posted": "julianday(j.posted_at)", "seen": "julianday(j.first_seen)"}[date_basis]
                clauses.append(f"{date_field}>=julianday('now')-?")
                args.append(recent_days)
        if region:
            clauses.append("j.region=?")
            args.append(region)
        if search:
            clauses.append("instr(lower(j.title || ' ' || c.name || ' ' || j.department), lower(?))>0")
            args.append(search)
        where = " AND ".join(clauses)
        year = self.graduation_year()
        fresh_first = ("CASE WHEN j.new_scan=(SELECT MAX(id) FROM scans WHERE scope='full' AND state='completed') "
                       "THEN 0 ELSE 1 END, " if view not in {"saved", "hidden"} else "")
        year_order = "graduation_conflict(j.description), " if year and view not in {"saved", "hidden"} else ""
        order = """CASE j.region WHEN 'india_remote' THEN 0 WHEN 'india' THEN 0 WHEN 'us_remote' THEN 2
                   WHEN 'unclear' THEN 3 ELSE 4 END, c.status='failed', """ + year_order + fresh_first + """j.score DESC,
                   coalesce(julianday(j.posted_at),julianday(j.first_seen)) DESC, j.id DESC"""
        if view == "saved":
            order = "CASE WHEN j.applied_at IS NULL THEN j.application_target END ASC NULLS LAST, " + order
        with self.connect() as conn:
            if year_order:
                conn.create_function("graduation_conflict", 1, lambda text: int(graduation_warning(text, year) is not None))
            rows = [dict(row) for row in conn.execute(f"""SELECT j.id,j.title,j.location,j.url,j.apply_url,j.posted_at,j.first_seen,j.last_seen,j.new_scan,
                                   j.region,j.evidence,j.reason,j.score,j.saved,j.applied_at,j.archived,j.manual,j.hidden,
                                   j.deadline_at,j.application_target,j.description AS _description,
                                   c.name AS company,c.provider,c.status AS source_status
                            FROM jobs j JOIN companies c ON c.id=j.company_id WHERE {where}
                            ORDER BY {order} LIMIT 31 OFFSET ?""", (*args, offset)).fetchall()]
        for row in rows:
            row["graduation_warning"] = graduation_warning(row.pop("_description"), year)
        return rows

    def job(self, job_id):
        row = self.one("""SELECT j.*, c.name AS company, c.provider, c.status AS source_status
                          FROM jobs j JOIN companies c ON c.id=j.company_id WHERE j.id=?""", (job_id,))
        if row:
            row["eligibility"] = eligibility_evidence(row["description"])
            row["graduation_warning"] = graduation_warning(row["description"], self.graduation_year())
            row["work_authorization"] = work_authorization_evidence(row["description"])
            row["referrals"] = self.all("""SELECT o.id, o.contact_id, o.channel, o.state, o.due_at, c.name AS person
                FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE o.job_id=?
                ORDER BY o.state IN ('closed','submitted'), o.id DESC""", (job_id,))
        return row

    def inbox(self):
        return self.all(OUTREACH_SELECT + " ORDER BY CASE WHEN o.state='waiting' THEN o.due_at END ASC NULLS LAST, o.id DESC")

    def contact_conversations(self, contact_id, *, exclude_id=None, exclude_job_id=None):
        return self.all("""SELECT o.id, o.channel, o.state, o.sent_at, j.title AS job_title
            FROM outreach o JOIN jobs j ON j.id=o.job_id
            WHERE o.contact_id=? AND o.sent_at IS NOT NULL
              AND (? IS NULL OR o.id<>?) AND (? IS NULL OR o.job_id<>?)
            ORDER BY o.sent_at DESC, o.id DESC LIMIT 5""",
            (contact_id, exclude_id, exclude_id, exclude_job_id, exclude_job_id))

    def next_actions(self, today):
        applications = self.all("""SELECT j.id, j.title, j.application_target, j.archived, c.name AS company
            FROM jobs j JOIN companies c ON c.id=j.company_id
            WHERE j.saved=1 AND j.applied_at IS NULL AND j.application_target<=?
            ORDER BY j.application_target, j.id LIMIT 3""", (today,))
        referrals = self.all(f"""SELECT id,state,due_at,person,company,inbox_category FROM ({OUTREACH_SELECT})
            WHERE inbox_category IN ('delivery','schedule_issue','due','reply','offer','draft')
            ORDER BY CASE inbox_category WHEN 'delivery' THEN 0 WHEN 'schedule_issue' THEN 1 WHEN 'due' THEN 2
                     WHEN 'reply' THEN 3 WHEN 'offer' THEN 4 ELSE 5 END, due_at, id DESC LIMIT 3""")
        return {"applications": applications, "referrals": referrals}

    def outreach(self, item_id):
        row = self.one(OUTREACH_SELECT + " WHERE o.id=?", (item_id,))
        if row:
            row["events"] = self.all("SELECT kind,at,note,message FROM events WHERE outreach_id=? ORDER BY id DESC", (item_id,))
            row["prior_conversations"] = self.contact_conversations(row["contact_id"], exclude_id=item_id)
        return row


