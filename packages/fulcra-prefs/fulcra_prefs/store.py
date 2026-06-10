"""The ONLY module that talks to Fulcra. Files via the fulcra_api library
(list/resolve/download/upload); record writes via the generic request method
to /ingest/v1/record — the library has no record-write helper yet (see
FULCRA-PRIMITIVES.md); switch to CLI/lib annotation commands when they land.

Real-library shape notes (fulcra_api.core.FulcraAPI, verified v0.1.30,
file-commands branch):

  resolve_filepath(filepath) -> dict
    Returns ONE dict (the file record) when found; raises
    Exception("File not found in Fulcra Library: <filepath>") when absent.
    Does NOT return a list — read_json does match["id"], not matches[0]["id"].
    read_json catches the "File not found" exception and returns None to
    signal "missing"; transport errors (OSError subclasses) propagate.

  list_files(path="/") -> dict {"files": [...], ...}
    Returns a wrapper dict, NOT a plain list. list_json extracts ["files"].

  download_file(file_id) -> http.client.HTTPResponse
    Has a .read() method; this is the only interface store.py uses.

  upload_file(data, file_type, file_size, filepath) -> dict
    Signature matches; io.BytesIO satisfies the io.BufferedReader contract.

  fulcra_api(url_path, method="GET", query=None, data=None, ...) -> bytes
    store.py uses keyword args throughout so parameter order doesn't matter.
"""
from __future__ import annotations
import io
import json
from .schema import Signal, canonical_json, temp_signal_id, CAPTURE_SOURCE_PREFIX


def build_record(sig: Signal, data_type: str) -> dict:
    """Canonical record envelope for ingest/outbox spool.

    Single authoritative place for record shape: ingest_signal, capture's
    outbox spool, and cmd_get's disclosure spool all call this. Using
    canonical_json for the data field ensures deterministic byte output
    (sorted keys, fixed float precision) regardless of call site.
    """
    sid = sig.id or temp_signal_id(sig.key, sig.observed_at, sig.platform)
    return {
        "data": canonical_json(sig.to_payload()),
        "metadata": {
            "content_type": "application/json",
            "data_type": data_type,
            "recorded_at": sig.observed_at,
            "source": [sid, f"{CAPTURE_SOURCE_PREFIX}{sig.platform}"],
        },
        "specversion": 1,
    }

PREFS_ROOT = "prefs"
META_PATH = f"{PREFS_ROOT}/meta.json"
COMPILED_PATH = f"{PREFS_ROOT}/compiled.json"
CONSENT_PATH = f"{PREFS_ROOT}/consent.json"
SIGNALS_CACHE_PREFIX = f"{PREFS_ROOT}/signals-cache"


def _abs(path: str) -> str:
    """The file API requires absolute paths for uploads (422 otherwise,
    verified live 2026-06-10); reads tolerate both. Normalize everything."""
    return path if path.startswith("/") else "/" + path


def platform_path(platform: str) -> str:
    return f"{PREFS_ROOT}/platforms/{platform}.json"


class FulcraStore:
    def __init__(self, api):
        self._api = api                      # fulcra_api.core.FulcraAPI (or fake)

    def read_json(self, path: str):
        """Return the parsed JSON at path, or None if the file does not exist.

        resolve_filepath(path) returns ONE dict when found, and raises
        Exception("File not found in Fulcra Library: ...") when absent.
        We catch only that specific message so transport failures (OSError
        subclasses) still propagate — callers must not confuse an outage
        with a legitimately missing file.
        """
        try:
            match = self._api.resolve_filepath(_abs(path))
        except Exception as e:
            if "File not found" in str(e):
                return None
            raise
        resp = self._api.download_file(match["id"])
        return json.loads(resp.read().decode())

    def write_json(self, path: str, obj) -> None:
        body = canonical_json(obj).encode()
        self._api.upload_file(io.BytesIO(body), "application/json",
                              len(body), _abs(path))

    def list_json(self, folder_path: str) -> list[dict]:
        """List direct JSON children under a folder. Used by the v1 signals-cache
        workaround as one-file-per-signal shards, avoiding a shared remote RMW
        file that concurrent captures could clobber.

        The real fulcra_api.list_files returns {"files": [...]} — a wrapper dict,
        not a plain list — so we extract the "files" key before iterating.
        """
        result = self._api.list_files(_abs(folder_path))
        # Real library wraps results: {"files": [...], ...}
        file_records = result["files"] if isinstance(result, dict) else result
        out = []
        for rec in file_records:
            resp = self._api.download_file(rec["id"])
            out.append(json.loads(resp.read().decode()))
        return out

    def ingest_signal(self, sig: Signal, data_type: str) -> None:
        record = build_record(sig, data_type)
        self._api.fulcra_api("/ingest/v1/record", data=record, method="POST")
