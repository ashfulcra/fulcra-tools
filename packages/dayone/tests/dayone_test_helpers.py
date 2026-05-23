"""Non-fixture test helpers shared across fulcra-dayone test modules."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Core Data epoch — seconds since 2001-01-01 UTC.
_CD_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _cd_seconds(dt: datetime) -> float:
    return (dt - _CD_EPOCH).total_seconds()


def build_dayone_db(path: Path) -> None:
    """Build a minimal Day One Core Data SQLite database for tests.

    Entity numbers: Entry=17, Tag=66 (mirroring a real Day One store);
    the entry<->tag join is therefore Z_17TAGS.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT, Z_MAX INTEGER);
        CREATE TABLE ZJOURNAL (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT);
        CREATE TABLE ZTAG (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT);
        CREATE TABLE ZLOCATION (
            Z_PK INTEGER PRIMARY KEY, ZPLACENAME TEXT, ZLOCALITYNAME TEXT,
            ZADMINISTRATIVEAREA TEXT, ZCOUNTRY TEXT);
        CREATE TABLE ZENTRY (
            Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZCREATIONDATE REAL,
            ZMARKDOWNTEXT TEXT, ZSTARRED INTEGER, ZJOURNAL INTEGER,
            ZLOCATION INTEGER);
        CREATE TABLE ZATTACHMENT (ZENTRY INTEGER, ZTYPE INTEGER);
        CREATE TABLE Z_17TAGS (Z_17ENTRIES INTEGER, Z_66TAGS1 INTEGER);
        """
    )
    conn.executemany(
        "INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_MAX) VALUES (?, ?, ?)",
        [(17, "Entry", 2), (66, "Tag", 2), (27, "Journal", 1)],
    )
    conn.executemany(
        "INSERT INTO ZJOURNAL (Z_PK, ZNAME) VALUES (?, ?)",
        [(1, "Personal"), (2, "Travel")],
    )
    conn.executemany(
        "INSERT INTO ZTAG (Z_PK, ZNAME) VALUES (?, ?)",
        [(1, "work"), (2, "travel")],
    )
    conn.execute(
        "INSERT INTO ZLOCATION (Z_PK, ZPLACENAME, ZLOCALITYNAME, "
        "ZADMINISTRATIVEAREA, ZCOUNTRY) VALUES (1, 'Cafe', 'Seattle', 'WA', 'USA')"
    )
    d1 = _cd_seconds(datetime(2024, 1, 15, 9, 30, tzinfo=timezone.utc))
    d2 = _cd_seconds(datetime(2024, 2, 20, 14, 0, tzinfo=timezone.utc))
    d3 = _cd_seconds(datetime(2024, 3, 1, 8, 0, tzinfo=timezone.utc))
    conn.executemany(
        "INSERT INTO ZENTRY (Z_PK, ZUUID, ZCREATIONDATE, ZMARKDOWNTEXT, "
        "ZSTARRED, ZJOURNAL, ZLOCATION) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "AAA111", d1, "First entry body", 1, 1, 1),
            (2, "BBB222", d2, "Second entry", 0, 2, None),
            (3, "CCC333", d3, None, 0, 1, None),  # empty text -> skipped
        ],
    )
    conn.executemany(
        "INSERT INTO ZATTACHMENT (ZENTRY, ZTYPE) VALUES (?, ?)",
        [(1, 1), (1, 1)],  # entry 1 has 2 attachments
    )
    conn.executemany(
        "INSERT INTO Z_17TAGS (Z_17ENTRIES, Z_66TAGS1) VALUES (?, ?)",
        [(1, 1), (1, 2), (2, 2)],  # entry1: work+travel, entry2: travel
    )
    conn.commit()
    conn.close()
