"""Platform hook installers for fulcra-prefs.

The hook contract is intentionally small:

* SessionStart compiles and injects the platform preference block as hook JSON.
* Capture hooks drain a per-session candidate file, if an agent wrote one.

Candidate path:
  ~/.local/state/fulcra-prefs/candidates/<platform>/<session_id>.json

The candidate file is the same JSON array accepted by ``capture-batch``. A
successful drain renames the file to ``.captured`` so repeated lifecycle hooks
do not double-ingest it.
"""
from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any


MANAGED_DIRNAME = "fulcra-prefs-hooks"
SESSION_START_SCRIPT = "session-start.sh"
CAPTURE_SCRIPT = "capture-candidates.sh"

_SUPPORTED = {"claude-code", "codex"}
_CAPTURE_EVENTS = {
    "claude-code": ("PreCompact", "Stop"),
    "codex": ("PreCompact",),
}


def _resolved_cli() -> str:
    exe = shutil.which("fulcra-prefs")
    if exe:
        return exe
    argv0 = Path(sys.argv[0])
    if argv0.name == "fulcra-prefs" and argv0.exists():
        return str(argv0)
    return "fulcra-prefs"


def _script_session_start(platform: str, cli: str) -> str:
    qcli = shlex.quote(cli)
    qplatform = shlex.quote(platform)
    return f"""#!/usr/bin/env bash
# fulcra-prefs SessionStart hook. Fail-safe: emit nothing on any error.
set +e
PLATFORM={qplatform}
FULCRA_PREFS={qcli}
"$FULCRA_PREFS" compile >/dev/null 2>&1
PREFS="$("$FULCRA_PREFS" inject --platform "$PLATFORM" 2>/dev/null)"
[ -z "$PREFS" ] && exit 0
python3 - "$PREFS" <<'PY' 2>/dev/null
import json, sys
ctx = sys.argv[1]
print(json.dumps({{"hookSpecificOutput": {{
    "hookEventName": "SessionStart",
    "additionalContext": ctx,
}}}}))
PY
exit 0
"""


def _script_capture(platform: str, cli: str) -> str:
    qcli = shlex.quote(cli)
    qplatform = shlex.quote(platform)
    return f"""#!/usr/bin/env bash
# fulcra-prefs candidate-drain hook. Fail-safe: no candidate file -> no-op.
set +e
PLATFORM={qplatform}
FULCRA_PREFS={qcli}
INPUT="$(cat 2>/dev/null)"
SID="$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null)"
[ -z "$SID" ] && exit 0
case "$SID" in */*|*..*) exit 0;; esac
ROOT="${{FULCRA_PREFS_CANDIDATE_DIR:-$HOME/.local/state/fulcra-prefs/candidates}}"
FILE="$ROOT/$PLATFORM/$SID.json"
[ -f "$FILE" ] || exit 0
"$FULCRA_PREFS" drain-candidates --platform "$PLATFORM" --session "$SID" >/dev/null 2>&1
RC=$?
[ "$RC" -eq 0 ] || exit 0
exit 0
"""


def _target_dir(platform: str, target_dir: str | Path | None) -> Path:
    if target_dir is not None:
        return Path(target_dir)
    if platform == "claude-code":
        return Path.home() / ".claude"
    if platform == "codex":
        return Path.home() / ".codex"
    raise ValueError(f"unsupported platform: {platform}")


def _config_path(platform: str, root: Path) -> Path:
    return root / ("settings.json" if platform == "claude-code" else "hooks.json")


def _is_managed(command: str) -> bool:
    return (
        MANAGED_DIRNAME in command
        # Legacy pre-installer one-liner from the initial dogfood pass.
        or ("fulcra-prefs compile" in command and "fulcra-prefs inject" in command)
    )


def _load_config(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise ValueError(
            f"{path} is not valid JSON; fix it before installing hooks"
        ) from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _strip_managed(hooks: dict[str, Any], events: set[str]) -> None:
    for event in events:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            hooks.pop(event, None)
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            entry_hooks = entry.get("hooks", [])
            if not isinstance(entry_hooks, list):
                kept.append(entry)
                continue
            filtered = [
                h for h in entry_hooks
                if not (isinstance(h, dict) and _is_managed(str(h.get("command", ""))))
            ]
            if filtered:
                kept.append({**entry, "hooks": filtered})
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)


def install_platform_hooks(*, platform: str, target_dir: str | Path | None = None,
                           uninstall: bool = False, dry_run: bool = False,
                           cli: str | None = None) -> dict[str, Any]:
    """Install or remove managed prefs hooks for a platform.

    The merge is surgical and idempotent: only entries pointing at
    ``fulcra-prefs-hooks`` are replaced/removed.
    """
    if platform not in _SUPPORTED:
        raise ValueError(f"unsupported platform: {platform}")

    root = _target_dir(platform, target_dir)
    hooks_dir = root / MANAGED_DIRNAME
    config_path = _config_path(platform, root)
    cli = cli or _resolved_cli()
    capture_events = _CAPTURE_EVENTS[platform]
    managed_events = {"SessionStart", *capture_events}
    plan: dict[str, Any] = {
        "platform": platform,
        "config": str(config_path),
        "hooks_dir": str(hooks_dir),
        "uninstall": uninstall,
        "events": sorted(managed_events),
    }

    config = _load_config(config_path, dry_run=dry_run)
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    else:
        hooks = dict(hooks)
    _strip_managed(hooks, managed_events)

    if not uninstall:
        session_start = str(hooks_dir / SESSION_START_SCRIPT)
        hooks.setdefault("SessionStart", []).append({
            "hooks": [{"type": "command", "command": session_start}]
        })
        capture = str(hooks_dir / CAPTURE_SCRIPT)
        for event in capture_events:
            hooks.setdefault(event, []).append({
                "hooks": [{"type": "command", "command": capture}]
            })

    config["hooks"] = hooks
    plan["would_write"] = config
    if dry_run:
        return plan

    root.mkdir(parents=True, exist_ok=True)
    if not uninstall:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        scripts = {
            SESSION_START_SCRIPT: _script_session_start(platform, cli),
            CAPTURE_SCRIPT: _script_capture(platform, cli),
        }
        for name, body in scripts.items():
            path = hooks_dir / name
            path.write_text(body)
            path.chmod(0o755)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return plan
