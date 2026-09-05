"""Command-line entry point exposing the bridge's three explicit phases."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .assignments import (
    DEFAULT_DELIVERY_CAP,
    EngineTellDispatcher,
    FulcraRosterReader,
    default_roster_path,
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
    # No hardcoded team: this ships from a public repo, and a default here
    # points another team's bridge at ours. COORD_TEAM stays first for
    # back-compat; FULCRA_COORD_TEAM is the fleet-wide name.
    parser.add_argument(
        "--coord-team",
        default=(os.environ.get("COORD_TEAM")
                 or os.environ.get("FULCRA_COORD_TEAM") or ""))
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
        default=default_roster_path(),
        help="linear-assignments: the nickname roster document in the coord store",
    )
    return parser


def _service(args: argparse.Namespace) -> BridgeService:
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key or not args.linear_team_id:
        raise LinearError("LINEAR_API_KEY and --linear-team-id/LINEAR_TEAM_ID are required")
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
            api_key = os.environ.get("LINEAR_API_KEY")
            if not api_key or not args.linear_team_id:
                raise LinearError(
                    "LINEAR_API_KEY and --linear-team-id/LINEAR_TEAM_ID are required")
            client = LinearClient(ReadOnlyTransport(HttpxGraphQLTransport(api_key)))
            result = fetch_inbox(client, args.linear_team_id)
            print(render_fold(result, team_id=args.linear_team_id))
            # UNKNOWN must not exit 0: a caller scripting this verb has to be
            # able to tell "Ash has no work" from "I could not read the board".
            return 3 if result.unknown else 0

        if args.phase == "linear-assignments":
            # Same shape as linear-inbox: no BridgeService, so no ledger, no
            # lease and no tracker adapter exist to reach a Linear write with.
            api_key = os.environ.get("LINEAR_API_KEY")
            if not api_key or not args.linear_team_id:
                raise LinearError(
                    "LINEAR_API_KEY and --linear-team-id/LINEAR_TEAM_ID are required")
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
        print(f"coord-tracker-bridge: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
