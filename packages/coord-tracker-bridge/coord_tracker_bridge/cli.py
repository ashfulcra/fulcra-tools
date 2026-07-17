"""Command-line entry point exposing the bridge's three explicit phases."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .lease import LeaseHeld
from .linear import HttpxGraphQLTransport, LinearClient, LinearError, LinearTrackerAdapter
from .policy import load_policy
from .service import BridgePlan, BridgeService
from .source import EngineSourceAdapter, TeamsSourceAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coord-tracker-bridge")
    parser.add_argument("phase", choices=("plan", "apply-resources", "sync"))
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
        service = _service(args)
        if args.phase == "plan":
            print(json.dumps(_plan_json(service.plan()), sort_keys=True, default=str))
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
