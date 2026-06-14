"""Conservative text-to-candidate extraction.

This module turns explicit user preference statements into the same candidate
specs accepted by ``capture-batch``. It is intentionally narrow: ambiguous task
instructions produce no candidates.
"""
from __future__ import annotations

import re
from typing import Any


EXPLICIT_RE = re.compile(
    r"\b("
    r"from now on|remember that|remember this|my preference is|"
    r"i prefer|i want|i need|i like|"
    r"i don't want|i do not want|i dislike|i hate|"
    r"we prefer|we want|we need"
    r")\b",
    re.IGNORECASE,
)
NEGATIVE_RE = re.compile(r"\b(i don't want|i do not want|i dislike|i hate)\b",
                         re.IGNORECASE)
SENSITIVE_RE = re.compile(
    r"\b(password|passcode|token|api key|secret|credential|ssn|"
    r"medical|diagnosis|bank|credit card)\b",
    re.IGNORECASE,
)
# PII that must never be stored verbatim in a shareable/injectable record.
# The stored value is the raw sentence, so a sentence carrying PII is skipped
# entirely (conservative, like SENSITIVE_RE) rather than partially redacted.
PII_RE = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w.-]+)?"                      # email (TLD optional)
    r"|\b(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b",  # phone
    re.IGNORECASE,
)
# Disavowed / reported / hypothetical statements: the user is negating the act
# of stating or assuming a preference, or quoting someone — not expressing one.
# Targets negated META verbs (say/mean/assume/claim) and reported "said", so it
# does NOT catch valid aversions like "I don't want X" (handled by NEGATIVE_RE).
DISAVOW_RE = re.compile(
    r"\b(?:never|do(?:es)?\s*n'?t|do(?:es)?\s+not|did\s*n'?t|did\s+not)\s+"
    r"(?:say|said|mean|meant|assume|claim)\b"
    r"|\bnever\s+(?:said|claimed|meant)\b"
    r"|\bnot\s+assume\b"
    r"|\bsaid\b",                                         # reported speech
    re.IGNORECASE,
)
NON_ASSERTIVE_RE = re.compile(
    r"^\s*(?:if|when|suppose)\s+(?:i|we)\s+"
    r"(?:prefer|want|need|like|dislike|hate)\b"
    r"|^\s*do\s+(?:i|we)\s+"
    r"(?:prefer|want|need|like|dislike|hate)\b"
    r"|\b(?:ask|tell)\s+me\s+(?:if|whether)\s+(?:i|we)\s+"
    r"(?:prefer|want|need|like|dislike|hate)\b"
    r"|\blet\s+me\s+know\s+(?:if|whether)\s+(?:i|we)\s+"
    r"(?:prefer|want|need|like|dislike|hate)\b",
    re.IGNORECASE,
)
SENTENCE_RE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")


def extract_candidates(text: str, *, platform: str, session: str,
                       agent: str | None = None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for sentence in _sentences(text):
        if not EXPLICIT_RE.search(sentence):
            continue
        if SENSITIVE_RE.search(sentence) or PII_RE.search(sentence):
            continue
        if DISAVOW_RE.search(sentence) or NON_ASSERTIVE_RE.search(sentence):
            continue
        key = _classify_key(sentence)
        if key is None:
            continue
        strength = -0.8 if NEGATIVE_RE.search(sentence) else 0.8
        value = _value_for(key, sentence, strength)
        dedup = (key, str(value))
        if dedup in seen:
            continue
        seen.add(dedup)
        candidates.append({
            "key": key,
            "value": value,
            "strength": strength,
            "kind": "preference",
            "scope": "global",
            "confidence": 0.9,
            "half_life_days": 180.0,
            "platform": platform,
            "agent": agent,
            "session": session,
            "supersedes": None,
        })
    return candidates


def _sentences(text: str) -> list[str]:
    return [
        match.group(0).strip()
        for match in SENTENCE_RE.finditer(text or "")
        if match.group(0).strip()
    ]


def _classify_key(sentence: str) -> str | None:
    s = sentence.lower()
    if any(term in s for term in ("documentation", "docs", "readme", "agent guide")):
        if "human" in s and "agent" in s:
            return "docs.style.human_agent_quality"
        return "docs.style.documentation"
    if any(term in s for term in ("tone", "concise", "brief", "short", "verbose")):
        return "comms.tone"
    if "plan" in s and any(term in s for term in ("todo", "implementation", "implement")):
        return "process.plan_before_implementation"
    if "research" in s and any(term in s for term in ("constraint", "architecture", "platform")):
        return "process.research_platform_constraints_first"
    if "review" in s and any(term in s for term in ("arc", "code", "pr", "pull request")):
        return "process.review"
    return None


def _value_for(key: str, sentence: str, strength: float) -> dict[str, Any]:
    if key == "comms.tone":
        return {"preference": sentence, "preferred": strength > 0}
    return {"preference": sentence}
