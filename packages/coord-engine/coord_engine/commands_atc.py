"""coord-engine ATC commands — the cross-subscription cap ledger (fulcra-agent-atc).

Extracted verbatim from ``cli.py`` (behavior-preserving module split): the usage
ledger, headroom/route folds, harvest/report, and interactive init. cli-level
shared helpers (``_now``/``_iso``/``_host``/``_stamp_for_path`` and the
review/roles path helpers) are reached through the ``cli`` module so
``monkeypatch.setattr(cli, …)`` still steers them and there is no import cycle at
module-load time. Dispatch stays wired in ``cli.build_parser``; every public name
here is re-exported from ``cli``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from . import atc, continuity, okf
from . import cli
from .transport import TransportError


# --- ATC: cross-subscription cap ledger (fulcra-agent-atc) -------------------


def _atc_accounts_path(team: str) -> str:
    return f"team/{team}/atc/accounts.json"


def _atc_bindings_path(team: str) -> str:
    return f"team/{team}/atc/bindings.json"


def _atc_usage_prefix(team: str) -> str:
    return f"team/{team}/atc/usage/"


def _atc_usage_shards(transport: Any, team: str) -> list[dict[str, Any]]:
    """Read usage shards into the row shape ``atc.headroom`` folds.

    Malformed shards (bad frontmatter, unparseable/absent ``ts``, no account)
    are skipped rather than raising — one corrupt shard cannot break the fold.
    """
    rows: list[dict[str, Any]] = []
    pfx = _atc_usage_prefix(team)
    try:
        entries = transport.list_dir(pfx)
    except TransportError:
        return rows
    for e in entries:
        n = e.get("name") or ""
        if e.get("is_dir") or not n.endswith(".md"):
            continue
        try:
            fm = okf.parse_frontmatter(transport.read(pfx + n)) or {}
            ts = continuity._parse_created_at(fm.get("ts"))
            if ts is None or not fm.get("account"):
                continue
            row = {"account": fm["account"], "ts": ts,
                   "units": int(fm.get("units") or 0),
                   "throttled": bool(fm.get("throttled"))}
            # `tier` drives the report/dash tier-mix + headline; the outcome
            # fields (model/task_class/outcome) flow through only when present.
            # v1 shards missing any of these reach the folds untouched.
            for k in ("tier", "model", "task_class", "outcome"):
                if fm.get(k) is not None:
                    row[k] = fm[k]
            rows.append(row)
        except Exception:
            continue
    return rows


def cmd_usage_log(args: argparse.Namespace, transport: Any) -> int:
    agent = args.agent or cli._host()
    task_class = getattr(args, "task_class", None)
    # --task-class is taxonomy-validated (exit 2 on unknown, matching route's
    # unknown-need contract) — validate BEFORE any write so a rejected
    # invocation leaves no shard behind. --outcome is argparse-choices gated.
    if task_class is not None and task_class not in atc.TAXONOMY:
        print(f"usage log — unknown task-class: {task_class} (must be one of: "
              f"{','.join(sorted(atc.TAXONOMY))})", file=sys.stderr)
        return 2
    ts = cli._iso(cli._now())
    fm = {"schema": "atc-usage/v1", "agent": agent, "ts": ts,
          "account": args.account, "tier": args.tier,
          "units": int(args.units or 0), "throttled": bool(args.throttled)}
    # Outcome-attribution fields are written ONLY when provided, so v1 shards
    # stay v1 (the headroom + demotions folds both tolerate their absence).
    if getattr(args, "model", None):
        fm["model"] = args.model
    if task_class is not None:
        fm["task_class"] = task_class
    if getattr(args, "outcome", None) is not None:
        fm["outcome"] = args.outcome
    # Path-safe stamp (colons stripped) + agent slug, matching the repo's
    # timestamped-shard convention (_stamp_for_path); fm["ts"] keeps the real
    # ISO value the headroom fold parses.
    transport.write(_atc_usage_prefix(args.team) + cli._stamp_for_path(ts, agent) + ".md",
                    okf.render_frontmatter(fm) + "\n")
    extra = "".join(
        f", {k}={fm[k]}" for k in ("model", "task_class", "outcome") if k in fm)
    print(f"logged {fm['units']} units -> {args.account} ({args.tier}"
          + (", THROTTLED" if args.throttled else "") + ")" + extra)
    return 0


def cmd_headroom(args: argparse.Namespace, transport: Any) -> int:
    text = transport.read(_atc_accounts_path(args.team))
    parsed = atc.parse_accounts(text)
    if not parsed["accounts"]:
        print("headroom — no accounts declared"
              + (f" ({parsed['error']})" if parsed.get("error") else "")
              + " — see fulcra-agent-atc §setup")
        return 0
    shards = _atc_usage_shards(transport, args.team)
    rows = atc.headroom(parsed["accounts"], shards, cli._now())
    if args.json:
        # Contract change (task 3): headroom --json now emits an OBJECT with the
        # per-window rows under "windows" plus a "demotions" list folded from the
        # outcome shards — the array top-level could not gain a sibling key.
        demo = [{"model": m, "task_class": tc, "bad": v["bad"], "of": v["of"]}
                for (m, tc), v in sorted(atc.demotions(shards).items())]
        print(json.dumps({"windows": rows, "demotions": demo}, indent=2))
        return 0
    print(f"headroom — {args.team}")
    for r in rows:
        flags = " THROTTLED(calibrate caps)" if r["throttled"] else ""
        print(f"  {r['account']:<20} {r['window_hours']:>4}h  "
              f"{r['headroom']}/{r['cap']} ({r['pct']}%){flags}")
    return 0


def _atc_models_overlay(text: Optional[str]) -> Optional[dict[str, Any]]:
    """Extract the optional top-level ``models`` overlay from accounts.json.

    Returns the overlay dict, or ``None`` when absent/malformed (v1 accounts.json
    has no ``models`` key -> defaults-only routing). Never raises."""
    if not text:
        return None
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    m = d.get("models") if isinstance(d, dict) else None
    return m if isinstance(m, dict) else None


def cmd_route(args: argparse.Namespace, transport: Any) -> int:
    text = transport.read(_atc_accounts_path(args.team))
    parsed = atc.parse_accounts(text)
    merged, merge_reports = atc.merge_models(
        atc.load_default_models(), _atc_models_overlay(text))
    needs = [n.strip() for n in (args.needs or "").split(",") if n.strip()]
    # An empty/whitespace-only --needs (e.g. `--needs ""` or `--needs ,`) is a
    # taxonomy-strictness error, not "match everything" — mirror the unknown-need
    # exit 2 rather than silently routing ALL models.
    if not needs:
        print("route — no needs given", file=sys.stderr)
        return 2
    shards = _atc_usage_shards(transport, args.team)
    # Fold outcome shards -> demoted (model, task_class) pairs, then adapt to the
    # {model: [tags]} shape route consumes (task_class values are taxonomy tags).
    demo_for_route = atc._demotions_for_route(atc.demotions(shards))
    result = atc.route(parsed, merged, needs, shards,
                       demotions=demo_for_route, now=cli._now())
    # Surface the overlay-merge notes alongside the fold's own coercion notes.
    result["dropped_unknown_tags"] = merge_reports + result.get("dropped_unknown_tags", [])
    role = getattr(args, "for_role", None)
    role_note = None
    if role:
        # the coordinator join: filter candidates to the role's bound account and
        # surface holder liveness -- never route into a void silently.
        bparsed = atc.parse_bindings(transport.read(_atc_bindings_path(args.team)))
        b = bparsed["bindings"].get(role)
        if not b:
            print(f"route -- no binding for role {role!r}: declare it in "
                  f"{_atc_bindings_path(args.team)}", file=sys.stderr)
            return 2
        result["candidates"] = [c for c in result["candidates"]
                                if c["account"] == b["account"]]
        if not result["candidates"] and not result.get("reason"):
            result["reason"] = (f"no headroom-bearing candidates on "
                                f"{b['account']} (binding for {role})")
        holders, ok = cli._role_fresh_holders(transport, args.team, role,
                                           now=cli._iso(cli._now()))
        role_note = (f"role {role}: UNKNOWN (lease fold degraded -- verify before dispatch)"
                     if not ok else
                     f"role {role}: HELD by {', '.join(holders)}" if holders else
                     f"role {role}: VACANT -- dispatch will wait for a holder")
        result["role"] = {"role": role, "account": b["account"],
                          "fresh_holders": holders if ok else None, "fold_ok": ok}
        if not ok or not holders:
            # FAIL CLOSED (codex P1): a VACANT/UNKNOWN role returns NO candidates
            # — a JSON consumer must never dispatch into a void off rank 1.
            result["candidates"] = []
            result["reason"] = f"role {role} is not HELD ({role_note})"
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"no candidates: {result['reason']}")
            return 1
    reason = result.get("reason")
    unknown_need = bool(reason) and reason.startswith("unknown need:")
    if args.json:
        print(json.dumps(result, indent=2))
        return 2 if unknown_need else 0
    if unknown_need:
        print(f"route — {reason} (needs must be one of: "
              f"{','.join(sorted(atc.TAXONOMY))})")
        return 2
    if not result["candidates"]:
        print(f"no candidates: {reason}")
        if role_note:
            print(role_note)
        return 0
    print(f"route — {args.team} — needs {','.join(needs)} "
          f"(map {result['map_version']})")
    for i, c in enumerate(result["candidates"], 1):
        pct = f"{c['headroom_pct']:g}"
        tags = ",".join(c["tags"])
        demo = f" [demoted: {', '.join(c['demoted'])}]" if c["demoted"] else ""
        print(f"{i}. {c['model']} — ({c['account']}) — {pct}% — {tags}{demo}")
    if role_note:
        print(role_note)
    return 0



def cmd_atc_harvest(args: argparse.Namespace, transport: Any) -> int:
    """Derive outcome shards from SETTLED review families on the bus, attributed
    via team/<team>/atc/bindings.json (agent/role -> account/tier[/model/
    task_class]). Self-reported outcome logging has empirically failed; the bus
    already records how work turned out — this folds it into the ledger.

    Idempotent by construction: each family writes ONE deterministic shard
    (``harvest-<base>.md``), so a re-run rewrites the same bytes (§7.10). Units
    are 0 — harvest feeds the demotion fold, never fakes headroom spend."""
    braw = transport.read(_atc_bindings_path(args.team))
    parsed = atc.parse_bindings(braw)
    for d in parsed["dropped"]:
        print(f"harvest — dropped binding: {d}", file=sys.stderr)
    if parsed.get("error"):
        print(f"harvest — {parsed['error']}", file=sys.stderr)
        return 1
    if not parsed["bindings"]:
        print(f"harvest — no bindings declared: write {_atc_bindings_path(args.team)} "
              "({\"bindings\": [{\"agent\": ..., \"account\": ..., \"tier\": ...}]})")
        return 0
    # review root listing: dirs are slugs; docs are <slug>.md (tombstones excluded
    # by requiring the doc to exist and read).
    entries = transport.list_dir(f"team/{args.team}/review/")
    slugs = sorted({e["name"][:-3] for e in entries
                    if e.get("name", "").endswith(".md")})
    already = {e.get("name", "") for e in
               transport.list_dir(_atc_usage_prefix(args.team))}
    written = skipped = 0
    unattributed: list[str] = []
    for base, rounds in sorted(atc.review_families(slugs).items()):
        shard_name = f"harvest-{base}.md"
        latest = rounds[-1]
        # settled marker is the terminal-APPROVED signal; unsettled families wait.
        names = {e.get("name") for e in
                 transport.list_dir(cli._verdicts_prefix(args.team, latest))}
        if cli.SETTLED_MARKER not in names:
            continue
        doc = transport.read(cli._review_doc_path(args.team, latest))
        fm_doc = okf.parse_frontmatter(doc) if doc else None
        author = fm_doc.get("requested_by") if fm_doc else None
        b = parsed["bindings"].get(author or "")
        if not b:
            unattributed.append(f"{base} (requested_by={author or '?'})")
            continue
        outcome = atc.family_outcome(rounds)
        if shard_name in already:
            # CONVERGENT, not write-once: a later settled round must flip a
            # previously-harvested clean family to rework. Rewrite only when the
            # derived outcome/round-count changed; unchanged families skip.
            prior = okf.parse_frontmatter(
                transport.read(_atc_usage_prefix(args.team) + shard_name)) or {}
            if (prior.get("outcome") == outcome
                    and str(prior.get("rounds")) == str(len(rounds))):
                skipped += 1
                continue
        fm = {"schema": "atc-usage/v1", "agent": "atc-harvest", "ts": cli._iso(cli._now()),
              "account": b["account"], "tier": b["tier"], "units": 0,
              "throttled": False, "outcome": outcome, "rounds": len(rounds),
              "harvest_source": base}
        if b.get("model"):
            fm["model"] = b["model"]
        if b.get("task_class"):
            fm["task_class"] = b["task_class"]
        if not transport.write(_atc_usage_prefix(args.team) + shard_name,
                               okf.render_frontmatter(fm) + "\n"):
            print(f"harvest — write failed for {base} (transport)", file=sys.stderr)
            return 1
        written += 1
    for u in unattributed:
        print(f"harvest — no binding for author of {u}; add them to bindings.json")
    print(f"harvest — {args.team}: {written} shard(s) written, {skipped} already "
          f"harvested, {len(unattributed)} unattributed")
    return 0


def cmd_atc_report(args: argparse.Namespace, transport: Any) -> int:
    """Team dispatch/tier/calibration report over the trailing --days window.

    Reads the same accounts.json + usage shards the other ATC verbs use, folds
    the demotions (calibration) and merged model map alongside, and renders the
    estimate-labelled text block. Never crashes on an empty/corrupt ledger."""
    text = transport.read(_atc_accounts_path(args.team))
    parsed = atc.parse_accounts(text)
    shards = _atc_usage_shards(transport, args.team)
    merged, _ = atc.merge_models(atc.load_default_models(),
                                 _atc_models_overlay(text))
    rep = atc.report_fold(parsed, shards, team=args.team,
                          demotions=atc.demotions(shards), models=merged,
                          days=args.days, now=cli._now())
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    print(atc.render_report(rep))
    return 0


# Plan-seeded rolling-window cap defaults for `atc init`. These are OPERATOR-
# CORRECTABLE ESTIMATES, not measured limits — subscriptions don't publish their
# caps, so init seeds a plausible starting point per provider and throttle events
# calibrate them from there (a real rate-limit hit zeroes that window regardless
# of the declared number). An operator edits the numbers freely, and DELETING an
# account's windows declares it uncapped (route treats no-windows as 100%
# headroom). Keyed by provider; anything else falls to the placeholder.
_ATC_SEED_WINDOWS: dict[str, list[dict[str, int]]] = {
    "anthropic": [{"hours": 5, "cap": 1000}, {"hours": 168, "cap": 15000}],
    "openai": [{"hours": 5, "cap": 600}],
}
_ATC_SEED_WINDOWS_DEFAULT: list[dict[str, int]] = [{"hours": 5, "cap": 500}]


def _atc_seed_windows(provider: str) -> list[dict[str, int]]:
    """Fresh copies (never the shared constant) so callers can't mutate defaults."""
    src = _ATC_SEED_WINDOWS.get(provider, _ATC_SEED_WINDOWS_DEFAULT)
    return [dict(w) for w in src]


def _atc_provider_harnesses(defaults: dict[str, Any]) -> dict[str, list[str]]:
    """Per-provider harness union folded from the default model map: every model's
    ``provider`` -> the sorted set of its declared ``harnesses``. This is the
    default an account's ``harnesses[]`` seeds from at init time."""
    acc: dict[str, set[str]] = {}
    for entry in (defaults.get("models") or {}).values():
        prov = entry.get("provider")
        if not isinstance(prov, str) or not prov:
            continue
        for h in entry.get("harnesses") or []:
            if isinstance(h, str) and h:
                acc.setdefault(prov, set()).add(h)
    return {p: sorted(hs) for p, hs in acc.items()}


def _atc_parse_account_spec(spec: str) -> Optional[tuple[str, str, str]]:
    """Parse a ``--account id=provider:plan`` token. ``:plan`` is optional. Returns
    ``(id, provider, plan)`` or ``None`` if the required ``id=provider`` shape is
    absent (the caller turns ``None`` into an exit-2 refusal)."""
    if "=" not in spec:
        return None
    acct_id, rest = spec.split("=", 1)
    acct_id = acct_id.strip()
    if not acct_id or not rest.strip():
        return None
    if ":" in rest:
        provider, plan = rest.split(":", 1)
    else:
        provider, plan = rest, ""
    provider, plan = provider.strip(), plan.strip()
    if not provider:
        return None
    return acct_id, provider, plan


def _atc_build_account(acct_id: str, provider: str, plan: str,
                       prov_harnesses: dict[str, list[str]],
                       harness_override: Optional[list[str]]) -> dict[str, Any]:
    harnesses = (list(harness_override) if harness_override
                 else list(prov_harnesses.get(provider, [])))
    if provider not in prov_harnesses and not harness_override:
        print(f"warning: provider {provider!r} not in default map; seeded "
              "5h/500 with no harnesses — pass --harness or edit accounts.json "
              "to make it routable", file=sys.stderr)
    acct: dict[str, Any] = {"id": acct_id, "provider": provider}
    if plan:
        acct["plan"] = plan
    acct["harnesses"] = harnesses
    acct["windows"] = _atc_seed_windows(provider)
    return acct


def _atc_init_interactive(providers: list[str],
                          prov_harnesses: dict[str, list[str]],
                          harness_override: Optional[list[str]]) -> list[dict[str, Any]]:
    """Numbered-prompt onboarding over the default map's provider set. Reads via
    the builtin ``input`` (monkeypatched in tests). An empty/blank selection
    returns ``[]`` — the caller refuses zero accounts with exit 2."""
    print("Providers in the packaged default model map:")
    for i, p in enumerate(providers, 1):
        print(f"  {i}. {p}")
    sel = input("Select providers to declare (comma-separated numbers): ").strip()
    chosen: list[str] = []
    ignored: list[str] = []
    for tok in sel.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            idx = int(tok)
        except ValueError:
            ignored.append(tok)
            continue
        if not (1 <= idx <= len(providers)):
            ignored.append(tok)
            continue
        if providers[idx - 1] not in chosen:
            chosen.append(providers[idx - 1])
    if ignored:
        print("ignored: " + ", ".join(ignored))
    gathered: list[dict[str, Any]] = []
    for prov in chosen:
        default_id = f"{prov}-main"
        acct_id = input(f"  account id for {prov} [{default_id}]: ").strip() or default_id
        plan = input(f"  plan for {prov} (blank for none): ").strip()
        gathered.append(_atc_build_account(acct_id, prov, plan,
                                           prov_harnesses, harness_override))
    return gathered


def cmd_atc_init(args: argparse.Namespace, transport: Any) -> int:
    """Standalone ATC onboarding: seed ``team/<team>/atc/accounts.json`` so a
    fresh operator has a routable cap ledger in one command.

    Interactive by default (numbered prompts over the default map's providers);
    ``--yes`` runs non-interactively and requires >=1 ``--account id=provider:plan``.
    Idempotent: an existing accounts.json is loaded, the newly-declared accounts
    merged in by id (existing entries and sibling keys like ``tiers``/``models``
    are preserved), and the result written back through the same transport-write
    seam the review-request flow uses. Refuses a zero-account run with exit 2."""
    defaults = atc.load_default_models()
    prov_harnesses = _atc_provider_harnesses(defaults)
    providers = sorted(prov_harnesses)

    # --account is itself an unambiguous statement of non-interactive intent, so
    # its presence implies --yes even when --yes was not passed.
    if args.yes or args.account:
        gathered: list[dict[str, Any]] = []
        for spec in (args.account or []):
            parsed = _atc_parse_account_spec(spec)
            if parsed is None:
                print(f"atc init: malformed --account {spec!r} "
                      "(expected id=provider:plan)", file=sys.stderr)
                return 2
            gathered.append(_atc_build_account(*parsed, prov_harnesses, args.harness))
    else:
        gathered = _atc_init_interactive(providers, prov_harnesses, args.harness)

    if not gathered:
        print("atc init: no accounts declared — nothing written "
              "(--yes needs >=1 --account id=provider:plan)", file=sys.stderr)
        return 2

    # Idempotent merge: load whatever exists, keep it verbatim, append only the
    # new-by-id accounts. Read the raw doc (not parse_accounts) so sibling keys
    # (tiers, models overlay) survive untouched.
    path = _atc_accounts_path(args.team)
    raw = transport.read(path)
    try:
        doc = json.loads(raw) if raw else {}
        if not isinstance(doc, dict):
            doc = {}
    except (ValueError, TypeError):
        doc = {}
    existing = doc.get("accounts")
    if not isinstance(existing, list):
        existing = []
    existing_ids = {a.get("id") for a in existing if isinstance(a, dict)}
    added = [a for a in gathered if a["id"] not in existing_ids]
    doc["accounts"] = existing + added
    doc.setdefault("tiers", {})

    transport.write(path, json.dumps(doc, indent=2) + "\n")

    ex_id = gathered[0]["id"]
    print(f"wrote {path}: {len(doc['accounts'])} account(s) declared "
          f"({len(added)} new this run)")
    print("next steps — paste these:")
    print("  1. install the skill — see skills/fulcra-agent-atc/SKILL.md §Install")
    print(f"  2. coord-engine route {args.team} --needs code")
    print(f"  3. coord-engine usage log {args.team} --account {ex_id} "
          "--tier standard --units <est> --model <model> "
          "--task-class code --outcome clean")
    return 0


