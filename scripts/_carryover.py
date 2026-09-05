"""How many rows the previous plan named are STILL proposed after the sync.

Used by linear-sync.sh. Counting changes cannot distinguish "the sync did not
settle" from "the fleet wrote more rows while we were running", and on a cadence
the second happens constantly -- so a count-based check fires until nobody reads
it. Identity can tell them apart: a row carried over from the plan we just
applied is the real failure this check exists for.
"""

import json
import sys


def keys(path: str) -> set[tuple[str, str, str]]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return {
        (c["source"]["provider"], c["source"]["namespace"], c["source"]["item_id"])
        for c in payload["changes"]
    }


def main() -> int:
    before, after = keys(sys.argv[1]), keys(sys.argv[2])
    carried = sorted(before & after)
    print(len(carried))
    for _provider, namespace, item in carried[:10]:
        print(f"    {namespace} {item}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
