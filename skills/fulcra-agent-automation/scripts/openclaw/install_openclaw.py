#!/usr/bin/env python3
"""coord OpenClaw HEARTBEAT/BOOT managed-block installer (fulcra-agent-automation).

Standalone (Python 3.10+ stdlib only). Ports ONLY the prose-file marker layer
from the retired first-generation adapter (available in git history): the
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

COEXISTENCE + MIGRATION: this installer fences its BOOT.md / HEARTBEAT.md block
with ``<!-- fulcra-agent:begin ... -->`` / ``<!-- fulcra-agent:end -->``. Two
other generations may exist in a workspace:

  * PRE-COORD2 LEGACY (never touched) — the original adapter fenced with
    ``<!-- fulcra-coord:begin ... -->`` / ``<!-- fulcra-coord:end -->``. The
    scanner matches marker lines by EXACT string comparison, and
    ``fulcra-agent:begin`` is not a substring of ``fulcra-coord:begin`` (nor
    vice-versa), so a workspace carrying a pre-coord2 block keeps it untouched.
  * COORD2-ERA (migrated in place) — the immediately-prior build of THIS
    installer used ``<!-- fulcra-coord2:begin ... -->`` /
    ``<!-- fulcra-coord2:end -->``. The scanner recognizes that pair too
    (``_LEGACY_BEGIN`` / ``_LEGACY_END``) and treats a coord2-fenced block as
    a managed block: a re-install strips it and writes the new fence in its
    place (user content outside the fence preserved), an uninstall removes it.
    Integrity is enforced for BOTH generations — an unbalanced marker of either
    generation, or a block whose BEGIN and END fences are of DIFFERENT
    generations, aborts with no write. The pre-coord2 and coord2-era marker
    strings appear in this file only as these recognition constants + comments.

PATH SAFETY (ported posture): the installer only ever writes the two canonical
workspace basenames (``HEARTBEAT.md``, ``BOOT.md``) directly under the given
``--workspace`` dir. Any other name, or a resolved path that escapes the
workspace (e.g. via a symlink), is rejected. If a canonical file does not exist
it is created containing only the fenced block (a fresh OpenClaw workspace); if
it exists the fenced region is appended/replaced in place, preserving all user
content.

CONTENT SAFETY (hardened after review — three reproduced findings):
  * Marker integrity — before ANY write, each target file's coord markers are
    validated line-wise. Unbalanced markers (e.g. an orphan BEGIN left by a
    crash-truncated write) or an END preceding its BEGIN abort with exit 1 and
    a message telling the operator to repair the file manually; nothing is
    written. Refusal is the fail-safe: blindly appending a second block would
    set a trap where the next uninstall spans orphan-BEGIN -> appended-END and
    destroys the user content between them.
  * Code-fence awareness — files are scanned line-wise with a minimal fence
    state machine (a line whose stripped form starts with ``` toggles fence
    state). Marker lines inside a code fence are IGNORED for both counting and
    span matching, so a user documenting the markers in a fenced sample never
    has that sample treated as a real managed block. Only a marker alone at
    the start of its own line, outside any code fence, counts.
  * Gated husk delete — on uninstall a file is deleted only if a coord block
    was actually stripped from it AND nothing remains. A pre-existing empty or
    whitespace-only file that never held a coord block is left untouched.

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
import sys
from pathlib import Path
from typing import Any

# Marker block fencing our managed content inside BOOT.md / HEARTBEAT.md so a
# re-install or uninstall is surgical and never clobbers the user's own boot /
# heartbeat prose. The current fence is ``fulcra-agent``; the coord2-era
# ``fulcra-coord2`` pair is recognized for in-place migration only (see module
# doc). Neither collides with the pre-coord2 ``fulcra-coord`` fence.
_BEGIN = "<!-- fulcra-agent:begin (managed; do not edit between markers) -->"
_END = "<!-- fulcra-agent:end -->"
_LEGACY_BEGIN = "<!-- fulcra-coord2:begin (managed; do not edit between markers) -->"
_LEGACY_END = "<!-- fulcra-coord2:end -->"

# Block bodies (brief-binding: the canonical watcher tick, OpenClaw voice).
# {team} / {agent} are the only format fields; the placeholders `<slug>`,
# `<task>`, `<acct>`, `<tier>`, `<est>`, the `"..."`, and HEARTBEAT_OK are
# literal prose the agent fills in / emits at run time.
HEARTBEAT_BLOCK = """\
On each heartbeat, as {agent} on coord team {team}: resume continuity and run
`coord-engine briefing {team} --agent {agent}` once. Handle every surfaced item end-to-end.
A degraded section is not clear; apply its documented targeted fallback. For reviews, write
and verify the exact required verdict before acking. Snapshot material work, refresh presence
and held-role leases, park before session end, then report last. If nothing is actionable,
the final output is exactly HEARTBEAT_OK.
"""

BOOT_BLOCK = """\
On boot, as {agent} on coord team {team}: resume continuity and run
`coord-engine briefing {team} --agent {agent}` once before new work. A degraded section is not
clear; apply its documented targeted fallback. For reviews, write and verify the exact required
verdict before acking. Handle other work end-to-end, snapshot material work, refresh presence
and held-role leases, park before session end, then report last.
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


class MarkerIntegrityError(ValueError):
    """The file's coord markers are unbalanced/malformed; refuse to write."""


def _is_marker_line(raw: str, marker: str) -> bool:
    """True iff ``raw`` is exactly ``marker`` alone at the start of its line.

    A marker with leading whitespace, trailing text, or embedded mid-line is
    inert prose: it is neither counted for integrity nor spanned for strip.
    The blocks this installer writes always satisfy this shape. Exact string
    comparison means the pre-coord2 ``fulcra-coord`` marker line can never match
    (distinct strings), so pre-coord2 blocks are untouched.
    """
    return raw.rstrip("\r\n").rstrip() == marker and raw.startswith(marker)


def _marker_kind(raw: str) -> "tuple[str, str] | None":
    """Classify a line as a managed marker: ``(role, generation)`` where role is
    ``begin``/``end`` and generation is ``new`` (fulcra-agent) or ``legacy``
    (coord2-era), else None. Both generations are managed; the pre-coord2
    ``fulcra-coord`` fence classifies as None (distinct strings) so it is inert.
    """
    for marker, role, gen in (
        (_BEGIN, "begin", "new"), (_END, "end", "new"),
        (_LEGACY_BEGIN, "begin", "legacy"), (_LEGACY_END, "end", "legacy"),
    ):
        if _is_marker_line(raw, marker):
            return role, gen
    return None


def _scan(text: str, filename: str) -> "tuple[list[str], list[tuple[int, int]]]":
    """Line-wise scan for coord blocks, code-fence-aware, integrity-checked.

    Returns ``(lines, spans)`` where ``lines`` is ``splitlines(keepends=True)``
    and each span is an inclusive ``(begin_line_idx, end_line_idx)`` of a
    well-formed managed block of EITHER generation (current ``fulcra-agent`` or
    coord2-era ``fulcra-coord2``). Lines whose stripped form starts with ``` toggle
    code-fence state; marker lines inside a code fence are IGNORED for both
    counting and span matching (finding 2). Raises MarkerIntegrityError on an
    orphan BEGIN, an END with no open BEGIN, a nested BEGIN (finding 1), or a
    block whose BEGIN and END fences are of DIFFERENT generations (a mismatched
    fence that could only arise from corruption/hand-edit) — callers must write
    NOTHING in that case; manual repair is the only safe path.
    """
    lines = text.splitlines(keepends=True)
    spans: "list[tuple[int, int]]" = []
    in_code_fence = False
    open_begin: "int | None" = None
    open_gen: "str | None" = None
    for i, raw in enumerate(lines):
        if raw.strip().startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        mk = _marker_kind(raw)
        if mk is None:
            continue
        role, gen = mk
        if role == "begin":
            if open_begin is not None:
                raise MarkerIntegrityError(
                    f"{filename}: coord BEGIN marker on line {i + 1} while the "
                    f"block opened on line {open_begin + 1} is still open "
                    "(unbalanced markers). Refusing to modify the file; repair "
                    "it manually so every BEGIN has exactly one matching END.")
            open_begin = i
            open_gen = gen
        else:  # role == "end"
            if open_begin is None:
                raise MarkerIntegrityError(
                    f"{filename}: coord END marker on line {i + 1} with no "
                    "preceding BEGIN (unbalanced markers). Refusing to modify "
                    "the file; repair it manually so every END follows its "
                    "BEGIN.")
            if gen != open_gen:
                raise MarkerIntegrityError(
                    f"{filename}: coord END marker on line {i + 1} closes a "
                    f"block opened on line {open_begin + 1} with a fence of a "
                    "DIFFERENT generation (mismatched fulcra-agent/fulcra-coord2 "
                    "markers). Refusing to modify the file; repair it manually.")
            spans.append((open_begin, i))
            open_begin = None
            open_gen = None
    if open_begin is not None:
        raise MarkerIntegrityError(
            f"{filename}: orphan coord BEGIN marker on line "
            f"{open_begin + 1} with no matching END (e.g. a crash-truncated "
            "write). Refusing to modify the file; repair it manually — remove "
            "the orphan marker or restore the missing END — then re-run.")
    return lines, spans


def _strip_block(text: str, filename: str) -> str:
    """Remove every well-formed coord block (marker lines inclusive)."""
    lines, spans = _scan(text, filename)
    if not spans:
        return text
    drop = {i for begin, end in spans for i in range(begin, end + 1)}
    return "".join(raw for i, raw in enumerate(lines) if i not in drop)


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
    """Install/uninstall the coord managed block in BOOT.md + HEARTBEAT.md.

    Idempotent. ``dry_run`` writes nothing but returns the plan; ``uninstall``
    surgically removes only the coord-fenced region, preserving user content
    (and deleting a file left empty because it held ONLY our block — never a
    pre-existing empty/whitespace-only file that had no block).

    Raises MarkerIntegrityError — before any write — if a target file's coord
    markers are unbalanced (orphan BEGIN, END-before-BEGIN, nested BEGIN).
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
        # Scans + validates marker integrity; raises (MarkerIntegrityError)
        # before ANY file has been written — the write loop runs only after
        # every target validated cleanly.
        stripped = _strip_block(existing, name)

        if uninstall:
            new_text = stripped
            # Only a "remove" if there was actually a coord block to drop.
            if new_text != existing:
                plan["removes"].append(str(path))
                if new_text.strip() == "":
                    # A block was stripped AND nothing remains: the file held
                    # only our block — delete the husk. Both conditions are
                    # required: a pre-existing empty/whitespace-only file that
                    # never held a block (new_text == existing) is untouched.
                    plan["deletes"].append(str(path))
                    actions.append(("delete", path, ""))
                else:
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
        description="Install the coord OpenClaw HEARTBEAT/BOOT managed block.")
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
    try:
        plan = install(args.team, args.agent, workspace=workspace,
                       uninstall=args.uninstall, dry_run=args.dry_run)
    except MarkerIntegrityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("No files were modified.", file=sys.stderr)
        return 1
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
