"""The crash-safe poll pipeline — B1 contiguous frontier + ordered effects.

This is where a poll actually happens, for one ``(account, rule)`` pair:

1. **Query** ``messages.list`` with the rule's server ``q`` and the 24h-overlap
   cursor (fully paginated by the client).
2. **Refine** each candidate to an *effective match* via
   :func:`fulcra_gmail.rules.evaluate` — locally-rejected candidates are dropped
   here and produce ZERO residue (no File, no ledger, no bus, no PII in logs).
3. **Order** effective matches deterministically oldest-first by
   ``(internalDate, message_id)`` — the API guarantees no page order, so we never
   trust it.
4. **Process** each through the ordered, ledger-barriered effect pipeline
   (:func:`process_message`): ``file → ledger → relay → ledger``.
5. **Advance** the watermark ONLY through the contiguous prefix of fully-done
   matches; stop at the first incomplete one so no hole is ever skipped.

**Crash safety.** Every effect is followed by a durable ledger append before the
next effect runs. A crash between any two barriers leaves the message *incomplete*
in the ledger; the next run re-derives its remaining actions and resumes — the
file is idempotent by path, the relay idempotent by coord slug. A ``crash`` hook
is injected at each barrier so tests can force a crash at an exact point.

**Privacy (B2).** Only opaque facts reach a log sink: ``account_id``, opaque
``message_id``, ``rule_id``, and decision/counters. Subject / from / body / snippet
never touch a logger, at any level.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from . import convert
from .files_writer import FilesWriter
from .ledger import ACTION_FILE, ACTION_RELAY, Ledger, LedgerEntry, outbox_key
from .relay import RelayEmitterProtocol, build_directive
from .rules import Rule, evaluate

_log = logging.getLogger("fulcra_gmail.pipeline")

#: Canonical effect order — file BEFORE relay (relay only after file-done).
_CANONICAL_ACTIONS = (ACTION_FILE, ACTION_RELAY)


class InjectedCrash(RuntimeError):
    """Raised by a test's ``crash`` hook to simulate a mid-pipeline crash."""


def _crash(hook: Callable[[str], None] | None, label: str) -> None:
    if hook is not None:
        hook(label)


def internal_seconds(message: dict) -> int:
    """Gmail ``internalDate`` (ms string) as epoch seconds; 0 if absent."""
    raw = message.get("internalDate")
    try:
        return int(raw) // 1000
    except (TypeError, ValueError):
        return 0


def required_actions(rule: Rule) -> list[str]:
    """The effect actions a rule requires, in canonical (file-before-relay) order.

    Drops ``relay`` when the rule has no ``relay_to`` (a misconfiguration): the
    cursor keeps advancing on the ``file`` effect instead of blocking forever on
    an un-routable relay. Logged (opaque rule id only)."""
    acts = [a for a in _CANONICAL_ACTIONS if a in rule.actions]
    if ACTION_RELAY in acts and not rule.relay_to:
        _log.warning(
            "gmail: rule %s requests relay but sets no relay_to — relay skipped",
            rule.id,
        )
        acts = [a for a in acts if a != ACTION_RELAY]
    return acts


def process_message(
    message: dict,
    *,
    rule: Rule,
    account_id: str,
    ledger: Ledger,
    files_writer: FilesWriter,
    relay_emitter: RelayEmitterProtocol | None,
    crash: Callable[[str], None] | None = None,
) -> bool:
    """Run the ordered, ledger-barriered effect pipeline for one effective match.

    Returns ``True`` iff every action the rule requires is ``done`` in the ledger
    after this call. Only the actions still missing a ``done`` entry are
    re-executed, so a file already written is never re-filed and a relay already
    delivered is never re-emitted (idempotent recovery).
    """
    message_id = str(message.get("id"))
    rid, rver = rule.id, rule.version
    required = required_actions(rule)
    if relay_emitter is None:
        # No relay backend (no coord team configured) — file-only. The cursor
        # still advances on the file effect rather than blocking on a relay it
        # cannot deliver.
        required = [a for a in required if a != ACTION_RELAY]
    remaining = ledger.remaining_actions(message_id, rid, rver, required)
    if not remaining:
        return True

    _crash(crash, "before_first_effect")

    if ACTION_FILE in remaining:
        selected = convert.to_selected_email(message)
        result = files_writer.write(
            account_id, message_id, message.get("internalDate"), selected
        )
        ledger.append(LedgerEntry.file_done(
            account_id=account_id, message_id=message_id, rule_id=rid,
            rule_version=rver, sha256=result.sha256, destination=result.path,
        ))
        _crash(crash, "after_file_done")

    if ACTION_RELAY in remaining:
        key = outbox_key(account_id, message_id, rid, rver)
        ledger.append(LedgerEntry.relay_pending(
            account_id=account_id, message_id=message_id, rule_id=rid,
            rule_version=rver, outbox_key=key,
        ))
        _crash(crash, "after_relay_pending")
        directive = build_directive(key, rule)
        emit = relay_emitter.emit(directive)
        if not emit.ok:
            _log.warning("gmail: relay emit failed for outbox %s (rule %s)", key, rid)
            return False
        _crash(crash, "after_relay_emit")
        # B3 readback: confirm the canonical directive is visible before marking
        # done — never claim a relay we can't verify.
        if not relay_emitter.exists(directive):
            _log.warning("gmail: relay readback failed for outbox %s (rule %s)", key, rid)
            return False
        ledger.append(LedgerEntry.relay_done(
            account_id=account_id, message_id=message_id, rule_id=rid,
            rule_version=rver, outbox_key=key,
        ))
        _crash(crash, "after_relay_done")

    return not ledger.remaining_actions(message_id, rid, rver, required)


@dataclass(frozen=True)
class PollResult:
    """Outcome of one ``(account, rule)`` poll."""

    account_id: str
    rule_id: str
    rule_version: int
    candidates: int
    effective: int
    processed: int
    blocked: bool
    cursor: int | None


def poll_account_rule(
    *,
    client,
    rule: Rule,
    account_id: str,
    ledger: Ledger,
    cursors,
    files_writer: FilesWriter,
    relay_emitter: RelayEmitterProtocol | None,
    now_epoch: int | None = None,
    crash: Callable[[str], None] | None = None,
) -> PollResult:
    """Run one contiguous-frontier poll for a single ``(account, rule)``.

    ``client`` is a :class:`fulcra_gmail.client.GmailClient` (or a fake): an
    auth-failed account yields ``[]`` from ``list_message_ids`` / ``None`` from
    ``get_message`` and this poll no-ops cleanly (fail-soft).
    """
    # ``now_epoch`` is accepted for test determinism / future window bounding;
    # the strict contiguous-frontier rule advances the cursor only through
    # observed done candidates, so wall-clock time is not consulted here.
    rid, rver = rule.id, rule.version
    cursor = cursors.get(rid, rver)

    from .rules import build_query
    q = build_query(rule, cursor_epoch=cursor)
    ids = client.list_message_ids(q)

    # Refine candidates → effective matches (rejected ones vanish here).
    effective: list[dict] = []
    for message_id in ids:
        message = client.get_message(message_id)
        if message is None:  # auth-failed mid-run — fail-soft
            continue
        if evaluate(rule, message, account_id=account_id).matched:
            effective.append(message)

    # Deterministic oldest-first order (never trust API page order).
    effective.sort(key=lambda m: (internal_seconds(m), str(m.get("id"))))

    new_cursor = cursor
    blocked = False
    processed = 0
    for message in effective:
        done = process_message(
            message, rule=rule, account_id=account_id, ledger=ledger,
            files_writer=files_writer, relay_emitter=relay_emitter, crash=crash,
        )
        if done:
            processed += 1
            if not blocked:
                # Advance the frontier through this contiguously-done match.
                new_cursor = max(new_cursor or 0, internal_seconds(message))
        else:
            # First incomplete match — the frontier stops here. Later matches
            # still get processed (over-capture), but never advance the cursor
            # past this hole.
            blocked = True

    if new_cursor is not None and new_cursor != cursor:
        cursors.set(rid, rver, new_cursor)

    _log.debug(
        "gmail poll account=%s rule=%s candidates=%d effective=%d processed=%d blocked=%s",
        account_id, rid, len(ids), len(effective), processed, blocked,
    )
    return PollResult(
        account_id=account_id, rule_id=rid, rule_version=rver,
        candidates=len(ids), effective=len(effective), processed=processed,
        blocked=blocked, cursor=cursors.get(rid, rver),
    )
