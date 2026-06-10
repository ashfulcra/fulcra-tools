"""fulcra-prefs CLI. run(argv, api, outbox_dir, now) is dependency-injected
for tests; main() binds the real FulcraAPI (reusing the user's `fulcra auth
login` credentials), the real outbox dir, and the real clock.

Signal reads in v1 go through a compile cache in the file library: capture
posts the canonical signal to Fulcra and writes one independent cache shard per
signal id under `prefs/signals-cache/`. Because the fulcra-api library has no
record-read-by-definition helper for arbitrary windows wired here yet, compile
lists those cache shards. Do NOT use one shared `signals-cache.json` file:
that would be a remote read-modify-write race across concurrently-capturing
platforms and would violate SPEC.md's atomic-capture rationale. The shard cache
is an implementation detail replaced by real get-records reads when CLI
annotation commands land (tracked on the bus).

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
from .capture import capture_signal
from .compileprefs import compile_signals
from .consent import disclosure_signal, filter_for_audience
from .inject import render_block
from .outbox import Outbox
from .schema import Signal, canonical_json, parse_record
from .solver import solve
from .store import (FulcraStore, build_record, COMPILED_PATH, CONSENT_PATH,
                    META_PATH, PREFS_ROOT, SIGNALS_CACHE_PREFIX, platform_path)


def _store(api) -> FulcraStore:
    return FulcraStore(api)


def _require_meta(store: FulcraStore) -> dict | None:
    meta = store.read_json(META_PATH)
    if not meta:
        print("fulcra-prefs: not onboarded — run `fulcra-prefs onboard` first "
              "(creates the Preference Signals definition + prefs/meta.json).",
              file=sys.stderr)
    return meta


def _load_cached_signals(store: FulcraStore) -> list[Signal]:
    return [parse_record(env) for env in store.list_json(SIGNALS_CACHE_PREFIX)]


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


def cmd_capture(args, api, outbox_dir, now) -> int:
    store = _store(api)
    meta = _require_meta(store)
    if not meta:
        return 2
    sig = capture_signal(
        store, Outbox(outbox_dir), data_type=meta["data_type"], now=now,
        key=args.key, value=json.loads(args.value), strength=args.strength,
        kind=args.kind, scope=args.scope, confidence=args.confidence,
        half_life_days=args.half_life, platform=args.platform,
        agent=args.agent, session=args.session, supersedes=args.supersedes)
    outbox = Outbox(outbox_dir)
    try:
        _append_signal_cache(store, sig)
    except (OSError, ConnectionError, TimeoutError) as e:
        # Cache-write failure must never abort a successful capture. The signal
        # is already posted (or spooled), but v1 compile reads cache shards, so
        # also spool the record for a later flush/back-fill.
        outbox.spool(build_record(sig, meta["data_type"]))
        print(f"fulcra-prefs: warning: could not write signal cache shard: {e}",
              file=sys.stderr)
    print(f"captured {sig.id}", file=sys.stderr)
    return 0


def cmd_compile(args, api, outbox_dir, now) -> int:
    store = _store(api)
    meta = _require_meta(store)
    if not meta:
        return 2
    Outbox(outbox_dir).flush(store)
    docs = compile_signals(_load_cached_signals(store), now)
    store.write_json(COMPILED_PATH, docs["global"])
    for p, doc in docs["platforms"].items():
        store.write_json(platform_path(p), doc)
    # Watermark meta.json so callers can tell when a compile last ran
    # without reading the full compiled doc.
    meta["last_compile"] = now.isoformat()
    store.write_json(META_PATH, meta)
    print(f"compiled {len(docs['global']['keys'])} keys, "
          f"{len(docs['platforms'])} platform views", file=sys.stderr)
    return 0


def cmd_get(args, api, outbox_dir, now) -> int:
    store = _store(api)
    path = platform_path(args.platform) if args.platform else COMPILED_PATH
    doc = store.read_json(path) or {"v": 1, "compiled_at": now.isoformat(),
                                    "keys": {}}
    if args.audience:
        grants = (store.read_json(CONSENT_PATH) or {"grants": []})["grants"]
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
                             if not (g["audience"] == args.audience
                                     and g["key_glob"] == args.key_glob)]
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
    return p


def run(argv, api, outbox_dir, now) -> int:
    args = _parser().parse_args(argv)
    handlers = {"onboard": lambda: cmd_onboard(args, api, now),
                "capture": lambda: cmd_capture(args, api, outbox_dir, now),
                "compile": lambda: cmd_compile(args, api, outbox_dir, now),
                "get": lambda: cmd_get(args, api, outbox_dir, now),
                "consent": lambda: cmd_consent(args, api, outbox_dir, now),
                "inject": lambda: cmd_inject(args, api, outbox_dir, now),
                "solve": lambda: cmd_solve(args, api, outbox_dir, now)}
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
