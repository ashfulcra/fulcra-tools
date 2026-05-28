"""Read Day One's local Core Data SQLite database into DayOneEntry[]."""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..entry import DayOneEntry

# Whitelist for SQLite identifiers we interpolate into queries. Day One's
# schema-discovery code finds the entry/tag join table by name and column;
# both come from the on-disk database via sqlite_master / PRAGMA, never
# from HTTP input. If an attacker has write access to the user's Day One
# container they have far worse options — but we still enforce a strict
# `[A-Z_0-9]+` shape so a malformed table name (e.g. one containing
# whitespace, quotes, or semicolons) is rejected before it can land in
# an f-string SQL query.
_DAYONE_IDENT_RE = re.compile(r"^[A-Z_0-9]+$")


def _safe_ident(name: str) -> str:
    """Validate `name` matches the Day One schema identifier shape.
    Raises ValueError if not — caller turns that into the SCHEMA_ERROR
    surface so the user sees a clear message instead of a SQL error."""
    if not _DAYONE_IDENT_RE.match(name):
        raise ValueError(
            f"Day One schema identifier rejected (got {name!r})"
        )
    return name

# Core Data stores timestamps as float seconds since 2001-01-01 UTC.
_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_DB_GLOB = "Library/Group Containers/*.dayoneapp2/Data/Documents/DayOne.sqlite"
_SCHEMA_ERROR = "Day One database schema not recognized — use the JSON export instead"


def find_database() -> Path:
    """Locate the Day One SQLite database under the user's home directory."""
    matches = sorted(Path.home().glob(_DB_GLOB))
    if not matches:
        raise FileNotFoundError(
            f"no Day One database found at ~/{_DB_GLOB}; "
            "pass --db-path or use the JSON export"
        )
    return matches[0]


def _snapshot(db: Path) -> Path:
    """Copy the DB to a temp file (APFS clone when possible) so the live
    database is never opened directly."""
    dest = Path(tempfile.mkdtemp()) / "dayone-snapshot.sqlite"
    try:
        subprocess.run(
            ["cp", "-c", str(db), str(dest)], check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        shutil.copy2(db, dest)
    return dest


def _entity_number(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = ?", (name,),
    ).fetchone()
    if row is None:
        raise ValueError(f"{_SCHEMA_ERROR} (no '{name}' entity)")
    return int(row[0])


def _find_tag_join(conn: sqlite3.Connection, entry_ent: int) -> tuple[str, str, str]:
    """Return (join_table, entry_column, tag_column) for the entry<->tag
    many-to-many relation, discovered from the schema."""
    entry_col = f"Z_{entry_ent}ENTRIES"
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'Z\\_%TAGS' ESCAPE '\\'"
    ).fetchall()
    for (table,) in rows:
        # `table` came from sqlite_master so SQLite already validated it
        # as a real table name, but we still strict-check against
        # _DAYONE_IDENT_RE before letting it land in an f-string. A
        # malformed table name (whitespace, quote, semicolon) raises
        # ValueError; SCHEMA_ERROR surface translates that for the user.
        _safe_ident(table)
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info('{table}')")]
        if entry_col in cols and len(cols) == 2:
            tag_col = cols[0] if cols[1] == entry_col else cols[1]
            _safe_ident(tag_col)
            return table, entry_col, tag_col
    raise ValueError(f"{_SCHEMA_ERROR} (no entry/tag join table)")


def read_local_db(db_path: Path | None = None) -> list[DayOneEntry]:
    """Read entries from the local Day One database. With no `db_path`,
    locate it automatically."""
    src = db_path or find_database()
    if not src.exists():
        raise FileNotFoundError(f"Day One database not found: {src}")
    snapshot = _snapshot(src)
    try:
        conn = sqlite3.connect(snapshot)
        conn.row_factory = sqlite3.Row
        try:
            return _read(conn)
        except sqlite3.OperationalError as exc:
            raise ValueError(f"{_SCHEMA_ERROR} ({exc})") from exc
        finally:
            conn.close()
    finally:
        shutil.rmtree(snapshot.parent, ignore_errors=True)


def _read(conn: sqlite3.Connection) -> list[DayOneEntry]:
    entry_ent = _entity_number(conn, "Entry")
    join_table, entry_col, tag_col = _find_tag_join(conn, entry_ent)

    journals = {
        r["Z_PK"]: r["ZNAME"]
        for r in conn.execute("SELECT Z_PK, ZNAME FROM ZJOURNAL")
    }
    tag_names = {
        r["Z_PK"]: r["ZNAME"]
        for r in conn.execute("SELECT Z_PK, ZNAME FROM ZTAG")
    }
    locations = {
        r["Z_PK"]: (
            r["ZPLACENAME"] or r["ZLOCALITYNAME"]
            or r["ZADMINISTRATIVEAREA"] or r["ZCOUNTRY"]
        )
        for r in conn.execute(
            "SELECT Z_PK, ZPLACENAME, ZLOCALITYNAME, ZADMINISTRATIVEAREA, "
            "ZCOUNTRY FROM ZLOCATION"
        )
    }
    tags_by_entry: dict[int, list[str]] = {}
    for r in conn.execute(
        f"SELECT {entry_col} AS e, {tag_col} AS t FROM {join_table}"
    ):
        name = tag_names.get(r["t"])
        if name:
            tags_by_entry.setdefault(r["e"], []).append(name)
    photos_by_entry = {
        r["ZENTRY"]: r["n"]
        for r in conn.execute(
            "SELECT ZENTRY, COUNT(*) AS n FROM ZATTACHMENT "
            "WHERE ZENTRY IS NOT NULL GROUP BY ZENTRY"
        )
    }

    out: list[DayOneEntry] = []
    skipped = 0
    for r in conn.execute(
        "SELECT Z_PK, ZUUID, ZCREATIONDATE, ZMARKDOWNTEXT, ZSTARRED, "
        "ZJOURNAL, ZLOCATION FROM ZENTRY"
    ):
        text = r["ZMARKDOWNTEXT"]
        if not text or not r["ZUUID"] or r["ZCREATIONDATE"] is None:
            skipped += 1
            continue
        created = _CORE_DATA_EPOCH + timedelta(seconds=float(r["ZCREATIONDATE"]))
        out.append(DayOneEntry(
            uuid=r["ZUUID"],
            creation_date=created,
            text=text,
            tags=tuple(sorted(tags_by_entry.get(r["Z_PK"], []))),
            starred=bool(r["ZSTARRED"]),
            journal=journals.get(r["ZJOURNAL"], "(unknown)"),
            location=locations.get(r["ZLOCATION"]),
            photo_count=photos_by_entry.get(r["Z_PK"], 0),
            word_count=len(text.split()),
        ))
    if skipped:
        print(f"local_db: skipped {skipped} entries with no readable text",
              file=sys.stderr)
    return out
