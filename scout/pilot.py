"""Build a triage sheet for a five-job pilot from the local database.

The sheet answers one question: which roles are plausibly workable right now, and
what evidence does each one carry? It ranks India-eligible jobs, quotes the
employer's own requirements, and leaves decision columns empty for the user.

Read-only: nothing in the database is written. Files are written only where the
caller asks.
"""

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .backup import default_database
from .feeds import eligibility_evidence, graduation_warning

COLUMNS = ("review_priority", "job_id", "company", "title", "region", "location", "score",
           "early_career_title", "experience_stated", "eligibility_evidence", "graduation_check",
           "posted_days_ago", "first_seen_days_ago", "already_saved", "apply_url", "linkedin_people_search",
           "decision", "reject_reason", "chosen_next_step", "contact_name", "contact_link", "channel",
           "message_sent_at", "reply_at", "applied_at", "notes")
LEDGER_COLUMNS = ("slot", "job_id", "company", "title", "apply_url", "contact_name", "contact_link", "channel",
                  "message_sent_at", "reply_at", "follow_up_due", "applied_at", "referral_offered", "outcome", "notes")

YEARS = re.compile(r"\b\d{1,2}\s*(?:\+\s*|to\s*\d{1,2}\s*)?(?:years?|yrs?)\b", re.I)
ENTRY = re.compile(r"\b(new grad|intern|graduate|entry[- ]level|junior|software engineer (?:i|1)\b|scientist i\b|analyst i\b)", re.I)
INDIA = ("india", "india_remote")


def _readonly(path):
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=20)


def _days_since(value):
    if not value:
        return ""
    try:
        then = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - then).total_seconds() / 86400)


def _rank(job):
    early = 0 if ENTRY.search(job["title"]) else 1
    recent = _days_since(job["posted_at"] or job["first_seen"])
    return (0 if job["score"] >= 90 else 1, early, 999 if recent == "" else recent)


def _experience(description):
    """Quote the text around each years-of-experience mention, requirement sentences first."""
    windows = []
    for match in YEARS.finditer(description):
        start, end = max(0, match.start() - 100), min(len(description), match.end() + 120)
        window = " ".join(description[start:end].split())
        windows.append((0 if ("experience" in window.casefold() or "require" in window.casefold()) else 1, window))
    windows.sort(key=lambda item: item[0])
    return " | ".join(window for _, window in windows[:2])


def candidates(database):
    """Return the current India-eligible pool, distinct by company and title, best first."""
    connection = _readonly(database)
    connection.row_factory = sqlite3.Row
    try:
        feed_total = connection.execute("""SELECT count(*) FROM jobs j JOIN companies c ON c.id=j.company_id
            WHERE j.score>0 AND j.archived=0 AND j.hidden=0 AND c.enabled=1""").fetchone()[0]
        rows = [dict(row) for row in connection.execute("""
            SELECT j.id, j.title, c.name AS company, j.region, j.location, j.score, j.posted_at, j.first_seen,
                   j.url, j.apply_url, j.description, j.saved
            FROM jobs j JOIN companies c ON c.id=j.company_id
            WHERE j.score>0 AND j.archived=0 AND j.hidden=0 AND c.enabled=1
              AND j.region IN ('india','india_remote')
            ORDER BY j.score DESC, coalesce(julianday(j.posted_at), julianday(j.first_seen)) DESC""").fetchall()]
    finally:
        connection.close()
    seen, kept = set(), []
    for job in rows:
        key = (job["company"].casefold(), job["title"].casefold().strip())
        if key in seen:
            continue
        seen.add(key)
        kept.append(job)
    return feed_total, sorted(kept, key=_rank)


def build(database, review=20, total=25):
    feed_total, pool = candidates(database)
    chosen = pool[:total]
    rows = []
    for index, job in enumerate(chosen, 1):
        description = job["description"] or ""
        evidence = "; ".join(f'{item["label"]}: {item["text"]}' for item in (eligibility_evidence(description) or []))
        warning = graduation_warning(description, 2027)
        rows.append({
            "review_priority": index if index <= review else f"{index} (lower priority)",
            "job_id": job["id"], "company": job["company"], "title": job["title"], "region": job["region"],
            "location": job["location"], "score": job["score"],
            "early_career_title": "yes" if ENTRY.search(job["title"]) else "",
            "experience_stated": _experience(description)[:400],
            "eligibility_evidence": evidence[:400],
            "graduation_check": f"posting says {warning['excerpt']}" if warning else "",
            "posted_days_ago": _days_since(job["posted_at"]),
            "first_seen_days_ago": _days_since(job["first_seen"]),
            "already_saved": "saved" if job["saved"] else "",
            "apply_url": job["apply_url"] or job["url"],
            "linkedin_people_search": "https://www.linkedin.com/search/results/people/?keywords=" + job["company"].replace(" ", "+"),
            "decision": "", "reject_reason": "", "chosen_next_step": "", "contact_name": "", "contact_link": "",
            "channel": "", "message_sent_at": "", "reply_at": "", "applied_at": "", "notes": "",
        })
    summary = {
        "feed_total": feed_total,
        "india_eligible": len(pool),
        "reviewed": min(review, len(rows)),
        "explicit_early_career": sum(1 for job in chosen if job["score"] >= 90),
        "entry_level_titles": sum(1 for job in chosen if ENTRY.search(job["title"])),
        "years_stated": sum(1 for row in rows if row["experience_stated"]),
        "already_saved": sum(1 for row in rows if row["already_saved"]),
    }
    return {"rows": rows, "summary": summary}


def write(directory, database=None, review=20, total=25, force=False):
    report = build(database or default_database(), review=review, total=total)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    shortlist = directory / "pilot-shortlist.csv"
    ledger = directory / "pilot-ledger.csv"
    existing = [str(path) for path in (shortlist, ledger) if path.exists()]
    if existing and not force:
        raise FileExistsError("Refusing to overwrite filled-in pilot files: " + ", ".join(existing)
                              + ". Copy them first, then pass --force.")
    with shortlist.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        writer.writerows(report["rows"])
    with ledger.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(LEDGER_COLUMNS))
        writer.writeheader()
        writer.writerows([{"slot": slot} for slot in range(1, 6)])
    return {"shortlist": str(shortlist), "ledger": str(ledger), "summary": report["summary"]}


def render(summary):
    return "\n".join([
        "Five-job pilot sheet",
        f"  jobs in feed            {summary['feed_total']:>6,}",
        f"  India-eligible roles    {summary['india_eligible']:>6,}   distinct by company and title",
        f"  rows written            {summary['reviewed']:>6,}   plus lower-priority roles",
        f"  explicit early career   {summary['explicit_early_career']:>6,}   score 90",
        f"  entry-level titles      {summary['entry_level_titles']:>6,}   including 'Software Engineer 1'",
        f"  posting states years    {summary['years_stated']:>6,}   the rest need a detail check",
        f"  already saved           {summary['already_saved']:>6,}",
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m scout.pilot",
                                     description="Write a triage sheet for a five-job pilot (read-only on the database).")
    parser.add_argument("--database", type=Path, default=default_database())
    parser.add_argument("--out", type=Path, required=True, help="Directory for pilot-shortlist.csv and pilot-ledger.csv")
    parser.add_argument("--review", type=int, default=20, help="How many rows to mark for review")
    parser.add_argument("--total", type=int, default=25, help="How many rows to write")
    parser.add_argument("--force", action="store_true", help="Overwrite existing pilot files")
    args = parser.parse_args(argv)
    try:
        result = write(args.out, args.database, review=args.review, total=args.total, force=args.force)
    except (FileNotFoundError, FileExistsError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(render(result["summary"]))
    print(f"\nWrote {result['shortlist']}")
    print(f"Wrote {result['ledger']}")
    print("Nothing was deleted or changed. Review rows, then fill the decision columns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
