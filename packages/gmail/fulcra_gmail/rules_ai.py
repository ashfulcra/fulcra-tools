"""Opt-in AI rule suggestion. The ONLY off-device path in the builder.

Sends the ``from``/``subject``/``snippet`` of the operator's labeled examples
(never full bodies, never other inbox content) to a model and parses back a rule
in the existing schema. The model call is an injected ``call_model(prompt)->str``
seam so this is unit-testable without any network; the route gates it behind an
explicit ``consent: true``.
"""
from __future__ import annotations

import json

from . import rules as rules_mod

_SCHEMA_HINT = (
    'Return ONLY JSON: {"draft_rule": {"id","version","name","match",'
    '"actions",["from_regex","subject_regex","has_attachment"]?}, "explanation": "..."}. '
    '"match" is a Gmail search query; actions is a subset of ["file","relay"].'
)


def _fmt(records: list[dict], label: str) -> str:
    lines = [f"{label}:"]
    for r in records:
        lines.append(f"- from={r.get('from','')} | subject={r.get('subject','')} "
                     f"| snippet={r.get('snippet','')}")
    return "\n".join(lines)


def build_prompt(positives: list[dict], negatives: list[dict]) -> str:
    return (
        "Draft a Gmail filter rule that matches the SHOULD-MATCH examples and "
        "excludes the SHOULD-NOT examples.\n\n"
        + _fmt(positives, "SHOULD-MATCH") + "\n\n"
        + _fmt(negatives, "SHOULD-NOT") + "\n\n" + _SCHEMA_HINT
    )


def suggest(positives: list[dict], negatives: list[dict], *, call_model) -> dict:
    raw = call_model(build_prompt(positives, negatives))
    try:
        parsed = json.loads(raw)
        draft = parsed["draft_rule"]
        explanation = str(parsed.get("explanation", ""))
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError(f"model returned unparseable rule: {e}") from e
    rules_mod.parse_rules([draft])  # raises ValueError on an invalid rule
    return {"draft_rule": draft, "explanation": explanation}
