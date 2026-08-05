"""Is the RUNNING engine the one the fleet agreed to run?

WHY THIS EXISTS. ``doctor`` answered "is this engine functioning?" and never
"is this engine CURRENT?", and post-container-swap those two questions have
different answers. Measured on 2026-08-05: a fresh container came up on
coord-engine v1.6.12 — an engine predating bus-v3, with no ``bus-v3``
subcommand at all and the retired ``listen`` still in its command list — and
``doctor`` printed three ticks and ``healthy``. That agent could not send a
single event or close a single item, and its only preflight told it everything
was fine. coord-boss counted 18 such stale restores in 24h across his own
container swaps alone.

This is the third member of one class in a week (the adopt sanity check, the
fast-path projection debt, now this), and the cure is the same each time: the
verifier must check the thing's CURRENCY, not just its pulse.

HOW CURRENCY IS PROVEN, and when it honestly cannot be.

The fleet pin is the ``PIN=`` line of the store's ``adopt-latest.sh`` — that
script IS the adoption authority, so nothing else gets to define "current".

The running build's identity comes from the install's own ``direct_url.json``
(PEP 610), which records the exact ``commit_id`` pip/uv resolved the VCS
requirement to. Read from the RUNNING interpreter's distribution metadata, for
the same reason :func:`coord_engine.cli.writer_present` imports rather than
probes: ``uv tool install`` gives the engine a private venv, and asking any
other interpreter answers a different question.

Version strings cannot substitute. ``1.11.0`` does not name a sha, and the map
from pin to version lives in the pinned tree's ``pyproject.toml`` — reachable
only over the network, which a local preflight has no business requiring and
cannot depend on when the outage IS the network. So when the build sha is
unknowable (an editable checkout, an sdist, a wheel built elsewhere) currency is
UNPROVEN and says so. Unknown is never rendered as a tick: that is the exact
failure being fixed, and reproducing it one layer up would be worse than useless.

POSTURE: warn, never unhealthy — the same call as the writer-presence line. A
mismatch is a strong signal but not a certainty (a maintainer running a build
AHEAD of the pin is a mismatch too, and offline there is no way to tell ahead
from behind), and flipping doctor to unhealthy on a signal that fires for
legitimate states would train agents to ignore the exit code. The line carries
the consequence and the remedy, which is what a reader actually needs.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import read_retry

#: Team-relative path of the adoption authority.
ADOPT_SCRIPT_NAME = "_coord/bus-v3/adopt-latest.sh"

#: Distribution name to interrogate for our own build identity.
DIST_NAME = "coord-engine"

#: ``PIN=<sha>`` with optional quoting, anchored to the start of a line so a
#: sha mentioned inside a comment or a message cannot be mistaken for the
#: assignment. 7 hex is git's short-sha floor; below that a "match" would be
#: coincidence rather than evidence.
_PIN_RE = re.compile(r'^[ \t]*PIN=[ \t]*["\']?([0-9a-fA-F]{7,40})["\']?',
                     re.MULTILINE)

#: Shortest prefix comparison we will call a match.
MIN_SHA_PREFIX = 7


def adopt_script_path(team: str) -> str:
    return f"team/{team}/{ADOPT_SCRIPT_NAME}"


def parse_pin(raw: Any) -> Optional[str]:
    """The fleet pin sha from ``adopt-latest.sh``, lowercased, or None.

    The FIRST assignment wins. A script with two ``PIN=`` lines is ambiguous
    about its own authority, and picking the last would silently prefer whatever
    a careless edit appended; taking the first at least matches how the shell
    that runs it would behave if the second were inside a conditional it skips.
    """
    if not isinstance(raw, str):
        return None
    m = _PIN_RE.search(raw)
    return m.group(1).lower() if m else None


def build_sha(dist_name: str = DIST_NAME) -> Optional[str]:
    """The VCS commit this engine was installed from, or None if unknowable.

    None is a legitimate, common answer (editable installs, sdists, wheels) and
    means UNPROVEN — never "fine".
    """
    try:
        import importlib.metadata as md
        raw = md.distribution(dist_name).read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    vcs = doc.get("vcs_info")
    if not isinstance(vcs, dict):
        return None
    commit = vcs.get("commit_id")
    if not isinstance(commit, str):
        return None
    commit = commit.strip().lower()
    return commit if len(commit) >= MIN_SHA_PREFIX else None


def shas_match(build: Optional[str], pin: Optional[str]) -> bool:
    """Prefix-tolerant sha equality — the pin may legitimately be short."""
    if not build or not pin:
        return False
    a, b = build.lower(), pin.lower()
    n = min(len(a), len(b))
    if n < MIN_SHA_PREFIX:
        return False
    return a[:n] == b[:n]


def classify(build: Optional[str], pin: Optional[str], pin_status: str) -> str:
    """One of ``current`` | ``mismatch`` | ``unknown-pin`` | ``unknown-build``.

    Pin trouble is reported ahead of build trouble on purpose: if we cannot say
    what the fleet agreed to run, knowing our own sha exactly changes nothing.
    """
    if pin_status != "ok" or not pin:
        return "unknown-pin"
    if not build:
        return "unknown-build"
    return "current" if shas_match(build, pin) else "mismatch"


def load_pin(transport: Any, team: str) -> tuple[Optional[str], str]:
    """``(pin, status)`` with status in ok|absent|invalid|error.

    ``invalid`` means the authority script was readable but carries no parsable
    ``PIN=`` line — bytes exist and do not say what they must, which is a
    different problem from a store outage and must not be reported as one.
    """
    path = adopt_script_path(team)
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        try:
            raw = transport.read(path)
        except Exception:
            return None, "error"
        if raw is None:
            return None, "absent"
        status = "ok"
    else:
        try:
            raw, status = read_retry.read_classified_retrying(reader, path)
        except Exception:
            return None, "error"
        if status == "error":
            return None, "error"
        if raw is None:
            return None, "absent"
    pin = parse_pin(raw)
    return (pin, "ok") if pin else (None, "invalid")


def report_line(verdict: str, *, build: Optional[str], pin: Optional[str],
                pin_status: str, team: Optional[str]) -> str:
    """The single doctor line. Only ``current`` earns a tick."""
    if verdict == "current":
        return (f"  ✓ engine matches the fleet pin ({(pin or '')[:12]})")
    remedy = ("run the store adopt-latest.sh before acting; a stale engine "
              "cannot speak bus-v3")
    if verdict == "mismatch":
        return (f"  ! engine build {(build or '?')[:12]} does NOT match the "
                f"fleet pin {(pin or '?')[:12]} — may be BEHIND the fleet; "
                f"{remedy}")
    if verdict == "unknown-build":
        return ("  ! engine build sha UNKNOWN (no VCS install metadata) — "
                f"currency vs fleet pin {(pin or '?')[:12]} cannot be proven; "
                f"if this host was just recreated, {remedy}")
    if team is None:
        return ("  ! fleet pin NOT CHECKED (no team given) — currency unknown; "
                "run `coord-engine doctor <team>` to verify the engine is "
                "current")
    reason = {
        "error": "adoption authority unreadable (store read failed)",
        "absent": "adoption authority absent",
        "invalid": "adoption authority carries no PIN= line",
    }.get(pin_status, "adoption authority unusable")
    return (f"  ! fleet pin UNKNOWN — {reason}; currency cannot be proven; "
            f"if this host was just recreated, {remedy}")


def report(transport: Any, team: Optional[str]) -> str:
    """Compute and render the doctor line. Never raises, never unhealthy."""
    build = build_sha()
    if team is None:
        return report_line("unknown-pin", build=build, pin=None,
                           pin_status="no-team", team=None)
    try:
        pin, pin_status = load_pin(transport, team)
    except Exception:
        pin, pin_status = None, "error"
    verdict = classify(build, pin, pin_status)
    return report_line(verdict, build=build, pin=pin, pin_status=pin_status,
                       team=team)
