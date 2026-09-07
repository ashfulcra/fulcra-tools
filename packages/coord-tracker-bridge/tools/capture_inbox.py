#!/usr/bin/env python3
"""Capture ONE real Linear issues response and stamp its own provenance.

WHY THIS EXISTS. `tests/test_inbox.py` pins field names against a real
response, and that pin is only worth anything if the fixture's provenance was
MEASURED rather than typed. A fixture labelled "captured from 0.1.40" that came
from 0.1.38 cost the sealed-secrets lane a review round; the fix there was a
capture tool that stamps what it observed, and this is the same tool for this
surface.

It is also read-only by construction: it drives the same `ReadOnlyTransport` the
verb uses, so a capture run cannot mutate the user's board either.

Usage (needs LINEAR_API_KEY and a team id; neither is ever written to the
fixture):

    python tools/capture_inbox.py --team-id <TEAM>
"""
import argparse
import json
import re
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coord_tracker_bridge.inbox import INBOX_QUERY, ReadOnlyTransport  # noqa: E402
from coord_tracker_bridge.linear import HttpxGraphQLTransport  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(os.path.dirname(HERE), "tests", "fixtures", "real_linear_issues.json")

#: Fields we must never write into a fixture that lands in the repository.
#: Titles and descriptions are private workspace content. Field names and
#: state types are what the contract tests pin. Resource identities and
#: activity timestamps are replaced separately before publication.
_REDACT = ("title", "description", "url")

#: Label values that identify the fleet rather than describe the work.
#: coord-boss's ruling on the first capture: this fixture lands in a PUBLIC
#: repo, and the fleet has a standing task for exactly this class
#: (separation-sweep-2, public docs carrying fleet identifiers). Agent names are
#: borderline — they appear throughout this repo already — but a hostname
#: fragment is over the line, and one rule covers both.
#:
#: `kind:*`, `lane:*` and other generic vocabulary stay intact: they describe
#: the work, they leak nothing, and the tests legitimately exercise them.
_AGENT_LABEL = re.compile(r"^agent:", re.IGNORECASE)
#: A dotted token with a TLD-ish tail, e.g. "Example-Laptop.localdomain".
#: Matched anywhere in the value, because the identifier that prompted this rule
#: was a suffix on an agent label rather than a bare hostname.
_HOSTNAME_ISH = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\.[A-Za-z][A-Za-z0-9-]{1,}")


# Only public fixture vocabulary survives; custom workspace label names can
# contain private project names or principals even when they are not hostnames.
_PUBLIC_LABELS = frozenset({
    "kind:directive", "kind:task", "kind:idea", "kind:missed",
    "lane:active", "lane:blocked", "lane:backlog", "lane:asks", "lane:missed",
    "lane:threads-missed", "origin:user", "origin:fleet", "blocked-on-user",
    "P1", "P2", "P3", "qa-answer", "type:both", "type:factual",
})


def redact_label(name):
    """Keep public fixture vocabulary and replace private/custom labels."""
    if not isinstance(name, str):
        return name
    if _AGENT_LABEL.search(name):
        return "<redacted agent label>"
    if _HOSTNAME_ISH.search(name):
        return "<redacted host label>"
    if name in _PUBLIC_LABELS:
        return name
    if name.startswith("origin:"):
        return "origin:user"
    return "<redacted label>"


def redact(node):
    out = dict(node)
    for key in _REDACT:
        if key in out:
            out[key] = f"<redacted {key}>"
    if isinstance(out.get("assignee"), dict):
        out["assignee"] = {"displayName": "<redacted person>"}
    labels = out.get("labels")
    if isinstance(labels, dict) and isinstance(labels.get("nodes"), list):
        out["labels"] = dict(labels)
        out["labels"]["nodes"] = [
            ({**entry, "name": redact_label(entry.get("name"))}
             if isinstance(entry, dict) else entry)
            for entry in labels["nodes"]
        ]
    return out



def sanitize_response(response):
    """Preserve the API shape with synthetic issue identity and activity values.

    Counter-based replacements are independent of the private values: hashing
    identifiers would still preserve a link to the original workspace.
    """
    body = json.loads(json.dumps(response))
    issues = body.get("data", {}).get("issues", {})
    sanitized = []
    for index, node in enumerate(issues.get("nodes", []), 1):
        out = redact(node)
        if "id" in out:
            out["id"] = f"00000000-0000-4000-8000-{index:012d}"
        if "identifier" in out:
            out["identifier"] = f"EXAMPLE-{index}"
        if "updatedAt" in out:
            out["updatedAt"] = f"2024-01-{1 + (index - 1) // 24:02d}T{(index - 1) % 24:02d}:00:00.000Z"
        sanitized.append(out)
    issues["nodes"] = sanitized
    page = issues.get("pageInfo")
    if isinstance(page, dict) and page.get("endCursor"):
        page["endCursor"] = "synthetic-page-cursor"
    return body


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

    body = sanitize_response(response.body)
    nodes = body.get("data", {}).get("issues", {}).get("nodes", [])

    stamped = {
        # The API/query provenance is measured; time and resource values are
        # synthetic so the fixture carries no private activity history.
        "captured_from": "linear.app",
        "captured_at": "2024-01-01T00:00:00+00:00",
        "fixture_privacy": "Captured schema; identities and activity/capture timestamps are synthetic.",
        "operation": "CoordInbox",
        "query": INBOX_QUERY,
        "node_count": len(nodes),
        "redacted_fields": list(_REDACT) + [
            "assignee.displayName", "labels.nodes.name(agent:*)",
            "labels.nodes.name(hostname-shaped)",
            "id", "identifier", "updatedAt", "pageInfo.endCursor"],
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
