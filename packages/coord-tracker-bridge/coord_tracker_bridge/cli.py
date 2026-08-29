"""Command-line entry point for the bridge.

Phases (all explicit, none implied by another):

- ``plan`` / ``adopt-markers`` / ``apply-resources`` / ``sync`` — the write
  pipeline, each gated behind its own verb so nothing mutates Linear as a side
  effect of a read.
- ``linear-inbox`` / ``linear-assignments`` — read verbs. They build no
  ``BridgeService``, so no ledger, lease or tracker adapter exists for a write
  path to reach, and their transport refuses any non-query document.

Credentials: see ``_resolve_linear_key``. The canonical table of which
variable holds which Linear credential lives in the repo's ``AGENTS.md``
("Which credential, and which variable holds it") and in
``packages/coord-tracker-bridge/README.md``; this module must never grow a
second copy of it (one canonical home per fact) and must never log a value.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

from .assignments import (
    DEFAULT_DELIVERY_CAP,
    EngineTellDispatcher,
    FulcraRosterReader,
    run_assignments,
)
from .lease import LeaseHeld
from .inbox import ReadOnlyTransport, fetch_inbox, render_fold
from .linear import HttpxGraphQLTransport, LinearClient, LinearError, LinearTrackerAdapter
from .policy import load_policy
from .service import BridgePlan, BridgeService
from .source import EngineSourceAdapter, TeamsSourceAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coord-tracker-bridge")
    parser.add_argument(
        "phase",
        choices=(
            "plan", "adopt-markers", "apply-resources", "sync",
            "linear-inbox", "linear-assignments",
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview adopt-markers mappings without provider or ledger writes",
    )
    parser.add_argument("--coord-team", default=os.environ.get("COORD_TEAM", "fulcra"))
    parser.add_argument("--source", choices=("engine", "teams"), default="engine")
    parser.add_argument("--principal", default="ash")
    parser.add_argument("--linear-team-id", default=os.environ.get("LINEAR_TEAM_ID"))
    parser.add_argument("--policy", type=Path)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local/state/coord-tracker-bridge",
    )
    # linear-assignments only. Preview is the default on purpose: --deliver is
    # the flag that dispatches directives AND the flag that advances the
    # watermark, so a run that shows you the plan can never consume it.
    parser.add_argument(
        "--deliver",
        action="store_true",
        help="linear-assignments: actually dispatch the planned directives",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="linear-assignments: adopt the current board as the baseline, delivering nothing",
    )
    parser.add_argument(
        "--delivery-cap",
        type=int,
        default=DEFAULT_DELIVERY_CAP,
        help="linear-assignments: refuse a run that would dispatch more than this",
    )
    parser.add_argument(
        "--coordinator",
        default=os.environ.get("COORD_COORDINATOR", "coord-boss"),
        help="linear-assignments: who receives unresolved/unassigned cards for triage",
    )
    parser.add_argument(
        "--sender",
        default=os.environ.get("FULCRA_COORD_AGENT", "coord-opus-worker"),
        help="linear-assignments: the --from identity on dispatched directives",
    )
    parser.add_argument(
        "--roster-path",
        default="team/fulcra/_coord/roster-nicknames.md",
        help="linear-assignments: the nickname roster document in the coord store",
    )
    return parser


# Credential resolution. The bridge originally read exactly one variable,
# LINEAR_API_KEY, and nothing else. When that credential stopped
# authenticating the projection went silently stale (last successful sync
# 2026-07-31) while three other working Linear credentials sat unused in the
# same environment -- and because this file named only the broken one, every
# operator and agent who read the code to find "the" credential landed on it.
# Resolution is therefore a list whose order is the preference, LINEAR_KEY_ENV
# names one explicitly, and an auth failure says which variable it used.
LINEAR_KEY_ENV_VARS: tuple[str, ...] = (
    "LINEAR_PERSONAL_KEY",
    "LINEAR_PERSONAL_KEY_2",
    "COORD_BRIDGE_DEVELOPER_TOKEN",
    "LINEAR_API_KEY",
)

_AUTH_FAILURE_MARKERS = (
    "http_status=401",
    "http_status=403",
    "AUTHENTICATION_ERROR",
    "FORBIDDEN",
)


def _linear_key_candidates(env: Mapping[str, str]) -> list[str]:
    """Candidate variable names in resolution order; LINEAR_KEY_ENV wins."""
    override = (env.get("LINEAR_KEY_ENV") or "").strip()
    return [override] if override else list(LINEAR_KEY_ENV_VARS)


def _resolve_linear_key(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return (variable name, credential) for the first candidate that is set.

    The name comes back with the value because an auth failure is only
    diagnosable if the operator can see WHICH of the several credentials
    usually present in the environment was the one actually sent.
    """
    env = os.environ if env is None else env
    for name in _linear_key_candidates(env):
        value = (env.get(name) or "").strip()
        if value:
            return name, value
    raise LinearError(
        "no Linear credential in the environment: set one of "
        + ", ".join(_linear_key_candidates(env))
        + ", or name the variable to use in LINEAR_KEY_ENV"
    )


def _looks_like_auth_failure(message: str) -> bool:
    upper = message.upper()
    return any(marker.upper() in upper for marker in _AUTH_FAILURE_MARKERS)


def _auth_failure_hint(env: Mapping[str, str] | None = None) -> str:
    """Name the credential that was used, and the ones that were not.

    Never includes a credential value -- only variable names. This is the
    line whose absence turned one dead credential into a month of silently
    stale projection: the failure said 401, not 401-using-LINEAR_API_KEY-
    while-LINEAR_PERSONAL_KEY-sat-right-there.
    """
    env = os.environ if env is None else env
    try:
        used, _value = _resolve_linear_key(env)
    except LinearError:
        return ""
    others = [
        name
        for name in LINEAR_KEY_ENV_VARS
        if name != used and (env.get(name) or "").strip()
    ]
    hint = f" [credential came from {used}"
    if others:
        hint += (
            f"; also set but not tried: {', '.join(others)}"
            "; select one with LINEAR_KEY_ENV=<name>"
        )
    return hint + "]"


def _service(args: argparse.Namespace) -> BridgeService:
    if not args.linear_team_id:
        raise LinearError("--linear-team-id/LINEAR_TEAM_ID is required")
    _key_env, api_key = _resolve_linear_key()
    policy = load_policy(args.policy)
    state_key = f"{args.source}-{args.coord_team}-{args.linear_team_id}-{policy.hash[:12]}"
    source = (
        EngineSourceAdapter(args.coord_team, principal=args.principal)
        if args.source == "engine"
        else TeamsSourceAdapter(args.coord_team)
    )
    return BridgeService(
        source,
        LinearTrackerAdapter(LinearClient(HttpxGraphQLTransport(api_key)), args.linear_team_id),
        policy,
        args.state_dir / f"{state_key}.json",
        args.state_dir / "leases",
    )


def _plan_json(plan: BridgePlan) -> dict:
    return {
        "resources": {"labels": list(plan.resources.labels), "projects": list(plan.resources.projects)},
        "changes": [
            {
                "kind": change.kind,
                "source": change.source.to_dict(),
                "provider_id": change.provider_id,
                "fields": dict(change.fields),
            }
            for change in plan.projection.changes
        ],
        "diagnostics": [diagnostic.to_dict() for diagnostic in plan.projection.diagnostics],
        "snapshot": {
            "complete": plan.snapshot.complete,
            "observed_at": plan.snapshot.observed_at.isoformat(),
            "capabilities": {key: value for key, value in plan.snapshot.capabilities.items()},
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.dry_run and args.phase != "adopt-markers":
            raise ValueError("--dry-run is only valid with adopt-markers")
        if (args.deliver or args.seed) and args.phase != "linear-assignments":
            raise ValueError("--deliver/--seed are only valid with linear-assignments")
        if args.phase == "linear-inbox":
            # Deliberately does NOT build a BridgeService: no ledger, no lease,
            # no tracker adapter, so no write path exists to reach. The client
            # is wrapped in a transport that refuses any non-query document.
            if not args.linear_team_id:
                raise LinearError("--linear-team-id/LINEAR_TEAM_ID is required")
            _key_env, api_key = _resolve_linear_key()
            client = LinearClient(ReadOnlyTransport(HttpxGraphQLTransport(api_key)))
            result = fetch_inbox(client, args.linear_team_id)
            print(render_fold(result, team_id=args.linear_team_id))
            # The read verbs fold every failure into UNKNOWN, so an auth
            # failure here never reaches main's handler. Say which credential
            # was sent anyway: an operator who sees only "http_status=401"
            # cannot tell a revoked key from the wrong variable.
            if result.unknown and _looks_like_auth_failure(result.detail or ""):
                print(
                    f"coord-tracker-bridge: Linear rejected the credential{_auth_failure_hint()}",
                    file=sys.stderr,
                )
            # UNKNOWN must not exit 0: a caller scripting this verb has to be
            # able to tell "Ash has no work" from "I could not read the board".
            return 3 if result.unknown else 0

        if args.phase == "linear-assignments":
            # Same shape as linear-inbox: no BridgeService, so no ledger, no
            # lease and no tracker adapter exist to reach a Linear write with.
            if not args.linear_team_id:
                raise LinearError("--linear-team-id/LINEAR_TEAM_ID is required")
            _key_env, api_key = _resolve_linear_key()
            client = LinearClient(ReadOnlyTransport(HttpxGraphQLTransport(api_key)))
            state_path = (
                args.state_dir
                / f"assignments-{args.coord_team}-{args.linear_team_id}.json"
            )
            outcome = run_assignments(
                client,
                team_id=args.linear_team_id,
                state_path=state_path,
                roster_reader=FulcraRosterReader(path=args.roster_path),
                coordinator=args.coordinator,
                dispatcher=EngineTellDispatcher(team=args.coord_team, sender=args.sender),
                deliver=args.deliver,
                do_seed=args.seed,
                cap=args.delivery_cap,
            )
            print(outcome.text)
            if _looks_like_auth_failure(outcome.text or ""):
                print(
                    f"coord-tracker-bridge: Linear rejected the credential{_auth_failure_hint()}",
                    file=sys.stderr,
                )
            return outcome.code

        service = _service(args)
        if args.phase == "plan":
            print(json.dumps(_plan_json(service.plan()), sort_keys=True, default=str))
        elif args.phase == "adopt-markers":
            if args.dry_run:
                adoptions = service.preview_marker_adoptions()
                print(json.dumps({
                    "dry_run": True,
                    "count": len(adoptions),
                    "adoptions": [
                        {
                            "provider_id": adoption.provider_id,
                            "source": adoption.source.to_dict(),
                            "capability": adoption.capability,
                        }
                        for adoption in adoptions
                    ],
                }, sort_keys=True))
            else:
                print(json.dumps({"adopted": service.adopt_markers()}))
        elif args.phase == "apply-resources":
            resources = service.apply_resources()
            print(json.dumps({"created_labels": list(resources.labels), "created_projects": list(resources.projects)}))
        else:
            result = service.sync()
            print(json.dumps({"applied": result.applied, "plan": _plan_json(result.plan)}, sort_keys=True, default=str))
        return 0
    except (LinearError, LeaseHeld, ValueError) as exc:
        message = f"coord-tracker-bridge: {type(exc).__name__}: {exc}"
        if isinstance(exc, LinearError) and _looks_like_auth_failure(str(exc)):
            message += _auth_failure_hint()
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
