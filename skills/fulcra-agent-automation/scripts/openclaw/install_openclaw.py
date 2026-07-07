#!/usr/bin/env python3
"""coord2 OpenClaw HEARTBEAT/BOOT managed-block installer (fulcra-agent-automation).

Standalone (Python 3.10+ stdlib only). Ports ONLY the prose-file marker layer
from the legacy adapter ``packages/fulcra-coord/fulcra_coord/openclaw.py``: the
fence/replace-in-place helpers that merge a managed block into an OpenClaw
workspace's ``BOOT.md`` / ``HEARTBEAT.md`` (agent-driven prompt files OpenClaw
reads at boot and on each heartbeat).

SCOPE — prose blocks only. The legacy adapter also materialized a hooks-dir of
``HOOK.md`` + ``handler.ts`` automation hooks (``fulcra-coord-shutdown`` /
``-bootstrap`` / ``-compact``). That machinery is DELIBERATELY NOT ported here:
this pass is the prose-block layer alone. The two ``handler.ts`` templates also
carried a second, shorter marker pair (``<!-- fulcra-coord:begin -->`` /
``<!-- fulcra-coord:end -->``) used inside MEMORY.md injection — that is NOT the
fence ported here; the fence ported here is the BOOT/HEARTBEAT prose-merge pair
(legacy ``openclaw.py`` lines ~75-76).

COEXISTENCE: the legacy adapter is live on real hosts and fences its BOOT.md /
HEARTBEAT.md block with ``<!-- fulcra-coord:begin ... -->`` /
``<!-- fulcra-coord:end -->``. This installer uses a DISTINCT coord2 pair,
``<!-- fulcra-coord2:begin ... -->`` / ``<!-- fulcra-coord2:end -->``. The strip
regex escapes the coord2 begin string literally, and ``fulcra-coord2:begin`` is
not a substring of ``fulcra-coord:begin`` (nor vice-versa), so this installer's
install/uninstall can only ever touch the coord2-fenced region — a workspace
carrying the legacy fulcra-coord block keeps it untouched until the phase-3
freeze retires it. The legacy marker string appears in this file only in this
comment.

PATH SAFETY (ported posture): the installer only ever writes the two canonical
workspace basenames (``HEARTBEAT.md``, ``BOOT.md``) directly under the given
``--workspace`` dir. Any other name, or a resolved path that escapes the
workspace (e.g. via a symlink), is rejected. If a canonical file does not exist
it is created containing only the fenced block (a fresh OpenClaw workspace); if
it exists the fenced region is appended/replaced in place, preserving all user
content.

CLI:
  python3 install_openclaw.py <team> <agent>
      [--workspace DIR] [--uninstall] [--dry-run]

``--dry-run`` writes nothing and prints the plan (the plan JSON is this tool's
debugging instrumentation: it names every file it would create/write/remove).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Marker block fencing our managed content inside BOOT.md / HEARTBEAT.md so a
# re-install or uninstall is surgical and never clobbers the user's own boot /
# heartbeat prose. Renamed from the legacy ``fulcra-coord`` fence to a distinct
# ``fulcra-coord2`` pair so both can coexist until the freeze (see module doc).
_BEGIN = "<!-- fulcra-coord2:begin (managed; do not edit between markers) -->"
_END = "<!-- fulcra-coord2:end -->"

# Block bodies (brief-binding). {team} / {agent} are the only format fields;
# `<task>`, `"..."`, and HEARTBEAT_OK are literal prose.
HEARTBEAT_BLOCK = """\
On each heartbeat, as {agent} on coord2 team {team}:
1. coord-engine continuity resume {team} {agent}
2. coord-engine inbox {team} --agent {agent} ; coord-engine needs-me {team} --agent {agent}
3. Act on anything new; after completing a work item:
   coord-engine continuity snapshot {team} {agent} <task> --objective "..."
4. Otherwise reply HEARTBEAT_OK.
"""

BOOT_BLOCK = """\
On boot, as {agent} on coord2 team {team}: run
coord-engine continuity resume {team} {agent} and read your inbox before new work.
"""

# Canonical workspace basename -> block body template. These are the ONLY files
# this installer will ever create or modify.
CANONICAL_FILES: dict[str, str] = {
    "HEARTBEAT.md": HEARTBEAT_BLOCK,
    "BOOT.md": BOOT_BLOCK,
}


def _default_workspace() -> Path:
    """Default OpenClaw workspace, overridable via env for tests.

    FULCRA_OPENCLAW_WORKSPACE lets tests (and unusual installs) point the
    installer at an arbitrary tree without touching the real ~/.openclaw/.
    """
    env = os.environ.get("FULCRA_OPENCLAW_WORKSPACE", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".openclaw"


def _managed_block(body: str) -> str:
    return f"{_BEGIN}\n{body.rstrip()}\n{_END}\n"


# Match our coord2 marker block (and any trailing newline) for surgical strip.
# re.escape on the coord2 begin/end means a legacy ``fulcra-coord`` block can
# never match (distinct strings), so it is left untouched.
_BLOCK_RE = re.compile(
    re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\n?",
    re.DOTALL,
)


def _strip_block(text: str) -> str:
    return _BLOCK_RE.sub("", text)


def _resolve_target(workspace: Path, name: str) -> Path:
    """Validate + resolve a canonical target path under ``workspace``.

    Rejects any name that is not one of the two canonical basenames, and any
    resolved path that does not sit directly inside the workspace (guards
    ``..`` and symlink escapes). Ported path-validation posture: only the
    canonical workspace file names are ever writable.
    """
    if name not in CANONICAL_FILES:
        raise ValueError(f"refusing to touch non-canonical file: {name!r}")
    ws = workspace.resolve()
    target = (ws / name).resolve()
    if target.parent != ws:
        raise ValueError(
            f"refusing to write {target}: escapes workspace {ws}")
    if target.name != name:
        raise ValueError(f"resolved name {target.name!r} != canonical {name!r}")
    return target


def install(team: str, agent: str, *, workspace: Path,
            uninstall: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Install/uninstall the coord2 managed block in BOOT.md + HEARTBEAT.md.

    Idempotent. ``dry_run`` writes nothing but returns the plan; ``uninstall``
    surgically removes only the coord2-fenced region, preserving user content
    (and deleting a file that becomes empty because it held only our block).
    """
    plan: dict[str, Any] = {
        "workspace": str(workspace),
        "uninstall": uninstall,
        "dry_run": dry_run,
        "writes": [],
        "removes": [],
        "deletes": [],
    }
    actions: list[tuple[str, Path, str]] = []

    for name, body_tmpl in CANONICAL_FILES.items():
        path = _resolve_target(workspace, name)
        existing = path.read_text() if path.is_file() else ""
        stripped = _strip_block(existing)

        if uninstall:
            new_text = stripped
            # Only a "remove" if there was actually a coord2 block to drop.
            if new_text != existing:
                plan["removes"].append(str(path))
            if new_text.strip() == "":
                # File is now empty (held only our block) — delete the husk so
                # a fresh-workspace file we created leaves no trace.
                plan["deletes"].append(str(path))
                actions.append(("delete", path, ""))
            elif new_text != existing:
                actions.append(("write", path, new_text))
        else:
            block = _managed_block(body_tmpl.format(team=team, agent=agent))
            if stripped.strip():
                # Preserve the user's own content, append our block after it.
                new_text = stripped.rstrip() + "\n\n" + block
            else:
                # Fresh (or block-only) file: contains only the fenced block.
                new_text = block
            plan["writes"].append(str(path))
            actions.append(("write", path, new_text))

    if dry_run:
        return plan

    for action, path, text in actions:
        if action == "delete":
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:  # write
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    return plan


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        description="Install the coord2 OpenClaw HEARTBEAT/BOOT managed block.")
    p.add_argument("team")
    p.add_argument("agent")
    p.add_argument("--workspace", default=None,
                   help="OpenClaw workspace dir (default ~/.openclaw or "
                        "$FULCRA_OPENCLAW_WORKSPACE); overridable for tests")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    workspace = (Path(args.workspace).expanduser() if args.workspace
                 else _default_workspace())
    plan = install(args.team, args.agent, workspace=workspace,
                   uninstall=args.uninstall, dry_run=args.dry_run)
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
