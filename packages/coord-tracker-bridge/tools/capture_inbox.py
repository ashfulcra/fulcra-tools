#!/usr/bin/env python3
"""Capture ONE real Linear issues response and stamp its own provenance.

WHY THIS EXISTS. `tests/test_inbox.py` pins field names against a real
response, and that pin is only worth anything if the fixture's provenance was
MEASURED rather than typed. A fixture labelled "captured from 0.1.40" that came
from 0.1.38 cost the sealed-secrets lane a review round; the fix there was a
capture tool that stamps what it observed, and this is the same tool for this
surface.

It is also read-only by construction: it drives the same `ReadOnlyTransport` the
verb uses, so a capture run cannot mutate Ash's board either.

Usage (needs LINEAR_API_KEY and a team id; neither is ever written to the
fixture):

    python tools/capture_inbox.py --team-id <TEAM>
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coord_tracker_bridge.inbox import INBOX_QUERY, ReadOnlyTransport  # noqa: E402
from coord_tracker_bridge.linear import HttpxGraphQLTransport  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(os.path.dirname(HERE), "tests", "fixtures", "real_linear_issues.json")

#: Fields we must never write into a fixture that lands in the repository.
#: Titles and descriptions are Ash's private workspace content; identifiers and
#: state names are what the tests actually pin.
_REDACT = ("title", "description", "url")


def redact(node):
    out = dict(node)
    for key in _REDACT:
        if key in out:
            out[key] = f"<redacted {key}>"
    if isinstance(out.get("assignee"), dict):
        out["assignee"] = {"displayName": "<redacted person>"}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team-id", default=os.environ.get("LINEAR_TEAM_ID"))
    args = ap.parse_args()

    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key or not args.team_id:
        print("REFUSING: LINEAR_API_KEY and --team-id/LINEAR_TEAM_ID are required. "
              "An unstamped or hand-written fixture is the defect this tool exists "
              "to prevent, so there is no offline mode.", file=sys.stderr)
        return 2

    transport = ReadOnlyTransport(HttpxGraphQLTransport(api_key))
    response = transport.post({
        "operationName": "CoordInbox",
        "query": INBOX_QUERY,
        "variables": {"team": args.team_id, "after": None},
    })
    if response.status_code >= 400 or (response.body or {}).get("errors"):
        print(f"capture failed: status={response.status_code}", file=sys.stderr)
        return 1

    body = json.loads(json.dumps(response.body))       # plain dict
    nodes = body.get("data", {}).get("issues", {}).get("nodes", [])
    body["data"]["issues"]["nodes"] = [redact(n) for n in nodes]

    stamped = {
        # MEASURED at capture time, not typed by the runner.
        "captured_from": "linear.app",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "operation": "CoordInbox",
        "query": INBOX_QUERY,
        "node_count": len(nodes),
        "redacted_fields": list(_REDACT) + ["assignee.displayName"],
        "response": body,
    }
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w", encoding="utf-8") as fh:
        json.dump(stamped, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"captured {FIXTURE}: {len(nodes)} node(s), payload fields redacted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
