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

    A ``relay`` action always carries a ``relay_to`` (enforced at
    :func:`fulcra_gmail.rules.parse_rules`), so nothing is dropped here — the
    required set is exactly the rule's actions. A relay a rule requires is NEVER
    silently completed: if the backend is unavailable the message stays
    incomplete (see :func:`process_message`)."""
    return [a for a in _CANONICAL_ACTIONS if a in rule.actions]


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
        if relay_emitter is None:
            # The rule requires a relay but no backend is available (no coord
            # team configured). The message is INCOMPLETE — never marked done —
            # so the cursor does not advance past it. Once a relay backend is
            # configured, the next poll relays it and the frontier advances.
            # (File-only behavior requires a rule whose actions are only
            # ["file"]; a ["file","relay"] rule always needs a working relay.)
            _log.warning(
                "gmail: rule %s requires relay but no relay backend is "
                "configured — message held incomplete (configure relay_team)",
                rid,
            )
            return False
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
    #: Candidate ids that could not be fetched this run (frontier holes).
    unresolved: int = 0


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
    # A candidate id we could NOT resolve (get_message returned None or raised —
    # a transient per-message failure or a mid-run auth failure) is a FRONTIER
    # HOLE: we can't evaluate it, so we can't know whether it should have been
    # exported. We have no internalDate to place it in the sorted order, so any
    # unresolved candidate blocks the whole cursor advance for this run — the
    # watermark must never move past a candidate we never looked at. The 24h
    # overlap keeps the id inside the next run's window so it is re-attempted.
    effective: list[dict] = []
    unresolved = 0
    for message_id in ids:
        try:
            message = client.get_message(message_id)
        except Exception:  # noqa: BLE001 — any fetch error is a hole, not fatal
            message = None
        if message is None:
            unresolved += 1
            continue
        if evaluate(rule, message, account_id=account_id).matched:
            effective.append(message)

    # Deterministic oldest-first order (never trust API page order).
    effective.sort(key=lambda m: (internal_seconds(m), str(m.get("id"))))

    new_cursor = cursor
    # An unresolved candidate is a hole: start BLOCKED so no done match advances
    # the cursor past a candidate we couldn't fetch.
    blocked = unresolved > 0
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
        "gmail poll account=%s rule=%s candidates=%d effective=%d processed=%d "
        "unresolved=%d blocked=%s",
        account_id, rid, len(ids), len(effective), processed, unresolved, blocked,
    )
    return PollResult(
        account_id=account_id, rule_id=rid, rule_version=rver,
        candidates=len(ids), effective=len(effective), processed=processed,
        blocked=blocked, cursor=cursors.get(rid, rver), unresolved=unresolved,
    )
