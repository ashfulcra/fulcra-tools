"""Lossless JSON snapshots for private record payloads and durable intents."""
import json
import math


def snapshot_json(value):
    """Freeze JSON without silently coercing nested keys or invalid Unicode.

    Python's encoder accepts integer/bool/null object keys and stringifies them,
    even when that collides with a real string key. It also accepts unpaired
    surrogates. Reject those values before metadata, storage or transport effects.
    Tuples retain the existing JSON-array convention. Error text contains no
    private payload values. Shared acyclic containers remain valid.
    """
    try:
        pending, seen = [value], set()
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, (dict, list, tuple)):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                if isinstance(item, dict):
                    if any(not isinstance(key, str) for key in item):
                        raise ValueError
                    pending.extend(item)
                    pending.extend(item.values())
                else:
                    pending.extend(item)
            elif item is None or isinstance(item, (bool, int)):
                pass
            elif isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError
            else:
                raise ValueError
        return json.loads(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise ValueError("JSON requires string keys, valid Unicode and finite supported values") from None
