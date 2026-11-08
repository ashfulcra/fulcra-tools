"""Private delivery-state namespaces, separate from stable Gmail identities."""
import hashlib
import json
from pathlib import Path

from fulcra_common.json_values import snapshot_json
from fulcra_common.v1 import _canonical_uuid

from .ledger import ACTION_FILE, ACTION_RELAY, _default_root


def delivery_root(fulcra_account_id: str, rule, relay_team: str | None, *, root: Path | None = None) -> Path:
    """Select a destination-bound ledger/cursor root without creating it.

    Source account IDs, Files paths and canonical relay outbox identities stay
    unchanged. Old source-only ledgers remain evidence, never implicit proof of
    delivery to a captured Fulcra account or a newly selected relay target.
    """
    account = _canonical_uuid(fulcra_account_id)
    actions = [action for action in (ACTION_FILE, ACTION_RELAY) if action in rule.actions]
    if not actions or set(rule.actions) != set(actions):
        raise ValueError("Gmail delivery actions are invalid")
    target = {"schema_version": 1, "fulcra_account_id": account, "actions": actions}
    if ACTION_RELAY in actions:
        if relay_team is not None and (not isinstance(relay_team, str) or not relay_team.strip()):
            raise ValueError("Gmail relay team is invalid")
        target["relay"] = {"team": relay_team, "assignee": rule.relay_to}
    encoded = json.dumps(snapshot_json(target), sort_keys=True, separators=(",", ":")).encode()
    key = hashlib.sha256(encoded).hexdigest()
    return (Path(root) if root is not None else _default_root()) / "gmail-delivery-v1" / key
