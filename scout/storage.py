"""Read-only storage analysis for the local Job Scout database.

This module never writes, deletes or vacuums. It reports where the space goes and
how many rows a retention policy *would* affect, so any future pruning can be
decided from numbers instead of guesses.

The dominant cost is job descriptions: every posting any watched employer
publishes is stored with its full description, including postings the relevance
rules filter out. Rows are kept deliberately, because re-running the rules needs
the descriptions; a retention policy can still drop the text of filtered-out
postings that are no longer seen, and only then run VACUUM to reclaim the file.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .backup import default_database

TABLES = ("jobs", "companies", "scans", "contacts", "outreach", "events", "mail_seen", "settings", "posting_checks")


def _protected(connection):
    """Rows a retention policy must never touch, whatever their score."""
    known = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    clause = """j.saved=0 AND j.applied_at IS NULL AND j.hidden=0
                AND NOT EXISTS (SELECT 1 FROM outreach o WHERE o.job_id=j.id)"""
    if "posting_checks" in known:
        clause += "\n               AND NOT EXISTS (SELECT 1 FROM posting_checks p WHERE p.job_id=j.id)"
    return clause


def _readonly(path):
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=20)


def _scalar(connection, sql, params=()):
    row = connection.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else 0


def analyze(database, days=30, delete_days=90):
    """Return a storage report. Raises FileNotFoundError when the database is missing."""
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f"Database not found: {database}")
    connection = _readonly(database)
    connection.row_factory = sqlite3.Row
    try:
        page_size = _scalar(connection, "PRAGMA page_size")
        page_count = _scalar(connection, "PRAGMA page_count")
        freelist = _scalar(connection, "PRAGMA freelist_count")
        tables = {}
        for name in TABLES:
            try:
                tables[name] = _scalar(connection, f"SELECT count(*) FROM {name}")
            except sqlite3.OperationalError:
                continue
        total = tables.get("jobs", 0)
        description_bytes = _scalar(connection, "SELECT COALESCE(SUM(LENGTH(description)),0) FROM jobs")
        matching = _scalar(connection, "SELECT count(*) FROM jobs WHERE score>0")
        filtered_bytes = _scalar(connection, "SELECT COALESCE(SUM(LENGTH(description)),0) FROM jobs WHERE score=0")
        in_feed = _scalar(connection, """SELECT count(*) FROM jobs j JOIN companies c ON c.id=j.company_id
            WHERE j.score>0 AND j.archived=0 AND j.hidden=0 AND c.enabled=1""")

        clear = connection.execute(f"""SELECT count(*) AS rows, COALESCE(SUM(LENGTH(j.description)),0) AS bytes
            FROM jobs j WHERE j.score=0 AND {_protected(connection)}
              AND julianday('now')-julianday(j.last_seen)>=?""", (days,)).fetchone()
        delete = connection.execute(f"""SELECT count(*) AS rows, COALESCE(SUM(LENGTH(j.description)),0) AS bytes
            FROM jobs j WHERE j.score=0 AND {_protected(connection)}
              AND julianday('now')-julianday(j.last_seen)>=?""", (delete_days,)).fetchone()
        saved_or_applied = _scalar(connection, "SELECT count(*) FROM jobs WHERE saved=1 OR applied_at IS NOT NULL")

        first_scan = _scalar(connection, "SELECT min(date(started_at)) FROM scans") or ""
        series = [dict(row) for row in connection.execute("""SELECT date(first_seen) AS day, count(*) AS rows
            FROM jobs WHERE julianday('now')-julianday(first_seen) <= 7 GROUP BY day ORDER BY day""")]
        # The first successful scan imports a whole watchlist; it is not a daily rate.
        recent = [item["rows"] for item in series if item["day"] > first_scan][-3:]
        daily = round(sum(recent) / len(recent)) if recent else 0
    finally:
        connection.close()

    file_bytes = page_size * page_count
    per_job = file_bytes / total if total else 0
    return {
        "database": str(database),
        "file_bytes": file_bytes,
        "freelist_bytes": freelist * page_size,
        "tables": tables,
        "jobs": {
            "total": total,
            "matching": matching,
            "filtered": total - matching,
            "in_feed": in_feed,
            "saved_or_applied": saved_or_applied,
            "description_bytes": description_bytes,
            "filtered_description_bytes": filtered_bytes,
            "average_description_bytes": round(description_bytes / total) if total else 0,
            "bytes_per_job": round(per_job),
        },
        "retention": {
            "days": days,
            "delete_days": delete_days,
            "clear_description": {"rows": clear["rows"], "description_bytes": clear["bytes"]},
            "delete_rows": {"rows": delete["rows"], "description_bytes": delete["bytes"]},
        },
        "growth": {
            "daily_rows": series,
            "recent_rows_per_day": daily,
            "megabytes_per_day": round(daily * per_job / 1e6, 2),
            "gigabytes_per_year": round(daily * per_job * 365 / 1e9, 2),
        },
    }


def render(report):
    jobs = report["jobs"]
    growth = report["growth"]
    retention = report["retention"]
    lines = [
        f"Job Scout storage report - {report['database']}",
        f"  file            {report['file_bytes']/1e6:8.1f} MB   free {report['freelist_bytes']/1e6:.1f} MB",
        f"  jobs            {jobs['total']:8,}   matching {jobs['matching']:,}, filtered out {jobs['filtered']:,}",
        f"  in current feed {jobs['in_feed']:8,}",
        f"  descriptions    {jobs['description_bytes']/1e6:8.1f} MB   (average {jobs['average_description_bytes']:,} bytes per job)",
        f"  filtered text   {jobs['filtered_description_bytes']/1e6:8.1f} MB   of that total",
        f"  bytes per job   {jobs['bytes_per_job']:8,}   including row overhead",
        f"  new rows/day    {growth['recent_rows_per_day']:8,}   last three days",
        f"  growth estimate {growth['megabytes_per_day']:8.2f} MB/day, about {growth['gigabytes_per_year']:.2f} GB/year",
        "",
        f"  retention candidates (filtered out, unsaved, unreferenced, not seen in {retention['days']} days)",
        f"    clear descriptions {retention['clear_description']['rows']:>7,} rows  "
        f"{retention['clear_description']['description_bytes']/1e6:.1f} MB",
        f"    delete rows        {retention['delete_rows']['rows']:>7,} rows  "
        f"(threshold {retention['delete_days']} days)",
        "",
        "  Nothing was changed. Run VACUUM only after an approved prune to reclaim the file.",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m scout.storage",
                                     description="Report where the local Job Scout database spends its space (read-only).")
    parser.add_argument("--database", type=Path, default=default_database())
    parser.add_argument("--days", type=int, default=30, help="Age before filtered descriptions become eligible")
    parser.add_argument("--delete-days", type=int, default=90, help="Age before filtered rows become eligible")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args(argv)
    try:
        report = analyze(args.database, days=args.days, delete_days=args.delete_days)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
