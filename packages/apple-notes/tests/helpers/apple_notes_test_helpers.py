"""Synthetic Apple Notes store + protobuf builder for tests.

Built to reproduce the awkward shapes measured on the real store, not a
tidy ideal: husk note rows with no body, attachments with no backing file,
and per-entity column reuse (ZMODIFICATIONDATE vs ZMODIFICATIONDATE1).
"""
from __future__ import annotations

import gzip
import sqlite3
import struct
from pathlib import Path

import pytest


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def pb_varint(field: int, value: int) -> bytes:
    return tag(field, 0) + varint(value)


def pb_bytes(field: int, value: bytes) -> bytes:
    return tag(field, 2) + varint(len(value)) + value


def pb_str(field: int, value: str) -> bytes:
    return pb_bytes(field, value.encode("utf-8"))


def make_run(length: int, *, style: int | None = None, indent: int = 0,
             checked: bool | None = None, link: str = "",
             attachment_id: str = "", type_uti: str = "") -> bytes:
    run = pb_varint(1, length)
    if style is not None or indent or checked is not None:
        para = b""
        if style is not None:
            para += pb_varint(1, style)
        if indent:
            para += pb_varint(4, indent)
        if checked is not None:
            para += pb_bytes(5, pb_bytes(1, b"uuid") + pb_varint(2, 1 if checked else 0))
        run += pb_bytes(2, para)
    if link:
        run += pb_str(9, link)
    if attachment_id:
        run += pb_bytes(12, pb_str(1, attachment_id) + pb_str(2, type_uti))
    return run


def make_body(text: str, runs: list[bytes], *, gzipped: bool = True) -> bytes:
    note = pb_str(2, text)
    for run in runs:
        note += pb_bytes(5, run)
    document = pb_varint(2, 1) + pb_bytes(3, note)
    store = pb_bytes(2, document)
    return gzip.compress(store) if gzipped else store


SCHEMA_COLUMNS = [
    "ZIDENTIFIER TEXT", "ZTITLE TEXT", "ZTITLE1 TEXT", "ZTITLE2 TEXT",
    "ZMODIFICATIONDATE REAL", "ZMODIFICATIONDATE1 REAL",
    "ZCREATIONDATE REAL", "ZCREATIONDATE1 REAL", "ZCREATIONDATE3 REAL",
    "ZMARKEDFORDELETION INTEGER", "ZFOLDER INTEGER", "ZNOTEDATA INTEGER",
    "ZNOTE INTEGER", "ZMEDIA INTEGER", "ZFILENAME TEXT", "ZTYPEUTI TEXT",
]


@pytest.fixture
def notes_db(tmp_path: Path) -> Path:
    """A container dir holding a NoteStore.sqlite shaped like the real one."""
    container = tmp_path / "group.com.apple.notes"
    container.mkdir()
    db = container / "NoteStore.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "create table ZICCLOUDSYNCINGOBJECT ("
        "Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, " + ", ".join(SCHEMA_COLUMNS) + ")")
    conn.execute("create table ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZDATA BLOB)")
    conn.execute("create table Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT)")
    for ent, name in ((5, "ICAttachment"), (11, "ICMedia"), (12, "ICNote"),
                      (15, "ICFolder")):
        conn.execute("insert into Z_PRIMARYKEY values (?,?)", (ent, name))

    # Folder
    conn.execute(
        "insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZTITLE2) "
        "values (1, 15, 'folder-uuid', 'Recipes')")

    # A real note with a body.
    body = make_body(
        "Soup\nBoil water\nAdd salt\n￼",
        [make_run(5, style=0),
         make_run(11, style=103, checked=True),
         make_run(9, style=103, checked=False),
         make_run(1, attachment_id="att-1", type_uti="public.png")])
    conn.execute("insert into ZICNOTEDATA values (10, ?)", (body,))
    conn.execute(
        "insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZTITLE1, "
        "ZFOLDER, ZNOTEDATA, ZMODIFICATIONDATE1, ZCREATIONDATE3, ZMARKEDFORDELETION) "
        "values (2, 12, 'note-uuid-1111', 'Soup', 1, 10, 700000000.0, 690000000.0, 0)")

    # A husk note: row exists, ZDATA is NULL, no title/folder/moddate,
    # NOT marked deleted.
    conn.execute("insert into ZICNOTEDATA values (11, NULL)")
    conn.execute(
        "insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZNOTEDATA, "
        "ZMARKEDFORDELETION) values (3, 12, 'husk-uuid-2222', 11, 0)")

    # Media row + attachment WITH a file.
    conn.execute(
        "insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZFILENAME) "
        "values (4, 11, 'media-uuid-1', 'photo.png')")
    conn.execute(
        "insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZNOTE, "
        "ZMEDIA, ZTYPEUTI, ZMODIFICATIONDATE) "
        "values (5, 5, 'att-1', 2, 4, 'public.png', 700000000.0)")

    # Attachment with NO media row (inline table / drawing).
    conn.execute(
        "insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZNOTE, "
        "ZTYPEUTI, ZMODIFICATIONDATE) "
        "values (6, 5, 'att-2', 2, 'com.apple.notes.table', 700000000.0)")
    # A second real note, so a partial pass can leave one unreached and a
    # deletion sweep has something it could wrongly mark.
    body2 = make_body("Second\nline", [make_run(7, style=0), make_run(4)])
    conn.execute("insert into ZICNOTEDATA values (12, ?)", (body2,))
    conn.execute(
        "insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZTITLE1, "
        "ZFOLDER, ZNOTEDATA, ZMODIFICATIONDATE1, ZCREATIONDATE3, ZMARKEDFORDELETION) "
        "values (7, 12, 'note-uuid-3333', 'Second', 1, 12, 700000100.0, 690000100.0, 0)")

    conn.commit()
    conn.close()

    media_root = container / "Accounts" / "LocalAccount" / "Media"
    media_dir = media_root / "media-uuid-1" / "inner"
    media_dir.mkdir(parents=True)
    (media_dir / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    # Reproductions of the two failures the first production sync hit.
    # 1. A scanned "paper bundle": the recorded filename is a DIRECTORY of
    #    page images plus a rendered PDF.
    bundle = media_root / "media-bundle" / "3_inner" / "Sample Scan"
    bundle.mkdir(parents=True)
    (bundle / "page1.jpeg").write_bytes(b"\xff\xd8" + b"a" * 100)
    (bundle / "page2.jpeg").write_bytes(b"\xff\xd8" + b"b" * 300)
    (bundle / "doc.pdf").write_bytes(b"%PDF-1.4" + b"c" * 50)

    # 2. A zero-byte attachment: the upload CLI sniffs content and fails.
    empty_dir = media_root / "media-empty" / "inner"
    empty_dir.mkdir(parents=True)
    (empty_dir / "Image.jpeg").write_bytes(b"")

    # 3. A bundle with no PDF at all.
    nopdf = media_root / "media-nopdf" / "inner" / "Scan"
    nopdf.mkdir(parents=True)
    (nopdf / "small.jpeg").write_bytes(b"\xff\xd8" + b"s" * 10)
    (nopdf / "big.jpeg").write_bytes(b"\xff\xd8" + b"B" * 500)
    return container
