"""fulcra-prefs CLI. run(argv, api, outbox_dir, now) is dependency-injected
for tests; main() binds the real FulcraAPI (reusing the user's `fulcra auth
login` credentials), the real outbox dir, and the real clock.

Signal reads: compile reads authoritatively via get-records
(store.read_signal_records) so captures from any platform are visible, UNIONed
with a write-through per-signal shard cache under `prefs/signals-cache/` (one
file per signal id — never a shared `signals-cache.json`, which would be a
remote read-modify-write race across concurrently-capturing platforms, against
SPEC.md's atomic-capture rationale). The shard cache covers offline-captured-
not-yet-ingested signals and ingest->read indexing lag; compile GCs a shard once
its record is confirmed in get-records, keeping the cache bounded. The remaining
workaround is the write side (no record delete/replace yet — corrections are
`supersedes`); native revocation lands when CLI annotation record commands do.

INTEGRATION DEVIATIONS from plan sketch (verified against fulcra-api 0.1.33):
  1. Credential wiring: FulcraAPI takes credentials= and refresh_callback= at
     construction time. The plan's `api.credentials = creds` would set a plain
     attribute rather than `api.fulcra_credentials`, so we pass both as kwargs
     to the constructor. The plan's api.refresh_callback assignment post-init
     would still work since it's just an attribute, but constructor is cleaner.
  2. Import path: `fulcra_api/cli.py` is a single module (not a subpackage),
     so `from fulcra_api.cli import load_creds, save_creds` — NOT `.cli.utils`.
  3. create_annotation: FulcraAPI 0.1.33 has no create_annotation() helper.
     cmd_onboard calls api.fulcra_api("/user/v1alpha1/annotation", data=...,
     method="POST") directly, matching the annotations_catalog() read pattern.
     The response is JSON bytes; we json.loads() and read ["id"].
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from .candidates import append_candidate, candidate_file, mark_captured
from .capture import capture_signal
from .compileprefs import compile_signals
from .consent import disclosure_signal, filter_for_audience
from .extract import extract_candidates
from .inject import render_block
from .installers import install_platform_hooks
from .outbox import Outbox
from .schema import Signal, canonical_json, parse_record, TEMP_ID_PREFIX
from .solver import solve
from .store import (FulcraStore, build_record, COMPILED_PATH, CONSENT_PATH,
                    META_PATH, SIGNALS_CACHE_PREFIX, platform_path)


def _store(api) -> FulcraStore:
    return FulcraStore(api)


def _require_meta(store: FulcraStore) -> dict | None:
    meta = store.read_json(META_PATH)
    if not meta:
        print("fulcra-prefs: not onboarded — run `fulcra-prefs onboard` first "
              "(creates the Preference Signals definition + prefs/meta.json).",
              file=sys.stderr)
    return meta


def _dedup_key(sig: Signal) -> str:
    # One capture has two representations: the authoritative get-records record
    # (id = Fulcra record id) and its write-through cache shard (id = temp id).
    # Both carry the temp id in sources, so key on that temp id to collapse them;
    # fall back to the signal id when there's no temp id (e.g. a synthetic shard).
    for s in sig.source_ids:
        if s.startswith(TEMP_ID_PREFIX):
            return s
    return sig.id or ""


def _gather_signals(store: FulcraStore, meta: dict | None = None
                    ) -> tuple[list[Signal], set[str]]:
    """Return (merged signals, confirmed capture-keys).

    Merged = union of the authoritative record read (visible to ALL platforms,
    incl. tier-2) and the local shard cache (covers offline-captured-not-yet-
    ingested and ingest->read indexing lag), deduped by capture identity;
    records are listed first so they win the dedup over their shard twin.
    Confirmed = the dedup keys that came from get-records — i.e. captures the
    authoritative source has, whose write-through shards are now safe to GC."""
    records = store.read_signal_records(meta.get("definition_id")) if meta else []
    shards: list[Signal] = []
    skipped_shards = 0
    for env in store.list_json(SIGNALS_CACHE_PREFIX):
        try:
            shards.append(parse_record(env))
        except (KeyError, ValueError, TypeError):
            skipped_shards += 1
    if skipped_shards:
        print(f"fulcra-prefs: skipped {skipped_shards} invalid cached signal shard(s)",
              file=sys.stderr)
    by_id: dict[str, Signal] = {}
    for sig in (*records, *shards):
        by_id.setdefault(_dedup_key(sig), sig)
    return list(by_id.values()), {_dedup_key(s) for s in records}


def _gc_confirmed_shards(store: FulcraStore, confirmed: set[str]) -> int:
    """Delete cache shards whose capture is confirmed in get-records. Shard
    filenames are the temp id (== the records' dedup key), so we prune by name
    without downloading. Unconfirmed shards (read outage / indexing lag) are
    kept — they may be the only copy of a signal. Best-effort: a failed delete
    is retried on the next compile, never fatal."""
    pruned = 0
    for name, fid in store.list_file_ids(SIGNALS_CACHE_PREFIX):
        stem = name[:-5] if name.endswith(".json") else name
        if stem in confirmed:
            try:
                store.delete_file(fid)
                pruned += 1
            except Exception:
                continue
    return pruned


def _append_signal_cache(store: FulcraStore, sig: Signal) -> None:
    # One file per signal id: concurrent captures write disjoint paths instead
    # of racing on a shared cache blob. The cache record mirrors get-records
    # enough for parse_record and preserves the temp id in sources.
    sid = sig.id
    env = {"id": sid, "recorded_at": sig.observed_at,
           "sources": [sid], "data": json.dumps(sig.to_payload())}
    store.write_json(f"{SIGNALS_CACHE_PREFIX}/{sid}.json", env)


def cmd_onboard(args, api, now) -> int:
    """Create the Preference Signals annotation definition and write meta.json.

    DEVIATION: Uses api.fulcra_api() directly because FulcraAPI 0.1.33 has no
    create_annotation() convenience method. POST to /user/v1alpha1/annotation
    mirrors the GET in annotations_catalog(). The body shape follows the Fulcra
    annotation definition schema: annotation_type='moment', name, description,
    tags, and spec fields — spec may be omitted for a moment annotation.
    """
    store = _store(api)
    if store.read_json(META_PATH):
        print("already onboarded", file=sys.stderr)
        return 0
    # POST to the same endpoint used by annotations_catalog() GET.
    # The annotation_type 'moment' produces a MomentAnnotation data type.
    annotation_body = {
        "annotation_type": "moment",
        "name": "Preference Signals",
        "description": ("Typed preference/fact/consent signals "
                        "captured by fulcra-prefs."),
        "tags": [],
        "spec": {},
    }
    resp = api.fulcra_api("/user/v1alpha1/annotation", data=annotation_body,
                          method="POST")
    created = json.loads(resp) if isinstance(resp, bytes) else resp
    def_id = created["id"] if isinstance(created, dict) else created
    store.write_json(META_PATH, {"definition_id": def_id,
                                 "data_type": f"MomentAnnotation/{def_id}",
                                 "v": 1})
    print(f"onboarded: definition {def_id}", file=sys.stderr)
    return 0


def _capture_one(store, outbox, meta, now, **kw) -> Signal:
    """Capture a single signal (ingest + write-through shard). Shared by
    `capture` and `capture-batch`. A cache-shard write failure never aborts a
    successful capture — the record is spooled for a later flush/back-fill (the
    signal is already posted or itself spooled by capture_signal)."""
    sig = capture_signal(store, outbox, data_type=meta["data_type"], now=now, **kw)
    try:
        _append_signal_cache(store, sig)
    except (OSError, ConnectionError, TimeoutError) as e:
        outbox.spool(build_record(sig, meta["data_type"]))
        print(f"fulcra-prefs: warning: could not write signal cache shard: {e}",
              file=sys.stderr)
    return sig


def cmd_capture(args, api, outbox_dir, now) -> int:
    store = _store(api)
    meta = _require_meta(store)
    if not meta:
        return 2
    sig = _capture_one(
        store, Outbox(outbox_dir), meta, now,
        key=args.key, value=json.loads(args.value), strength=args.strength,
        kind=args.kind, scope=args.scope, confidence=args.confidence,
        half_life_days=args.half_life, platform=args.platform,
        agent=args.agent, session=args.session, supersedes=args.supersedes)
    print(f"captured {sig.id}", file=sys.stderr)
    return 0


def _normalize_batch_spec(spec: object, index: int, args
                          ) -> tuple[dict | None, str | None]:
    if not isinstance(spec, dict):
        return None, f"--file item {index} must be an object"
    missing = [k for k in ("key", "value", "strength") if k not in spec]
    if missing:
        return None, (f"--file item {index} missing required field(s): "
                      f"{', '.join(missing)}")
    try:
        strength = float(spec["strength"])
        confidence = float(spec.get("confidence", 1.0))
        hl = spec.get("half_life_days", 90.0)
        half_life_days = None if hl is None else float(hl)
    except (TypeError, ValueError) as e:
        return None, f"--file item {index} has invalid numeric field: {e}"
    normalized = {
        "key": spec["key"],
        "value": spec["value"],
        "strength": strength,
        "kind": spec.get("kind", "preference"),
        "scope": spec.get("scope", "global"),
        "confidence": confidence,
        "half_life_days": half_life_days,
        "platform": args.platform,
        "agent": spec.get("agent", args.agent),
        "session": spec.get("session", args.session),
        "supersedes": spec.get("supersedes"),
    }
    try:
        Signal(id=None, observed_at="1970-01-01T00:00:00+00:00",
               source_ids=(), **normalized)
    except ValueError as e:
        return None, f"--file item {index} is invalid: {e}"
    return normalized, None


def cmd_capture_batch(args, api, outbox_dir, now) -> int:
    """Auto-capture mechanism: record many signals an agent noticed in one
    consented call. `--file` is a JSON array of specs (key, value, strength, and
    optional kind/scope/confidence/half_life_days/agent/session/supersedes).
    Lower-confidence INFERRED signals are safe — compile weights selection by
    confidence so they won't override explicit ones."""
    store = _store(api)
    meta = _require_meta(store)
    if not meta:
        return 2
    try:
        specs = json.loads(Path(args.file).read_text())
    except (OSError, ValueError) as e:
        print(f"fulcra-prefs: could not read --file: {e}", file=sys.stderr)
        return 2
    if not isinstance(specs, list):
        print("fulcra-prefs: --file must contain a JSON array of signal specs",
              file=sys.stderr)
        return 2
    normalized_specs = []
    for i, spec in enumerate(specs, start=1):
        normalized, err = _normalize_batch_spec(spec, i, args)
        if err:
            print(f"fulcra-prefs: {err}", file=sys.stderr)
            return 2
        normalized_specs.append(normalized)
    outbox = Outbox(outbox_dir)
    for spec in normalized_specs:
        _capture_one(store, outbox, meta, now, **spec)
    print(f"captured {len(normalized_specs)} signal(s)", file=sys.stderr)
    return 0


def _notice_spec(args) -> dict:
    spec = {
        "key": args.key,
        "value": json.loads(args.value),
        "strength": args.strength,
        "kind": args.kind,
        "scope": args.scope,
        "confidence": args.confidence,
        "half_life_days": args.half_life,
    }
    if args.agent:
        spec["agent"] = args.agent
    if args.supersedes:
        spec["supersedes"] = args.supersedes
    if args.session:
        spec["session"] = args.session
    normalized, err = _normalize_batch_spec(spec, 1, args)
    if err:
        raise ValueError(err)
    return normalized or {}


def cmd_notice(args, api, outbox_dir, now) -> int:
    try:
        path = candidate_file(args.platform, args.session, root=args.candidate_dir)
        spec = _notice_spec(args)
        count = append_candidate(path, spec)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"fulcra-prefs: could not write candidate: {e}", file=sys.stderr)
        return 2
    print(f"queued {count} candidate(s) in {path}", file=sys.stderr)
    return 0


def cmd_candidate_path(args) -> int:
    try:
        path = candidate_file(args.platform, args.session, root=args.candidate_dir)
    except ValueError as e:
        print(f"fulcra-prefs: {e}", file=sys.stderr)
        return 2
    print(path)
    return 0


def cmd_drain_candidates(args, api, outbox_dir, now) -> int:
    try:
        path = candidate_file(args.platform, args.session, root=args.candidate_dir)
    except ValueError as e:
        print(f"fulcra-prefs: {e}", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"no candidates at {path}", file=sys.stderr)
        return 0
    rc = cmd_capture_batch(
        argparse.Namespace(file=str(path), platform=args.platform,
                           agent=args.agent, session=args.session),
        api, outbox_dir, now,
    )
    if rc != 0:
        return rc
    try:
        captured = mark_captured(path)
    except OSError as e:
        print(f"fulcra-prefs: warning: captured but could not rename {path}: {e}",
              file=sys.stderr)
        return 0
    print(f"drained candidates -> {captured}", file=sys.stderr)
    return 0


def _read_extract_text(args) -> str:
    if args.text is not None:
        return args.text
    if args.file:
        return Path(args.file).read_text()
    return sys.stdin.read()


def cmd_extract_candidates(args, api, outbox_dir, now) -> int:
    try:
        text = _read_extract_text(args)
        candidates = extract_candidates(
            text, platform=args.platform, session=args.session, agent=args.agent)
        if args.write:
            path = candidate_file(args.platform, args.session, root=args.candidate_dir)
            for candidate in candidates:
                append_candidate(path, candidate)
            print(f"queued {len(candidates)} extracted candidate(s) in {path}",
                  file=sys.stderr)
        else:
            print(canonical_json(candidates))
    except (OSError, ValueError) as e:
        print(f"fulcra-prefs: could not extract candidates: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_compile(args, api, outbox_dir, now) -> int:
    store = _store(api)
    meta = _require_meta(store)
    if not meta:
        return 2
    Outbox(outbox_dir).flush(store)
    signals, confirmed = _gather_signals(store, meta)
    docs = compile_signals(signals, now)
    store.write_json(COMPILED_PATH, docs["global"])
    for p, doc in docs["platforms"].items():
        store.write_json(platform_path(p), doc)
    # Watermark meta.json so callers can tell when a compile last ran
    # without reading the full compiled doc.
    meta["last_compile"] = now.isoformat()
    store.write_json(META_PATH, meta)
    # GC: prune write-through shards now confirmed in the authoritative read,
    # bounding the cache and keeping compile from re-downloading dead shards.
    pruned = _gc_confirmed_shards(store, confirmed)
    print(f"compiled {len(docs['global']['keys'])} keys, "
          f"{len(docs['platforms'])} platform views"
          + (f", pruned {pruned} cached shard(s)" if pruned else ""),
          file=sys.stderr)
    return 0


def cmd_get(args, api, outbox_dir, now) -> int:
    store = _store(api)
    # A platform view is global + overlay. Compile only writes platforms/<p>.json
    # when <p> has a platform:-scoped signal, so for an override-less platform we
    # must fall back to the global doc (mirrors cmd_inject) — returning an empty
    # doc here would silently withhold the user's global prefs on the export path.
    doc = None
    if args.platform:
        doc = store.read_json(platform_path(args.platform))
    if doc is None:
        doc = store.read_json(COMPILED_PATH)
    doc = doc or {"v": 1, "compiled_at": now.isoformat(), "keys": {}}
    if args.audience:
        grants = (store.read_json(CONSENT_PATH) or {}).get("grants", [])
        doc = filter_for_audience(doc, grants, args.audience, now)
        meta = store.read_json(META_PATH)
        if meta and doc["keys"]:
            sig = disclosure_signal(sorted(doc["keys"]), args.audience,
                                    platform=args.platform or "cli", now=now)
            try:
                store.ingest_signal(sig, data_type=meta["data_type"])
            except (OSError, ConnectionError, TimeoutError):
                # Ledger guarantee: never disclose unlogged. Spool the
                # disclosure so it lands on the next flush, then proceed.
                Outbox(outbox_dir).spool(build_record(sig, meta["data_type"]))
                print("fulcra-prefs: disclosure log deferred to outbox "
                      "(ingest unreachable)", file=sys.stderr)
    print(canonical_json(doc))
    return 0


def cmd_consent(args, api, outbox_dir, now) -> int:
    store = _store(api)
    consent = store.read_json(CONSENT_PATH) or {"v": 1, "grants": []}
    consent.setdefault("grants", [])  # tolerate a legacy/partial file w/o 'grants'
    if args.consent_action == "grant":
        consent["grants"].append({"key_glob": args.key_glob,
                                  "audience": args.audience,
                                  "level": args.level,
                                  "granted_at": now.isoformat(),
                                  "expires": args.expires})
        store.write_json(CONSENT_PATH, consent)
        print(f"granted {args.key_glob} -> {args.audience}", file=sys.stderr)
    elif args.consent_action == "revoke":
        before = len(consent["grants"])
        consent["grants"] = [g for g in consent["grants"]
                             if not (g.get("audience") == args.audience
                                     and g.get("key_glob") == args.key_glob)]
        store.write_json(CONSENT_PATH, consent)
        print(f"revoked {before - len(consent['grants'])} grant(s)", file=sys.stderr)
    else:
        print(canonical_json(consent))
    return 0


def cmd_inject(args, api, outbox_dir, now) -> int:
    """Render the preference block for a session bootstrap.

    Per SPEC.md errors & edges: inject must NEVER break a session start.
    Any exception (network outage, missing file, corrupt JSON) is caught
    here; no output reaches stdout and a one-line warning goes to stderr.
    The caller (a session pre-prompt hook) ignores stderr.
    """
    try:
        store = _store(api)
        doc = store.read_json(platform_path(args.platform)) \
            or store.read_json(COMPILED_PATH)
        block = render_block(doc, platform=args.platform)
        if block:
            print(block)
    except Exception as e:
        print(f"fulcra-prefs: inject warning: {e}", file=sys.stderr)
    return 0


def cmd_solve(args, api, outbox_dir, now) -> int:
    options = json.loads(Path(args.options).read_text())
    participants = json.loads(Path(args.participants).read_text())
    result = solve(options, participants, policy=args.policy,
                   veto_threshold=args.veto_threshold)
    print(canonical_json(result))
    return 0


def cmd_install_hooks(args) -> int:
    try:
        plan = install_platform_hooks(
            platform=args.platform,
            target_dir=args.target_dir,
            uninstall=args.uninstall,
            dry_run=args.dry_run,
        )
    except ValueError as e:
        print(f"fulcra-prefs: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(canonical_json(plan))
    elif args.uninstall:
        print(f"removed fulcra-prefs hooks from {plan['config']}", file=sys.stderr)
    else:
        print(f"installed fulcra-prefs {args.platform} hooks -> {plan['config']}",
              file=sys.stderr)
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fulcra-prefs")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("onboard")

    c = sub.add_parser("capture")
    c.add_argument("--key", required=True)
    c.add_argument("--value", required=True, help="JSON value")
    c.add_argument("--strength", type=float, required=True)
    c.add_argument("--kind", default="preference",
                   choices=["preference", "fact", "consent"])
    c.add_argument("--scope", default="global")
    c.add_argument("--confidence", type=float, default=1.0)
    c.add_argument("--half-life", type=float, default=90.0, dest="half_life")
    c.add_argument("--platform", required=True)
    c.add_argument("--agent")
    c.add_argument("--session")
    c.add_argument("--supersedes")

    cb = sub.add_parser("capture-batch",
                        help="capture many signals from a JSON array file")
    cb.add_argument("--file", required=True, help="path to a JSON array of signal specs")
    cb.add_argument("--platform", required=True)
    cb.add_argument("--agent")
    cb.add_argument("--session")

    n = sub.add_parser("notice",
                       help="queue one auto-capture candidate for lifecycle drain")
    n.add_argument("--key", required=True)
    n.add_argument("--value", required=True, help="JSON value")
    n.add_argument("--strength", type=float, required=True)
    n.add_argument("--kind", default="preference",
                   choices=["preference", "fact", "consent"])
    n.add_argument("--scope", default="global")
    n.add_argument("--confidence", type=float, default=1.0)
    n.add_argument("--half-life", type=float, default=90.0, dest="half_life")
    n.add_argument("--platform", required=True)
    n.add_argument("--session", required=True)
    n.add_argument("--agent")
    n.add_argument("--supersedes")
    n.add_argument("--candidate-dir")

    cp = sub.add_parser("candidate-path",
                        help="print the auto-capture candidate queue path")
    cp.add_argument("--platform", required=True)
    cp.add_argument("--session", required=True)
    cp.add_argument("--candidate-dir")

    dc = sub.add_parser("drain-candidates",
                        help="capture and mark one session candidate queue")
    dc.add_argument("--platform", required=True)
    dc.add_argument("--session", required=True)
    dc.add_argument("--agent")
    dc.add_argument("--candidate-dir")

    ex = sub.add_parser("extract-candidates",
                        help="extract explicit preference candidates from text")
    ex.add_argument("--platform", required=True)
    ex.add_argument("--session", required=True)
    ex.add_argument("--agent")
    ex.add_argument("--text")
    ex.add_argument("--file")
    ex.add_argument("--write", action="store_true",
                    help="append extracted candidates to the session queue")
    ex.add_argument("--candidate-dir")

    sub.add_parser("compile")

    g = sub.add_parser("get")
    g.add_argument("--platform")
    g.add_argument("--for", dest="audience")

    co = sub.add_parser("consent")
    co_sub = co.add_subparsers(dest="consent_action", required=True)
    gr = co_sub.add_parser("grant")
    gr.add_argument("--key-glob", required=True)
    gr.add_argument("--audience", required=True)
    gr.add_argument("--level", default="read", choices=["read", "solve"])
    gr.add_argument("--expires")
    rv = co_sub.add_parser("revoke")
    rv.add_argument("--key-glob", required=True)
    rv.add_argument("--audience", required=True)
    co_sub.add_parser("list")

    i = sub.add_parser("inject")
    i.add_argument("--platform", required=True)

    s = sub.add_parser("solve")
    s.add_argument("--options", required=True, help="path to options JSON")
    s.add_argument("--participants", required=True,
                   help="path to {name: compiled_doc} JSON")
    s.add_argument("--policy", default="weighted-sum",
                   choices=["weighted-sum", "hard-veto"])
    s.add_argument("--veto-threshold", type=float, default=-0.5,
                   dest="veto_threshold")

    ih = sub.add_parser("install-hooks",
                        help="install platform session inject/capture hooks")
    ih.add_argument("--platform", required=True, choices=["claude-code", "codex"])
    ih.add_argument("--target-dir",
                    help="override platform config dir (default ~/.claude or ~/.codex)")
    ih.add_argument("--uninstall", action="store_true")
    ih.add_argument("--dry-run", action="store_true")
    return p


def run(argv, api, outbox_dir, now) -> int:
    args = _parser().parse_args(argv)
    handlers = {"onboard": lambda: cmd_onboard(args, api, now),
                "capture": lambda: cmd_capture(args, api, outbox_dir, now),
                "capture-batch": lambda: cmd_capture_batch(args, api, outbox_dir, now),
                "notice": lambda: cmd_notice(args, api, outbox_dir, now),
                "candidate-path": lambda: cmd_candidate_path(args),
                "drain-candidates": lambda: cmd_drain_candidates(args, api, outbox_dir, now),
                "extract-candidates": lambda: cmd_extract_candidates(args, api, outbox_dir, now),
                "compile": lambda: cmd_compile(args, api, outbox_dir, now),
                "get": lambda: cmd_get(args, api, outbox_dir, now),
                "consent": lambda: cmd_consent(args, api, outbox_dir, now),
                "inject": lambda: cmd_inject(args, api, outbox_dir, now),
                "solve": lambda: cmd_solve(args, api, outbox_dir, now),
                "install-hooks": lambda: cmd_install_hooks(args)}
    return handlers[args.command]()


def main() -> int:
    """Production entrypoint. Wires real FulcraAPI with persisted credentials.

    DEVIATION from plan sketch:
      - Import: `from fulcra_api.cli import load_creds, save_creds` (single
        module file, not a cli/utils subpackage).
      - Construction: FulcraAPI(credentials=creds, refresh_callback=save_creds)
        at construction time rather than post-init attribute assignment. The
        constructor sets self.fulcra_credentials from the credentials= kwarg;
        post-init `api.credentials = creds` would set a plain attribute named
        `credentials`, not `fulcra_credentials`, breaking is_expired() checks.
    """
    from fulcra_api.core import FulcraAPI
    # DEVIATION: single module fulcra_api.cli, not fulcra_api.cli.utils
    from fulcra_api.cli import load_creds, save_creds
    creds = load_creds()
    if creds is None:
        print("fulcra-prefs: not authenticated — run `fulcra auth login` first.",
              file=sys.stderr)
        return 2
    # DEVIATION: pass as constructor kwargs so FulcraAPI sets self.fulcra_credentials
    # correctly; post-init `api.credentials = creds` would NOT set fulcra_credentials.
    api = FulcraAPI(credentials=creds, refresh_callback=save_creds)
    outbox_dir = Path.home() / ".local/state/fulcra-prefs/outbox"
    return run(sys.argv[1:], api=api, outbox_dir=outbox_dir,
               now=datetime.now(timezone.utc))


if __name__ == "__main__":
    sys.exit(main())
