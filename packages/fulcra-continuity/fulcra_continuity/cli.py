"""Command-line interface for Fulcra Continuity."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .checkpoint import (
    checkpoint_from_dict,
    default_demo_checkpoint,
    ensure_parent,
    make_checkpoint,
    parse_artifact,
    parse_memory_write,
    render_resume_brief,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fulcra-continuity",
        description="Create agent continuity checkpoints and resume briefs.",
    )
    parser.add_argument("--version", action="version", version=f"fulcra-continuity {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    checkpoint = sub.add_parser("checkpoint", help="Write a continuity checkpoint JSON file")
    checkpoint.add_argument("--task-id", required=True)
    checkpoint.add_argument("--title", required=True)
    checkpoint.add_argument("--objective", required=True)
    checkpoint.add_argument("--owner-agent", default=os.environ.get("FULCRA_CONTINUITY_AGENT", ""))
    checkpoint.add_argument(
        "--workstream-id",
        default=os.environ.get("FULCRA_CONTINUITY_WORKSTREAM_ID", ""),
        help="Optional shared workstream identity, such as openclaw:discord:main-comms",
    )
    checkpoint.add_argument(
        "--agent-id",
        default=os.environ.get("FULCRA_CONTINUITY_AGENT_ID", ""),
        help="Optional logical agent identity for handoff and pickup",
    )
    checkpoint.add_argument(
        "--coord-task-id",
        default="",
        help="Optional fulcra-coord task ID this checkpoint resumes",
    )
    checkpoint.add_argument(
        "--coord-owner-agent",
        default="",
        help="Optional fulcra-coord owner agent for the referenced task",
    )
    checkpoint.add_argument("--source", default="manual")
    checkpoint.add_argument("--transcript-path", default="")
    checkpoint.add_argument("--context-used", type=int, default=None)
    checkpoint.add_argument(
        "--session-goal",
        default="",
        help="Broader work/session goal this checkpoint belongs to",
    )
    checkpoint.add_argument(
        "--why-continuity",
        default="",
        help="Why continuity matters for this workstream or handoff",
    )
    checkpoint.add_argument(
        "--session-state",
        default="",
        help="Current broader session/program state, beyond the immediate task",
    )
    checkpoint.add_argument(
        "--session-followup",
        default="",
        help="Immediate follow-up in the broader session/program",
    )
    checkpoint.add_argument("--decision", action="append", default=[])
    checkpoint.add_argument("--artifact", action="append", default=[], help="PATH or PATH=NOTE")
    checkpoint.add_argument("--open-question", action="append", default=[])
    checkpoint.add_argument("--next", dest="next_actions", action="append", default=[])
    checkpoint.add_argument(
        "--memory",
        action="append",
        default=[],
        help="CLAIM or CLAIM|SCOPE|TTL|SUPERSEDES",
    )
    checkpoint.add_argument("--tag", action="append", default=[])
    checkpoint.add_argument("--out", type=Path, required=True)
    checkpoint.add_argument(
        "--resume-brief",
        type=Path,
        default=None,
        help="Optional path for a human-readable resume brief",
    )

    resume = sub.add_parser("resume", help="Render a resume brief from checkpoint JSON")
    resume.add_argument("checkpoint", type=Path)
    resume.add_argument("--out", type=Path, default=None)

    demo = sub.add_parser("demo", help="Write a Context Cliff Rescue demo fixture")
    demo.add_argument("--out-dir", type=Path, required=True)

    return parser


def _write_json(path: Path, data: dict) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    path.write_text(text, encoding="utf-8")


def _load_checkpoint(path: Path):
    return checkpoint_from_dict(json.loads(path.read_text(encoding="utf-8")))


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "checkpoint":
        checkpoint = make_checkpoint(
            task_id=args.task_id,
            title=args.title,
            objective=args.objective,
            owner_agent=args.owner_agent,
            workstream_id=args.workstream_id,
            agent_id=args.agent_id,
            coord_task_id=args.coord_task_id,
            coord_owner_agent=args.coord_owner_agent,
            source=args.source,
            transcript_path=args.transcript_path,
            context_used_percent=args.context_used,
            session_context={
                "overall_goal": args.session_goal,
                "why_continuity_matters": args.why_continuity,
                "current_state": args.session_state,
                "immediate_followup": args.session_followup,
            },
            decisions=args.decision,
            artifacts=[parse_artifact(item) for item in args.artifact],
            open_questions=args.open_question,
            next_actions=args.next_actions,
            memory_writes=[parse_memory_write(item) for item in args.memory],
            tags=args.tag,
        )
        _write_json(args.out, checkpoint.to_dict())
        if args.resume_brief:
            _write_text(args.resume_brief, render_resume_brief(checkpoint))
        print(f"wrote checkpoint {checkpoint.checkpoint_id} to {args.out}")
        return 0

    if args.command == "resume":
        checkpoint = _load_checkpoint(args.checkpoint)
        brief = render_resume_brief(checkpoint)
        if args.out:
            _write_text(args.out, brief)
            print(f"wrote resume brief to {args.out}")
        else:
            sys.stdout.write(brief)
        return 0

    if args.command == "demo":
        checkpoint = default_demo_checkpoint()
        checkpoint_path = args.out_dir / "context-cliff-rescue.checkpoint.json"
        brief_path = args.out_dir / "context-cliff-rescue.resume.md"
        _write_json(checkpoint_path, checkpoint.to_dict())
        _write_text(brief_path, render_resume_brief(checkpoint))
        print(f"wrote demo checkpoint to {checkpoint_path}")
        print(f"wrote demo resume brief to {brief_path}")
        return 0

    parser.error(f"unknown command {args.command}")
    return 2


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
