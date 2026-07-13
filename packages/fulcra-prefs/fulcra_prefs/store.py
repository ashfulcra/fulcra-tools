"""The ONLY module that talks to Fulcra. Files via the fulcra_api library
(list/resolve/download/upload); record writes via the generic request method
to the TYPED ingest surface POST /ingest/v1/record/{data_type} — the library
has no record-write helper yet (see FULCRA-PRIMITIVES.md); switch to CLI/lib
annotation commands when they land.

Ingest write path (migrated 2026-07-13 from the legacy wrapped DataRecordV1
envelope to the typed surface, live-verified against MomentAnnotation
v1alpha1): build_record stays the single canonical envelope used by the outbox
spool + signals-cache shards; typed_body()/typed_ingest_endpoint() translate it
to the unwrapped typed body at the HTTP boundary. The custom definition rides in
`sources` (base type in the path), and the read side already reads note-or-data,
so this is forward-compatible with records ingested via either path.

Real-library shape notes (fulcra_api.core.FulcraAPI, verified v0.1.36):

  resolve_filepath(filepath, all_versions=False) -> list[dict]
    Returns a LIST of matching file records (the "files" array) when found;
    raises Exception("File not found in Fulcra: <filepath>") when absent.
    CHANGED in 0.1.36: pre-0.1.36 it returned a single dict, so read_json did
    match["id"]; now it returns a list and read_json must take matches[0]["id"].
    read_json tolerates BOTH shapes (list or lone dict) so it never re-breaks
    on either. It catches the "File not found" exception (substring match, so
    the "Library" wording drop in 0.1.36 is fine) and returns None to signal
    "missing"; transport errors (OSError subclasses) propagate.

  list_files(path="/", state="uploaded") -> dict {"files": [...], ...}
    Returns a wrapper dict, NOT a plain list. list_json extracts ["files"].
    Unchanged across the 0.1.35->0.1.36 bump.

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
from concurrent.futures import ThreadPoolExecutor
from .schema import (Signal, canonical_json, parse_record, temp_signal_id,
                     CAPTURE_SOURCE_PREFIX, ANNOTATION_SOURCE_PREFIX)


def build_record(sig: Signal, data_type: str) -> dict:
    """Canonical record envelope for ingest/outbox spool.

    Single authoritative place for record shape: ingest_signal, capture's
    outbox spool, and cmd_get's disclosure spool all call this. Using
    canonical_json for the data field ensures deterministic byte output
    (sorted keys, fixed float precision) regardless of call site.

    data_type format — split on the first "/":
      - The base type (e.g. "MomentAnnotation") is the FulcraDataTypes enum
        value the API accepts. Sending "MomentAnnotation/<uuid>" causes a 422.
      - The optional suffix is the definition id; when present it rides in
        metadata.source as "com.fulcradynamics.annotation.<definition_id>",
        matching the production wire.ts pattern (packages/attention/chrome/src/
        relayless/wire.ts lines 203-206): source = [sid, annotation-linkage].
        We append our extra capture-platform marker after those two, giving:
        source = [sid, annotation-linkage, capture-marker].
      - meta.json stores "data_type": "MomentAnnotation/<id>" as a read-side
        shorthand; only build_record decomposes it for the wire.
    """
    sid = sig.id or temp_signal_id(sig.key, sig.observed_at, sig.platform, sig.value)
    base_type, _, definition_id = data_type.partition("/")
    source: list[str] = [sid]
    if definition_id:
        source.append(f"{ANNOTATION_SOURCE_PREFIX}{definition_id}")
    source.append(f"{CAPTURE_SOURCE_PREFIX}{sig.platform}")
    return {
        "data": canonical_json(sig.to_payload()),
        "metadata": {
            "content_type": "application/json",
            "data_type": base_type,
            "recorded_at": sig.observed_at,
            "source": source,
        },
        "specversion": 1,
    }

def typed_ingest_endpoint(record: dict) -> str:
    """Typed ingest path for a canonical build_record envelope: the base
    data_type is a URL path segment (e.g. /ingest/v1/record/MomentAnnotation).
    A custom definition is NOT a valid path segment — it rides in `sources`
    instead (see typed_body), so the path is always the base type."""
    return f"/ingest/v1/record/{record['metadata']['data_type']}"


def typed_body(record: dict) -> dict:
    """Translate the canonical DataRecordV1 envelope (build_record output) into
    the UNWRAPPED body the typed endpoint expects. Mapping verified against the
    live MomentAnnotation v1alpha1 record schema (fields: id?/tags?/sources?/
    recorded_at?/note?):
        metadata.source     -> sources   (plural; carries the def linkage)
        data (JSON string)  -> note      (top-level string payload)
        metadata.recorded_at-> recorded_at
    `id` is omitted so the server generates one; the deterministic temp id stays
    in sources[0], which is what the read side keys on. `content_type` has no
    typed-body slot (the payload is a JSON string in `note`, parsed on read)."""
    md = record["metadata"]
    return {
        "note": record["data"],
        "recorded_at": md["recorded_at"],
        "sources": md["source"],
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

        resolve_filepath(path) returns a list[dict] of matching file records in
        fulcra-api 0.1.36 (it was a single dict pre-0.1.36), and raises
        Exception("File not found in Fulcra: ...") when absent. We catch only
        the "File not found" message (substring, so the 0.1.36 wording change is
        tolerated) so transport failures (OSError subclasses) still propagate —
        callers must not confuse an outage with a legitimately missing file.
        The first match is the current uploaded version (state="uploaded").
        """
        try:
            matches = self._api.resolve_filepath(_abs(path))
        except Exception as e:
            if "File not found" in str(e):
                return None
            raise
        # 0.1.36 returns a list; older libs / fakes may return a lone dict.
        match = matches[0] if isinstance(matches, list) else matches
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
        if not file_records:
            return []

        def _fetch(rec):
            resp = self._api.download_file(rec["id"])
            return json.loads(resp.read().decode())

        # Shard downloads are independent GETs and compile sorts by signal id,
        # so result order is irrelevant — fetch concurrently to keep compile
        # from scaling as N sequential round-trips. Bounded pool to avoid
        # hammering the API. ex.map preserves order and re-raises the first
        # download error, matching the previous sequential semantics.
        if len(file_records) == 1:
            return [_fetch(file_records[0])]
        with ThreadPoolExecutor(max_workers=min(8, len(file_records))) as ex:
            return list(ex.map(_fetch, file_records))

    def list_file_ids(self, folder_path: str) -> list[tuple[str, str]]:
        """(name, file_id) for direct children of a folder, WITHOUT downloading
        contents — used by cache GC, which prunes shards by filename (the temp
        id) so it never needs to read them."""
        result = self._api.list_files(_abs(folder_path))
        recs = result["files"] if isinstance(result, dict) else result
        return [(r.get("name", ""), r["id"]) for r in (recs or [])]

    def delete_file(self, file_id: str) -> None:
        self._api.delete_file(file_id)

    def ingest_signal(self, sig: Signal, data_type: str) -> None:
        record = build_record(sig, data_type)
        self._api.fulcra_api(typed_ingest_endpoint(record),
                             data=typed_body(record), method="POST")

    def read_signal_records(self, definition_id: str | None,
                            start_time=None, end_time=None) -> list[Signal]:
        """Authoritative signal read via get-records, so captures from ANY
        platform are visible to compile — including shell-less tier-2 agents
        that only POST to /ingest and never write a cache shard. Records are
        matched to our definition by the annotation-linkage source.

        Resilient by design: a transport error returns [] so compile can still
        proceed from the shard cache (never worse than the cache-only path this
        augments). Records that don't parse as our signals are skipped, not
        fatal. The payload field is read defensively (`data` then `note`):
        the live get-records shape for ingested DataRecordV1 records is pinned
        by the live-smoke round-trip, not assumed here.
        """
        if not definition_id:
            return []
        linkage = f"{ANNOTATION_SOURCE_PREFIX}{definition_id}"
        try:
            records = self._api.moment_annotations(start_time, end_time)
        except (OSError, ConnectionError, TimeoutError):
            return []
        out: list[Signal] = []
        for rec in (records or []):
            sources = rec.get("sources") or []
            if linkage not in sources:
                continue
            payload = rec.get("data")
            if payload is None:
                payload = rec.get("note")
            env = {"id": rec.get("id"), "recorded_at": rec.get("recorded_at"),
                   "sources": sources, "data": payload}
            try:
                out.append(parse_record(env))
            except (KeyError, ValueError, TypeError):
                continue   # not one of our signals / unexpected shape
        return out
