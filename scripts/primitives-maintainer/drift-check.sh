#!/bin/bash
# fulcra-primitives-maintainer — daily drift detector.
#
# Fingerprints the agent-facing Fulcra surface and compares it to a stored
# baseline. On drift (or on an unusable probe) it posts to the coord team bus
# (team fulcra) as fulcra-primitives-maintainer and writes an ALERT file so a
# Claude session does the actual FULCRA-PRIMITIVES.md rewrite. Detection is
# unattended; the rewrite (model judgment) is not.
#
# WHAT IT OBSERVES (see README for the full claim/limit table):
#   pypi_version    published fulcra-api version on PyPI.
#   cli_verbs       top-level verb list of the PUBLISHED CLI, parsed from
#                   `fulcra-api --help` of that exact version. This is the
#                   fingerprint that carries the documented full-rewrite
#                   trigger: `record` and `delete` are top-level verbs, so a
#                   record/delete verb landing (or vanishing) moves this.
#   cli_groups      subcommands of every top-level verb that is a group
#                   (auth, data-type, file, share, tag, + anything new —
#                   groups are discovered, not hardcoded).
#   cli_beta_verbs  verbs visible only under `--beta`.
#   spec_hash       sha of the FULL path->methods map of the published
#                   OpenAPI spec + DataRecordV1 presence.
#   mcp_scopes      OAuth `scopes_supported` from MCP discovery.
#   cli_head        fulcra-api-python main HEAD sha (pre-release early warning).
#
# WHAT IT CANNOT OBSERVE (do not read a clean run as covering these):
#   - MCP TOOL LIST. mcp_scopes is OAuth scopes only. A new MCP write tool
#     under an existing scope does not move it. Nothing here sees tools/list
#     (it needs an authenticated session). weekly-review.sh's human-eyes pass
#     is the only coverage.
#   - DATASHARE ENDPOINTS. The datashare REST surface behind the `share` verb
#     is not published in openapi.json at all (verified 2026-07-16: 53 paths,
#     zero datashare paths), so spec_hash cannot see it. The CLI `share` group
#     in cli_groups is the only coverage.
#   - Docs prose, semantics behind an unchanged signature, and anything the
#     CLI/spec do not expose. weekly-review.sh covers the first.
#
# FAIL-CLOSED. Every probe result is one of exactly two things: a real observed
# value, or UNKNOWN. There is no third category, and in particular no default
# that type-checks as data — no `or []`, no omitted key, no empty string that a
# comparison would read as a legitimate state. A probe that cannot answer is
# UNKNOWN -> alert, never "no drift", and UNKNOWN never advances the baseline.
# Every probe is additionally checked against a positive control before its
# result is trusted, because an absence of drift means nothing unless the probe
# could have found some. The failure this rebuild exists to fix (2026-07-16) was
# a fingerprint structurally incapable of producing a hit.
#
# The sub-probes obey the same rule as the probes. A failed group `--help` does
# not mean "that group has no subcommands", and a failed `--beta --help` does
# not mean "there is no beta surface" — today the beta surface is legitimately
# empty, so an `or []` there produces the exact value a healthy run produces.
# Any sub-probe failure rejects the WHOLE CLI fingerprint.
#
# Runs daily via launchd: com.fulcra.primitives-maintainer.plist
set -uo pipefail

# ROOT = the maintainer checkout root (two levels up from scripts/primitives-maintainer/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Overridable so tests/dry-runs never touch the live baseline.
STATE="${PRIMITIVES_STATE_DIR:-$ROOT/.primitives-state}"
BASELINE="$STATE/baseline.json"
# The outstanding-DOC-WORK marker. Written on drift, cleared only by a session
# that did the rewrite. Nothing in this script may truncate it — see UNKNOWN_MARK.
ALERT="$STATE/DRIFT-ALERT.txt"
# The outstanding-PROBE-FAILURE marker. Separate file on purpose: an UNKNOWN run
# is a different debt (fix the probe) with a different owner and a different
# discharge condition (the probe answering again). Writing probe-failure text
# over DRIFT-ALERT.txt would destroy an unactioned "WHAT CHANGED" payload, and
# the rewrite that was actually owed would never be seen.
UNKNOWN_MARK="$STATE/PROBE-UNKNOWN.txt"
LOG="$STATE/drift-check.log"
# Overridable so a test run can capture the notification instead of posting it.
CE="${PRIMITIVES_COORD_ENGINE:-$(command -v coord-engine || echo "$HOME/.local/bin/coord-engine")}"
TEAM="fulcra"
AGENT="claude-code:Mac:fulcra-primitives-maintainer"

SPEC_URL="${PRIMITIVES_SPEC_URL:-https://api.fulcradynamics.com/openapi.json}"
MCP_URL="${PRIMITIVES_MCP_URL:-https://mcp.fulcradynamics.com/.well-known/oauth-authorization-server}"
PYPI_URL="${PRIMITIVES_PYPI_URL:-https://pypi.org/pypi/fulcra-api/json}"
# Set to 1 on hosts with no gh auth. An explicit, visible opt-out — an absent
# probe must be a decision on the record, not a silent pass.
SKIP_GH="${PRIMITIVES_SKIP_GH:-0}"
# Sentinel verbs that must be present in any correctly-parsed CLI help. If
# these are missing the parser is broken or the package did not run; that is
# UNKNOWN, not "the CLI lost auth".
CLI_SENTINELS="${PRIMITIVES_CLI_SENTINELS:-auth,user-info,catalog}"
# Sentinel GROUPS that must come back with at least one observed subcommand.
# Without this, a group whose help probe is broken is indistinguishable from a
# group that genuinely has no subcommands, forever. `auth` (login,
# print-access-token) is the oldest, most stable group; one is enough to prove
# the group machinery can produce a hit.
CLI_GROUP_SENTINELS="${PRIMITIVES_CLI_GROUP_SENTINELS:-auth}"
# Base command for the CLI probe. Defaults to running the published package off
# PyPI at the exact version observed above. Overridable so tests can drive a
# stub CLI without a network round-trip.
CLI_CMD="${PRIMITIVES_CLI_CMD:-}"
# Sentinel path that must be present in any correctly-fetched spec.
SPEC_SENTINEL="/user/v1alpha1/annotation"
# Sentinel scope that must be present in any correctly-fetched MCP discovery
# doc. Set empty to disable.
MCP_SENTINEL="${PRIMITIVES_MCP_SENTINEL:-openid}"

mkdir -p "$STATE"
ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "$(ts)  $*" >> "$LOG"; }

TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT
SPEC="$TMPDIR_RUN/openapi.json"

# --- probe: PyPI published version ---------------------------------------
PYPI_VER="$(curl -s --max-time 20 "$PYPI_URL" 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null \
  || true)"
[ -n "$PYPI_VER" ] || PYPI_VER="UNKNOWN"

# --- probe: published CLI surface ----------------------------------------
# Fingerprints what an agent actually gets from `uv tool install fulcra-api`,
# NOT a decorator grep against one file in git main. The old fingerprint
# counted `@data_type.command` in fulcra_api/cli/data_types.py, which could
# not see `record`/`delete` (top-level, different file, no such decorator) —
# it missed 0.1.37 and 0.1.36 outright.
#
# Every sub-probe here returns an observation or raises. Nothing defaults. The
# probe emits exactly one line: "OK <json>" or "FAIL <reason> | <reason>".
CLI_JSON="UNKNOWN"
CLI_PROBE_ERR=""
if [ "$PYPI_VER" = "UNKNOWN" ]; then
  CLI_PROBE_ERR="no published version observed, so there is no version to probe"
elif [ -z "$CLI_CMD" ] && ! command -v uvx >/dev/null 2>&1; then
  CLI_PROBE_ERR="uvx not on PATH (needed to run the published package)"
else
  CLI_OUT="$(PYPI_VER="$PYPI_VER" CLI_CMD="$CLI_CMD" python3 - <<'PY' 2>/dev/null
import json, os, re, shlex, subprocess

ver = os.environ["PYPI_VER"]
override = os.environ.get("CLI_CMD", "").strip()
BASE = shlex.split(override) if override else [
    "uvx", "-q", "--from", "fulcra-api==%s" % ver, "fulcra-api"]


class ProbeFailure(Exception):
    """A command that was expected to answer did not. Never a value."""


def label(args):
    return " ".join(args) if args else "<top-level>"


def tail(*streams):
    for s in streams:
        s = (s or "").strip()
        if s:
            return s.splitlines()[-1][:200]
    return "no output"


def run_help(args):
    try:
        r = subprocess.run(BASE + args + ["--help"], capture_output=True,
                           text=True, timeout=120)
    except Exception as e:
        raise ProbeFailure("%s: could not run --help (%s)" % (label(args), e))
    return r.stdout, r.stderr, r.returncode


def help_text(args):
    """stdout of a successful --help. A nonzero exit is a failed probe, never
    an observation: click exits 0 for --help on every real command, leaf or
    group. Treating a failure as 'no output' is how a dead probe starts
    reading as an empty surface."""
    out, err, rc = run_help(args)
    if rc != 0:
        raise ProbeFailure("%s: --help exited %d (%s)"
                           % (label(args), rc, tail(err, out)))
    return out


def verbs(text, what):
    """Parse a click 'Commands:' block.

    Returns None when there is no 'Commands:' block at all — that is a real
    observation: a leaf command legitimately has no subcommands.
    Raises when a block exists but yields no command lines: that is the parser
    failing, and an empty list would be indistinguishable from a real surface.
    """
    if "Commands:" not in text:
        return None
    block = text.split("Commands:", 1)[1]
    out = []
    for line in block.splitlines():
        m = re.match(r"^ {2}(\S+)", line)
        if m:
            out.append(m.group(1))
        elif line.strip() and not line.startswith(" "):
            break
    if not out:
        raise ProbeFailure(
            "%s: 'Commands:' block present but no command lines parsed" % what)
    return sorted(set(out))


def beta_verbs(top):
    """Verbs visible only under --beta.

    Note the trap: the beta surface is CURRENTLY EMPTY on a healthy run, so a
    failed probe defaulting to [] produces byte-for-byte the value a healthy
    probe produces. It would compare clean forever. Hence: an empty list is
    returned only when --beta actually ran and actually parsed.
    A build with no --beta gate at all is a real observation too, but a
    DIFFERENT one, so it gets its own value rather than collapsing into [].
    """
    out, err, rc = run_help(["--beta"])
    if rc != 0:
        blob = (err or "") + (out or "")
        if re.search(r"no such option", blob, re.I) and "--beta" in blob:
            return "NO_BETA_FLAG"
        raise ProbeFailure("--beta: --help exited %d (%s)" % (rc, tail(err, out)))
    sub = verbs(out, "--beta")
    if sub is None:
        raise ProbeFailure("--beta: --help has no 'Commands:' block")
    return sorted(set(sub) - set(top))


errors = []
result = {}
try:
    top = verbs(help_text([]), "<top-level>")
    if top is None:
        raise ProbeFailure("<top-level>: --help has no 'Commands:' block")
    result["cli_verbs"] = top

    # A group whose probe dies is NOT a group with no subcommands. Collect every
    # failure (better diagnostics), then reject the whole fingerprint below.
    groups = {}
    for v in top:
        try:
            sub = verbs(help_text([v]), v)
        except ProbeFailure as e:
            errors.append(str(e))
            continue
        if sub is not None:
            groups[v] = sub
    result["cli_groups"] = groups

    try:
        result["cli_beta_verbs"] = beta_verbs(top)
    except ProbeFailure as e:
        errors.append(str(e))
except ProbeFailure as e:
    errors.append(str(e))
except Exception as e:
    errors.append("unexpected probe error: %r" % (e,))

# Partial observation is not observation. One dead sub-probe rejects all of it.
if errors:
    print("FAIL " + " | ".join(errors))
else:
    print("OK " + json.dumps(result, sort_keys=True))
PY
)"
  case "$CLI_OUT" in
    "OK "*)   CLI_JSON="${CLI_OUT#OK }" ;;
    "FAIL "*) CLI_PROBE_ERR="${CLI_OUT#FAIL }" ;;
    *)        CLI_PROBE_ERR="probe produced no usable output (python3 missing, or it crashed before reporting)" ;;
  esac
fi
[ -n "$CLI_JSON" ] || CLI_JSON="UNKNOWN"

# --- probe: OpenAPI spec --------------------------------------------------
curl -s --max-time 20 "$SPEC_URL" -o "$SPEC" 2>/dev/null
SPEC_OUT="$(python3 - "$SPEC" <<'PY' 2>/dev/null || echo "UNKNOWN {}"
import sys, json, hashlib
try:
    d = json.load(open(sys.argv[1]))
    paths = d["paths"]
    if not paths:
        raise ValueError("empty paths")
except Exception:
    print("UNKNOWN {}")
    sys.exit(0)
# Full path->methods map, not a hand-picked subset. The old sig only looked at
# paths containing "annotation" and paths under /ingest/, plus a path_count
# that does not move when a path is swapped 1-for-1 — so the catalog-schema
# endpoints moved without moving the fingerprint. Any added, removed, or
# re-methoded endpoint moves this hash.
sig = {
    "paths": {p: sorted(paths[p].keys()) for p in sorted(paths)},
    "path_count": len(paths),
    "has_DataRecordV1": "DataRecordV1" in d.get("components", {}).get("schemas", {}),
}
print(hashlib.sha256(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:16]
      + " " + json.dumps(sig, sort_keys=True))
PY
)"
SPEC_HASH="${SPEC_OUT%% *}"; SPEC_SIG="${SPEC_OUT#* }"

# --- probe: MCP OAuth scopes ---------------------------------------------
# OAuth scopes ONLY. This cannot see the MCP tool list; a new MCP write tool
# under an existing scope will not move it. The old header claimed this
# fingerprinted "MCP discovery" — it does not.
MCP_SCOPES="$(curl -s --max-time 12 "$MCP_URL" 2>/dev/null \
  | python3 -c 'import sys,json;print(",".join(json.load(sys.stdin).get("scopes_supported",[])))' 2>/dev/null \
  || true)"
[ -n "$MCP_SCOPES" ] || MCP_SCOPES="UNKNOWN"

# --- probe: fulcra-api-python main HEAD ----------------------------------
# Early warning only: the CLI ships to PyPI ahead of git main, so this is not
# the release signal. pypi_version + cli_verbs are.
if [ "$SKIP_GH" = "1" ]; then
  CLI_HEAD="disabled"
else
  CLI_HEAD="$(gh api repos/fulcradynamics/fulcra-api-python/commits/main --jq '.sha[:7]' 2>/dev/null || true)"
  [ -n "$CLI_HEAD" ] || CLI_HEAD="UNKNOWN"
fi

# --- positive control ------------------------------------------------------
# Prove each probe CAN produce a hit before trusting an absence of drift.
UNKNOWN_REASONS=()
[ "$PYPI_VER" = "UNKNOWN" ] && UNKNOWN_REASONS+=("pypi_version: fetch/parse failed ($PYPI_URL)")
[ "$SPEC_HASH" = "UNKNOWN" ] && UNKNOWN_REASONS+=("spec_hash: fetch/parse failed or empty paths ($SPEC_URL)")
[ "$MCP_SCOPES" = "UNKNOWN" ] && UNKNOWN_REASONS+=("mcp_scopes: fetch/parse failed ($MCP_URL)")
[ "$CLI_HEAD" = "UNKNOWN" ] && UNKNOWN_REASONS+=("cli_head: gh api failed (set PRIMITIVES_SKIP_GH=1 to opt out explicitly)")
if [ "$CLI_JSON" = "UNKNOWN" ]; then
  UNKNOWN_REASONS+=("cli surface: ${CLI_PROBE_ERR:-could not run/parse published fulcra-api --help}")
else
  # The control covers the WHOLE fingerprint, not just the top-level verbs. A
  # healthy top-level parse next to a dead group/beta probe was the hole: it
  # could be written as a first-run baseline, or compare clean forever.
  CLI_CONTROL="$(CLI_JSON="$CLI_JSON" SENT="$CLI_SENTINELS" GSENT="$CLI_GROUP_SENTINELS" python3 -c '
import json, os

probs = []
try:
    d = json.loads(os.environ["CLI_JSON"])
except Exception:
    print("cli fingerprint is not parseable JSON"); raise SystemExit(0)

verbs, groups, beta = d.get("cli_verbs"), d.get("cli_groups"), d.get("cli_beta_verbs")

if not isinstance(verbs, list) or not verbs:
    probs.append("cli_verbs is not a non-empty list")
else:
    missing = sorted(s for s in os.environ["SENT"].split(",") if s and s not in verbs)
    if missing:
        probs.append("sentinel top-level verbs missing: " + ",".join(missing))

if not isinstance(groups, dict):
    probs.append("cli_groups is not a mapping")
else:
    for g in os.environ["GSENT"].split(","):
        if not g:
            continue
        sub = groups.get(g)
        if not isinstance(sub, list) or not sub:
            probs.append("sentinel group %s came back with no observed subcommands "
                         "(a group whose probe fails must never read as an empty group)" % g)

if not (beta == "NO_BETA_FLAG" or isinstance(beta, list)):
    probs.append("cli_beta_verbs is neither an observed list nor NO_BETA_FLAG")

print("; ".join(probs))
' 2>/dev/null || echo "control script itself failed")"
  [ -n "$CLI_CONTROL" ] && UNKNOWN_REASONS+=("cli surface: positive control failed — $CLI_CONTROL")
fi
if [ "$SPEC_HASH" != "UNKNOWN" ] && ! grep -q "\"$SPEC_SENTINEL\"" "$SPEC" 2>/dev/null; then
  UNKNOWN_REASONS+=("spec_hash: positive control failed — sentinel path $SPEC_SENTINEL absent from fetched spec")
fi
if [ "$MCP_SCOPES" != "UNKNOWN" ] && [ -n "$MCP_SENTINEL" ] \
   && ! printf '%s' ",$MCP_SCOPES," | grep -q ",$MCP_SENTINEL,"; then
  UNKNOWN_REASONS+=("mcp_scopes: positive control failed — sentinel scope $MCP_SENTINEL absent from fetched discovery doc ($MCP_SCOPES)")
fi

# --- assemble fingerprint --------------------------------------------------
CUR="$(CLI_JSON="$CLI_JSON" python3 -c '
import json, os, sys
cli = os.environ["CLI_JSON"]
try:
    cli = json.loads(cli)
except Exception:
    cli = {"cli_verbs": "UNKNOWN", "cli_groups": "UNKNOWN", "cli_beta_verbs": "UNKNOWN"}
d = {
    "spec_hash": sys.argv[1],
    "cli_head": sys.argv[2],
    "mcp_scopes": sys.argv[3],
    "pypi_version": sys.argv[4],
}
d.update(cli)
print(json.dumps(d, sort_keys=True))
' "$SPEC_HASH" "$CLI_HEAD" "$MCP_SCOPES" "$PYPI_VER")"

# --- notify helper ---------------------------------------------------------
# `tell` drops a message (rc 1, "already exists") when a slug prefix collides,
# so the title carries a timestamp and the rc is checked and logged.
notify() {
  local prio="$1" title="$2" summary="$3" out rc
  out="$("$CE" tell "$TEAM" "$AGENT" "$title" --from "$AGENT" \
        --workstream fulcra-primitives --priority "$prio" --summary "$summary" 2>&1)"
  rc=$?
  if [ $rc -ne 0 ]; then
    log "TELL FAILED rc=$rc: $out"
  else
    log "tell ok: $title"
  fi
  return $rc
}

# --- UNKNOWN: fail closed --------------------------------------------------
# This branch writes UNKNOWN_MARK, never ALERT. An UNKNOWN run observed nothing,
# so it has nothing to say about a drift that a previous run DID observe. The
# earlier "WHAT CHANGED" payload is the only record of doc work that is owed;
# overwriting it with probe-failure text discharges that debt silently — the
# probe gets fixed, the marker gets removed, and the rewrite never happens.
if [ ${#UNKNOWN_REASONS[@]} -gt 0 ]; then
  {
    echo "DRIFT CHECK UNKNOWN $(ts)"
    echo "One or more probes could not answer. This is NOT 'no drift' — the"
    echo "surface was not observed. Baseline NOT advanced; this run will repeat"
    echo "until the probes work or the fingerprint is fixed."
    echo
    printf '%s\n' "${UNKNOWN_REASONS[@]}"
    echo
    echo "PARTIAL: $CUR"
    if [ -f "$ALERT" ]; then
      echo
      echo "NOTE: $ALERT is ALSO outstanding — an earlier run observed real drift"
      echo "that has not been actioned. This file does not replace or clear it."
      echo "Both debts are open: fix the probe (this file) AND do the rewrite."
    fi
    echo
    echo "Clear this file (rm $UNKNOWN_MARK) only by making the probe answer; the"
    echo "next successful run clears it for you."
  } > "$UNKNOWN_MARK"
  log "UNKNOWN: ${UNKNOWN_REASONS[*]}"
  OUTSTANDING_NOTE=""
  [ -f "$ALERT" ] && OUTSTANDING_NOTE=" SEPARATELY: $ALERT is still outstanding from an earlier real drift and is untouched by this run — that rewrite is still owed."
  notify P1 "UNKNOWN: Fulcra primitives drift check could not observe the surface ($(ts))" \
    "$(printf '%s; ' "${UNKNOWN_REASONS[@]}")Baseline NOT advanced — a probe that cannot answer is not a clean run. Fix the probe or the fingerprint, then re-run scripts/primitives-maintainer/drift-check.sh. Probe-failure file: $UNKNOWN_MARK.$OUTSTANDING_NOTE"
  exit 2
fi

# Probes answered. That discharges the probe-failure debt and only that debt:
# UNKNOWN_MARK describes a condition that no longer holds. ALERT describes a doc
# rewrite that is owed regardless of whether the probes are healthy today, so it
# is deliberately not touched here — only a session that did the rewrite rm's it.
if [ -f "$UNKNOWN_MARK" ]; then
  log "probes recovered; clearing $UNKNOWN_MARK (ALERT, if present, left alone)"
  rm -f "$UNKNOWN_MARK"
fi

# --- first run: write baseline, done ---------------------------------------
if [ ! -f "$BASELINE" ]; then
  echo "$CUR" > "$BASELINE"
  log "baseline written: $CUR"
  exit 0
fi

PREV="$(cat "$BASELINE")"

# --- compare ---------------------------------------------------------------
# NOTE on the `or {}` / `.get(k, [])` defaults below: they are describing, not
# deciding. The drift DECISION is exact string equality on the whole fingerprint
# ("$CUR" = "$PREV") and is computed independently of this renderer, and a
# renderer that produces nothing is caught by the DIFF RENDERER FAILED guard,
# which blocks rebaselining. So a default here can only mis-word an alert that is
# already firing; it cannot manufacture a clean comparison. Do not copy this
# pattern into a probe, where the same idiom IS the bug.
DIFF="$(PREV="$PREV" CUR="$CUR" python3 - <<'PY'
import json, os

prev = json.loads(os.environ["PREV"])
cur = json.loads(os.environ["CUR"])
lines = []

def listdiff(label, a, b):
    if not isinstance(a, list) or not isinstance(b, list):
        if a != b:
            lines.append(f"{label}: {a!r} -> {b!r}")
        return
    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    if added:
        lines.append(f"{label}: ADDED {', '.join(added)}")
    if removed:
        lines.append(f"{label}: REMOVED {', '.join(removed)}")

for k in ("pypi_version", "cli_head", "spec_hash", "mcp_scopes"):
    if prev.get(k) != cur.get(k):
        lines.append(f"{k}: {prev.get(k)} -> {cur.get(k)}")
listdiff("cli_verbs", prev.get("cli_verbs"), cur.get("cli_verbs"))
listdiff("cli_beta_verbs", prev.get("cli_beta_verbs"), cur.get("cli_beta_verbs"))

pg, cg = prev.get("cli_groups") or {}, cur.get("cli_groups") or {}
if isinstance(pg, dict) and isinstance(cg, dict):
    for g in sorted(set(pg) | set(cg)):
        listdiff(f"cli_groups[{g}]", pg.get(g, []), cg.get(g, []))
else:
    listdiff("cli_groups", pg, cg)

print("\n".join(lines) or "fingerprint changed but no field diff could be rendered")
PY
)"
# A drift we cannot describe is not a drift we may rebaseline away.
if [ -z "$DIFF" ]; then
  DIFF="DIFF RENDERER FAILED — fingerprint changed but the change could not be characterized"
fi

# Name the trigger explicitly when a write verb moves, so nobody is routed to
# the wrong file. The 2026-07-16 miss sent an agent to data_types.py to find a
# `record` verb that lives one directory up.
# Beta-gated too: a record/delete verb landing behind --beta is the same doc
# event as one landing in the default surface, and tier-2 guidance shifts either
# way. Only cli_groups is excluded — a subcommand named `delete` under `tag` is
# routine and not the documented trigger.
TRIGGER="$(printf '%s\n' "$DIFF" | grep -E '^cli_(verbs|beta_verbs): (ADDED|REMOVED).*\b(record|delete)\b' || true)"

if [ "$CUR" = "$PREV" ]; then
  if [ -f "$ALERT" ]; then
    # An earlier drift is still unactioned. A clean fingerprint does not clear
    # the debt: rebaselining silences the field, it does not do the rewrite.
    log "no new drift, but UNACKED alert outstanding: $ALERT"
    notify P1 "OUTSTANDING: Fulcra primitives drift still unactioned ($(ts))" \
      "No new drift vs baseline, but $ALERT is still present — a previous drift has not been actioned. Do the FULCRA-PRIMITIVES.md rewrite, then \`rm $ALERT\` to clear. Alert contents: $(head -c 800 "$ALERT" | tr '\n' ' ')"
    exit 1
  fi
  log "no drift (pypi=$PYPI_VER spec=$SPEC_HASH cli_head=$CLI_HEAD)"
  exit 0
fi

# --- drift -----------------------------------------------------------------
# APPEND. A second drift before the first is actioned must not overwrite the
# first — same defect as the UNKNOWN branch had: the baseline has already moved
# past the earlier change, so this file is the only surviving record of it. Every
# unactioned "WHAT CHANGED" stays until a session clears the file.
if [ -f "$ALERT" ]; then
  {
    echo
    echo "==================================================================="
    echo "ADDITIONAL DRIFT — everything above is STILL UNACTIONED. The"
    echo "baseline has already moved past it, so this file is the only record."
    echo "==================================================================="
  } >> "$ALERT"
fi
{
  echo "DRIFT DETECTED $(ts)"
  echo
  echo "WHAT CHANGED:"
  printf '%s\n' "$DIFF"
  if [ -n "$TRIGGER" ]; then
    echo
    echo "*** FULL-REWRITE TRIGGER: a top-level record/delete verb moved in the"
    echo "*** published CLI (fulcra-api $PYPI_VER). These are TOP-LEVEL verbs —"
    echo "*** do not go looking for them in fulcra_api/cli/data_types.py."
  fi
  echo
  echo "PREV: $PREV"
  echo "NOW:  $CUR"
  echo "spec_sig: $SPEC_SIG"
  echo
  echo "Clear this file (rm $ALERT) once FULCRA-PRIMITIVES.md is rewritten for"
  echo "EVERY entry in it — not just the last one."
} >> "$ALERT"
log "DRIFT: $(printf '%s' "$DIFF" | tr '\n' '; ')"

SUMMARY="WHAT CHANGED (fulcra-api $PYPI_VER): $(printf '%s' "$DIFF" | tr '\n' '; ')"
if [ -n "$TRIGGER" ]; then
  SUMMARY="FULL-REWRITE TRIGGER — a top-level record/delete verb moved in the published CLI. $SUMMARY Note: record/delete are TOP-LEVEL verbs, not data_types.py subcommands. Tier-2 API-direct guidance shifts too."
  PRIO=P1
else
  PRIO=P2
fi
notify "$PRIO" "DRIFT: Fulcra primitives surface changed ($(ts))" \
  "$SUMMARY Re-verify + rewrite FULCRA-PRIMITIVES.md, then rm $ALERT. Alert file: $ALERT."

# Advance the baseline only now that the change is characterized in the alert
# and named in the tell. The ALERT file stays until a session clears it, so an
# unactioned drift keeps being reported (see the no-drift branch above) —
# rebaselining stops repeat *discovery* noise, not the outstanding work.
# If we could not say WHAT changed, we do not get to forget that it changed.
case "$DIFF" in
  "DIFF RENDERER FAILED"*)
    log "baseline NOT advanced: change could not be characterized"
    ;;
  *)
    echo "$CUR" > "$BASELINE"
    ;;
esac
exit 1
