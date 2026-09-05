"""Resolve the new bus's channel from its config document (G15). No default channel."""
from __future__ import annotations

import json

from .transport import PointerTransport

CONFIG_PATH = "team/{team}/_coord/bus-v4/records.json"
_REQUIRED = ("data_type", "api_version")


class ChannelUnresolved(RuntimeError):
    pass


def config_path(team: str) -> str:
    return CONFIG_PATH.format(team=team)


def resolve(reader: PointerTransport, team: str) -> dict[str, str]:
    body, state = reader.read_classified(config_path(team))
    if state != "ok" or body is None:
        raise ChannelUnresolved(f"bus-v4 config for team {team}: {state}")
    try:
        cfg = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ChannelUnresolved(f"bus-v4 config unparsable: {exc}") from exc
    missing = [k for k in _REQUIRED if not cfg.get(k)]
    if missing:
        raise ChannelUnresolved(f"bus-v4 config missing {missing}")
    return {k: str(cfg[k]) for k in _REQUIRED}
