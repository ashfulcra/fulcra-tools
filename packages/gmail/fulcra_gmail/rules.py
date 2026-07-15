"""The local rules engine — parse rules, build the server ``q``, post-filter.

Everything here runs entirely on the operator's machine. A rule has two
halves:

* a **server-side** ``match`` (a Gmail ``q`` string) that minimizes the
  candidate surface pulled from the API, and
* **local post-filters** (``from_regex`` / ``subject_regex`` /
  ``has_attachment``) that refine candidates into *effective matches*.

Rule identity is ``(id, version)``. Bumping ``version`` starts a fresh
processed set downstream (the ledger keys on it), so a match/action change
never silently inherits an incompatible cursor.

**Privacy (B2 — the #1 gate).** :func:`evaluate` NEVER logs subject / from /
body / snippet at ANY level. The only DEBUG-loggable facts about a message are
``account_id``, the opaque Gmail ``message_id``, ``rule_id``, and the boolean
decision + its reason-code. Header/body values decoded for filtering stay in
memory and never reach a log sink.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from . import convert

_log = logging.getLogger("fulcra_gmail.rules")

#: Fixed overlap subtracted from an established cursor when building ``q`` —
#: re-scans the last 24h so an outage or out-of-order delivery never leaves a
#: hole (see the plan's Sync model).
OVERLAP_SECONDS = 24 * 3600
#: Default first-run window when a (account, rule) has no cursor yet.
DEFAULT_BACKFILL_DAYS = 7
#: The action verbs a rule may request.
VALID_ACTIONS = ("file", "relay")

_REQUIRED_FIELDS = ("id", "version", "name", "match", "actions")


class MatchReason(str, Enum):
    """Why :func:`evaluate` accepted or rejected a candidate. String-valued so
    it serializes/logs as a stable opaque token with no PII."""

    MATCHED = "matched"
    REJECTED_FROM_REGEX = "rejected:from_regex"
    REJECTED_SUBJECT_REGEX = "rejected:subject_regex"
    REJECTED_HAS_ATTACHMENT = "rejected:has_attachment"


@dataclass(frozen=True)
class MatchDecision:
    """The result of evaluating one message against one rule."""

    matched: bool
    reason: MatchReason


@dataclass(frozen=True)
class Rule:
    """A parsed relay rule. Identity is ``(id, version)``."""

    id: str
    version: int
    name: str
    match: str
    actions: list[str]
    #: Raw pattern strings kept for serialization/debugging. The COMPILED
    #: objects (``from_regex_re`` / ``subject_regex_re``) are what
    #: :func:`evaluate` matches against — they are compiled + validated once,
    #: at parse time, so a malformed operator pattern fails config validation
    #: instead of raising ``re.error`` mid-poll.
    from_regex: str | None = None
    subject_regex: str | None = None
    has_attachment: bool | None = None
    relay_to: str | None = None
    relay_priority: str | None = None
    #: ``None`` == applies to ALL authorized accounts. Otherwise a list of
    #: account_ids and/or email addresses.
    accounts: list[str] | None = None
    #: First-run window widener (in days). ``None`` == default 7d.
    backfill: int | None = None
    #: Whether the poller applies this rule. Absent in older configs ⇒ enabled.
    enabled: bool = True
    #: Precompiled post-filter patterns (``None`` when the raw field is unset).
    #: ``compare=False`` so equality/repr stay driven by the raw strings.
    from_regex_re: "re.Pattern[str] | None" = field(default=None, compare=False)
    subject_regex_re: "re.Pattern[str] | None" = field(default=None, compare=False)

    def applies_to_account(self, account_id: str, email: str) -> bool:
        """True if this rule targets the given account (by id or email).

        An omitted ``accounts`` list means "all authorized accounts".
        """
        if self.accounts is None:
            return True
        targets = {t.strip().lower() for t in self.accounts}
        return account_id.strip().lower() in targets or email.strip().lower() in targets


def rule_identity(rule: Rule) -> tuple[str, int]:
    """The downstream cursor/processed-set key: ``(rule_id, rule_version)``."""
    return (rule.id, rule.version)


def parse_rules(raw_rules: list[dict]) -> list[Rule]:
    """Parse TOML-shaped rule dicts into :class:`Rule` objects.

    Raises :class:`ValueError` on a missing required field
    (``id``/``version``/``name``/``match``/``actions``), an unknown action, or
    a malformed post-filter regex — the last so an operator's bad pattern fails
    at parse/startup naming the rule id + field, rather than raising
    ``re.error`` mid-poll and blocking otherwise-valid mail.
    """
    parsed: list[Rule] = []
    for raw in raw_rules:
        for required in _REQUIRED_FIELDS:
            if required not in raw:
                raise ValueError(f"rule missing required field: {required!r}")
        actions = list(raw["actions"])
        for action in actions:
            if action not in VALID_ACTIONS:
                raise ValueError(
                    f"rule {raw['id']!r} has unknown action {action!r} "
                    f"(allowed: {VALID_ACTIONS})"
                )
        # A rule that asks to relay MUST name a recipient. Rejecting this at
        # parse time (not silently dropping the relay mid-poll) means a
        # misconfigured relay rule fails loudly at startup — it can never mark a
        # message "done" without ever relaying it and then advance its cursor
        # past emails that should have been relayed (B1/B3 safety).
        if "relay" in actions and not raw.get("relay_to"):
            raise ValueError(
                f"rule {raw['id']!r} has a 'relay' action but no relay_to — "
                f"set relay_to to the recipient agent"
            )
        from_regex = raw.get("from_regex")
        subject_regex = raw.get("subject_regex")
        from_regex_re = _compile_pattern(raw["id"], "from_regex", from_regex)
        subject_regex_re = _compile_pattern(raw["id"], "subject_regex", subject_regex)
        accounts = raw.get("accounts")
        enabled = bool(raw.get("enabled", True))
        parsed.append(Rule(
            id=str(raw["id"]),
            version=int(raw["version"]),
            name=str(raw["name"]),
            match=str(raw["match"]),
            actions=actions,
            from_regex=from_regex,
            subject_regex=subject_regex,
            has_attachment=raw.get("has_attachment"),
            relay_to=raw.get("relay_to"),
            relay_priority=raw.get("relay_priority"),
            accounts=list(accounts) if accounts is not None else None,
            backfill=raw.get("backfill"),
            enabled=enabled,
            from_regex_re=from_regex_re,
            subject_regex_re=subject_regex_re,
        ))
    return parsed


def _compile_pattern(
    rule_id: object, field_name: str, pattern: str | None
) -> "re.Pattern[str] | None":
    """Compile an optional post-filter pattern at parse time.

    Returns ``None`` when the field is unset. Re-raises a malformed pattern as
    a :class:`ValueError` naming the offending rule id AND field, so config
    validation catches it (never ``re.error`` inside :func:`evaluate`).
    """
    if pattern is None:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(
            f"rule {rule_id!r}: invalid {field_name}: {exc}"
        ) from exc


def build_query(rule: Rule, *, cursor_epoch: int | None = None) -> str:
    """Build the server ``q`` for one (account, rule) poll.

    * With an established ``cursor_epoch``: ``"<match> after:<cursor-24h>"`` —
      the fixed 24h overlap re-scans recent history so nothing is skipped.
    * First run (``cursor_epoch is None``): bounded to ``newer_than:7d``, or
      ``newer_than:<backfill>d`` when the rule sets ``backfill`` to widen the
      initial window.
    """
    if cursor_epoch is not None:
        after = cursor_epoch - OVERLAP_SECONDS
        return f"{rule.match} after:{after}"
    days = rule.backfill if rule.backfill is not None else DEFAULT_BACKFILL_DAYS
    return f"{rule.match} newer_than:{days}d"


def evaluate(rule: Rule, message: dict, *, account_id: str) -> MatchDecision:
    """Decide whether a fetched message is an EFFECTIVE match for ``rule``.

    Post-filters are checked in a fixed order — ``from_regex``, then
    ``subject_regex``, then ``has_attachment`` — and the FIRST failing filter
    short-circuits with its reason-code.

    Privacy: only opaque facts (account_id, message_id, rule_id, decision +
    reason) are logged, at DEBUG. Decoded header/body values never leave this
    function.
    """
    payload = message.get("payload") or {}
    reason = MatchReason.MATCHED

    if rule.from_regex_re is not None:
        from_value = convert.get_header(payload, "From") or ""
        if rule.from_regex_re.search(from_value) is None:
            reason = MatchReason.REJECTED_FROM_REGEX

    if reason is MatchReason.MATCHED and rule.subject_regex_re is not None:
        subject_value = convert.get_header(payload, "Subject") or ""
        if rule.subject_regex_re.search(subject_value) is None:
            reason = MatchReason.REJECTED_SUBJECT_REGEX

    if reason is MatchReason.MATCHED and rule.has_attachment is not None:
        present = convert.has_attachment(payload)
        if present != rule.has_attachment:
            reason = MatchReason.REJECTED_HAS_ATTACHMENT

    matched = reason is MatchReason.MATCHED
    # PRIVACY: opaque ids + decision only — never subject/from/body/snippet.
    _log.debug(
        "gmail rule decision account=%s message=%s rule=%s matched=%s reason=%s",
        account_id, message.get("id"), rule.id, matched, reason.value,
    )
    return MatchDecision(matched=matched, reason=reason)


def rule_to_config_dict(rule: Rule) -> dict:
    """Serialize a Rule back to its config (TOML) dict shape.

    Inverse of one ``parse_rules`` element: emits only set/non-default fields
    and never the compiled ``*_re`` patterns, so the result round-trips
    through ``parse_rules`` to an equal Rule.
    """
    out: dict = {
        "id": rule.id,
        "version": rule.version,
        "name": rule.name,
        "match": rule.match,
        "actions": list(rule.actions),
    }
    if rule.from_regex is not None:
        out["from_regex"] = rule.from_regex
    if rule.subject_regex is not None:
        out["subject_regex"] = rule.subject_regex
    if rule.has_attachment is not None:
        out["has_attachment"] = rule.has_attachment
    if rule.relay_to is not None:
        out["relay_to"] = rule.relay_to
    if rule.relay_priority is not None:
        out["relay_priority"] = rule.relay_priority
    if rule.accounts is not None:
        out["accounts"] = list(rule.accounts)
    if rule.backfill is not None:
        out["backfill"] = rule.backfill
    if rule.enabled is not True:
        out["enabled"] = rule.enabled
    return out


def rule_summary(rule: Rule) -> str:
    """One-line, PII-free human summary for the rule list UI."""
    parts = [f"q={rule.match}"]
    if rule.from_regex:
        parts.append(f"from~{rule.from_regex}")
    if rule.subject_regex:
        parts.append(f"subject~{rule.subject_regex}")
    if rule.has_attachment:
        parts.append("has_attachment")
    tail = ",".join(rule.actions)
    if "relay" in rule.actions and rule.relay_to:
        tail += f"→{rule.relay_to}"
    return " · ".join(parts) + " · " + tail
