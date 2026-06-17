"""Platform hook installers for fulcra-vault.

The hook contract is intentionally small: a SessionStart hook injects the
vault's ``HOT.md`` (the compact session-start summary) as hook context, so an
agent sees the hot notes at the top of every session. Fail-safe: any error or
an empty/absent HOT injects nothing and never breaks session start.

Mirrors the fulcra-prefs installer's managed-config approach: the merge is
surgical and idempotent — only entries pointing at ``fulcra-vault-hooks`` are
replaced or removed, leaving any user-authored hooks untouched.
"""
from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any


MANAGED_DIRNAME = "fulcra-vault-hooks"
SESSION_START_SCRIPT = "session-start.sh"

_SUPPORTED = {"claude-code", "codex"}


def _resolved_cli() -> str:
    exe = shutil.which("fulcra-vault")
    if exe:
        return exe
    argv0 = Path(sys.argv[0])
    if argv0.name == "fulcra-vault" and argv0.exists():
        return str(argv0)
    return "fulcra-vault"


def _script_session_start(cli: str) -> str:
    qcli = shlex.quote(cli)
    return f"""#!/usr/bin/env bash
# fulcra-vault SessionStart hook. Fail-safe: emit nothing on any error.
set +e
FULCRA_VAULT={qcli}
HOT="$("$FULCRA_VAULT" read HOT 2>/dev/null)"
[ -z "$HOT" ] && exit 0
python3 - "$HOT" <<'PY' 2>/dev/null
import json, sys
ctx = sys.argv[1]
print(json.dumps({{"hookSpecificOutput": {{
    "hookEventName": "SessionStart",
    "additionalContext": ctx,
}}}}))
PY
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
    return MANAGED_DIRNAME in command


def _load_config(path: Path) -> dict[str, Any]:
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
    """Install or remove the managed vault SessionStart hook for a platform.

    The merge is surgical and idempotent: only entries pointing at
    ``fulcra-vault-hooks`` are replaced/removed.
    """
    if platform not in _SUPPORTED:
        raise ValueError(f"unsupported platform: {platform}")

    root = _target_dir(platform, target_dir)
    hooks_dir = root / MANAGED_DIRNAME
    config_path = _config_path(platform, root)
    cli = cli or _resolved_cli()
    managed_events = {"SessionStart"}
    plan: dict[str, Any] = {
        "platform": platform,
        "config": str(config_path),
        "hooks_dir": str(hooks_dir),
        "uninstall": uninstall,
        "events": sorted(managed_events),
    }

    config = _load_config(config_path)
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

    config["hooks"] = hooks
    plan["would_write"] = config
    if dry_run:
        return plan

    root.mkdir(parents=True, exist_ok=True)
    if not uninstall:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        path = hooks_dir / SESSION_START_SCRIPT
        path.write_text(_script_session_start(cli))
        path.chmod(0o755)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return plan
