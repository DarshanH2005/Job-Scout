"""Verified, compressed SQLite snapshots for a local Job Scout workspace.

Snapshots are taken with SQLite's online backup API, so they are consistent even
while the app is running. Each one is written to a temporary name, verified, then
gzipped and renamed into place, so a half-written file can never be mistaken for
a usable backup and an interrupted process cannot leave a corrupt snapshot.

Restore verifies the snapshot first and keeps the current database aside under a
"before-restore" name instead of destroying it.
"""

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

PREFIX = "scout-backup-"
SUFFIX = ".sqlite3"
GZIP_SUFFIX = ".sqlite3.gz"
DEFAULT_KEEP = 3
REQUIRED_TABLES = ("companies", "contacts", "jobs", "outreach", "scans", "settings")


def default_database():
    configured = os.getenv("SCOUT_DB")
    if configured:
        return Path(configured)
    return Path(os.getenv("LOCALAPPDATA") or Path.cwd()) / "JobScout" / "scout.sqlite3"


def default_directory(database):
    return Path(database).resolve().parent / "backups"


def _readonly(path):
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=20)


def _stamp(moment=None):
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _is_snapshot(path):
    return path.is_file() and path.name.startswith(PREFIX) and (
        path.name.endswith(GZIP_SUFFIX) or path.name.endswith(SUFFIX))


def _snapshots(directory):
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted((item for item in directory.iterdir() if _is_snapshot(item)),
                  key=lambda item: item.name, reverse=True)


def _remove(path):
    """Remove a database file together with any WAL sidecars SQLite left beside it."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


@contextmanager
def _readable(path):
    """Yield a path to an uncompressed SQLite file, expanding a .gz snapshot if needed."""
    path = Path(path)
    if not path.name.endswith(GZIP_SUFFIX):
        yield path
        return
    handle = tempfile.NamedTemporaryFile(suffix=SUFFIX, delete=False)
    handle.close()
    expanded = Path(handle.name)
    try:
        with gzip.open(path, "rb") as source, open(expanded, "wb") as destination:
            shutil.copyfileobj(source, destination)
    except (OSError, EOFError) as exc:
        expanded.unlink(missing_ok=True)
        raise ValueError(f"Backup cannot be decompressed: {exc}") from exc
    try:
        yield expanded
    finally:
        _remove(expanded)


def verify(path, thorough=False):
    """Raise ValueError unless the file is a readable Job Scout database; return its details."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Backup file not found: {path}")
    with _readable(path) as readable:
        if Path(readable).stat().st_size == 0:
            raise ValueError("Backup file is empty; the snapshot is incomplete.")
        try:
            connection = _readonly(readable)
        except sqlite3.Error as exc:
            raise ValueError(f"Backup cannot be opened: {exc}") from exc
        try:
            row = connection.execute("PRAGMA integrity_check" if thorough else "PRAGMA quick_check").fetchone()
            result = row[0] if row else "no result"
            if result != "ok":
                raise ValueError(f"Backup failed its integrity check: {result}")
            tables = {item[0] for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [name for name in REQUIRED_TABLES if name not in tables]
            if missing:
                raise ValueError("Backup is missing tables: " + ", ".join(missing))
            counts = {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in REQUIRED_TABLES}
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"Backup is not a readable Job Scout database: {exc}") from exc
        finally:
            connection.close()
    return {"path": str(path), "size": path.stat().st_size,
            "compressed": path.name.endswith(GZIP_SUFFIX), "counts": counts}


def recent(directory, limit=5):
    return [{"name": item.name, "size": item.stat().st_size,
             "modified_at": datetime.fromtimestamp(item.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
            for item in _snapshots(directory)[:limit]]


def _sweep(directory):
    """Delete sidecars and stale partial files left by an interrupted or older run."""
    directory = Path(directory)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    for item in directory.glob(f"{PREFIX}*"):
        if not item.is_file():
            continue
        if item.name.endswith(("-wal", "-shm")):
            base = item.with_name(item.name.rsplit("-", 1)[0])
            if not base.exists():
                item.unlink(missing_ok=True)
        elif item.name.endswith(".partial"):
            if datetime.fromtimestamp(item.stat().st_mtime, timezone.utc) < cutoff:
                item.unlink(missing_ok=True)


def prune(directory, keep=DEFAULT_KEEP):
    """Remove older snapshots, always leaving at least the newest one."""
    keep = max(1, int(keep))
    removed = []
    for item in _snapshots(directory)[keep:]:
        _remove(item)
        removed.append(item.name)
    _sweep(directory)
    return removed


def create(database, directory=None, keep=DEFAULT_KEEP, now=None):
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f"Database not found: {database}")
    directory = Path(directory) if directory else default_directory(database)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{PREFIX}{_stamp(now)}{SUFFIX}"
    target, partial = directory / f"{name}.gz", directory / f"{name}.partial"
    compressed_partial = directory / f"{target.name}.partial"
    for leftover in (partial, compressed_partial):
        leftover.unlink(missing_ok=True)
    source = _readonly(database)
    try:
        destination = sqlite3.connect(partial)
        try:
            source.backup(destination)
            # A snapshot must be one portable file, not a database plus WAL sidecars.
            destination.execute("PRAGMA journal_mode=DELETE")
        finally:
            destination.close()
    finally:
        source.close()
    try:
        details = verify(partial)
        with open(partial, "rb") as raw, gzip.open(compressed_partial, "wb", compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed)
        os.replace(compressed_partial, target)
    except Exception:
        _remove(partial)
        compressed_partial.unlink(missing_ok=True)
        raise
    _remove(partial)
    _sweep(directory)
    return {**details, "path": str(target), "name": target.name, "size": target.stat().st_size,
            "compressed": True, "created_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "pruned": prune(directory, keep)}


def restore(backup_path, database, keep_safety=True):
    """Replace the live database with a verified snapshot. Job Scout must not be running."""
    backup_path, database = Path(backup_path), Path(database)
    details = verify(backup_path, thorough=True)
    if database.is_file():
        try:
            probe = sqlite3.connect(database, timeout=1)
            try:
                probe.execute("BEGIN EXCLUSIVE")
                probe.rollback()
            finally:
                probe.close()
        except sqlite3.OperationalError as exc:
            raise ValueError("Close Job Scout before restoring: its database is still in use.") from exc
    safety = None
    if database.is_file() and keep_safety:
        # Kept in its own folder: rotation must never delete the copy taken just before a restore.
        safety_directory = default_directory(database) / "safety"
        safety_directory.mkdir(parents=True, exist_ok=True)
        safety = safety_directory / f"{PREFIX}before-restore-{_stamp()}{SUFFIX}"
        source = _readonly(database)
        try:
            destination = sqlite3.connect(safety)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    # A log left behind by an interrupted session must not be replayed into the restored file.
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    staging = database.with_name(database.name + ".restore")
    with _readable(backup_path) as readable:
        shutil.copyfile(readable, staging)
    os.replace(staging, database)
    verify(database, thorough=True)
    return {"restored_from": str(backup_path), "database": str(database),
            "safety_copy": str(safety) if safety else None, "counts": details["counts"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m scout.backup",
                                     description="Back up, verify or restore the local Job Scout database.")
    parser.add_argument("--database", type=Path, default=default_database())
    parser.add_argument("--directory", type=Path, default=None)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--restore", metavar="FILE|latest")
    args = parser.parse_args(argv)
    directory = args.directory or default_directory(args.database)
    try:
        if args.list:
            items = recent(directory, limit=50)
            for item in items:
                print(f"{item['name']}  {item['size']:,} bytes  {item['modified_at']}")
            if not items:
                print(f"No backups in {directory}")
            return 0
        if args.verify:
            print(json.dumps(verify(args.verify, thorough=True), indent=2))
            return 0
        if args.restore:
            source = args.restore
            if source == "latest":
                items = _snapshots(directory)
                if not items:
                    raise ValueError(f"No backups in {directory}")
                source = str(items[0])
            info = restore(Path(source), args.database)
            print(f"Restored {info['restored_from']} into {info['database']}")
            if info["safety_copy"]:
                print(f"Previous database kept at {info['safety_copy']}")
            return 0
        info = create(args.database, directory, args.keep)
        print(f"Backed up to {info['path']} ({info['size']:,} bytes, jobs={info['counts']['jobs']})")
        if info["pruned"]:
            print("Removed older backups: " + ", ".join(info["pruned"]))
        return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
