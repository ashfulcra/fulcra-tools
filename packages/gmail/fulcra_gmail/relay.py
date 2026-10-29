"""B3 — the bus relay emitter (exactly-once-visible directive).

When a rule's actions include ``relay``, an effective match emits a directive on
the operator's own coord bus so an agent picks it up (the receipt-capture flow).
The hard requirement is **exactly-once-visible**: retries after a crash must
converge on a single visible directive, never a fan-out of duplicates.

**How the guarantee is met.** The ledger mints a deterministic ``outbox_key``
for ``(account_id, message_id, rule_id, rule_version)``. :func:`build_directive`
derives the directive's identity fields — ``title``/``summary``/``next_action``/
``assignee`` — as **byte-stable functions of that key** (assignee + priority come
from the rule). coord-engine's ``tell`` hashes exactly those identity fields into
its canonical directive slug (``<title-slug>-<sha256(payload)[:8]>``, see
``coord_engine.cli._create_directive``), so two emits of the same relay compute
the same slug and dedupe to one doc by construction. Verified against the
installed coord-engine (``tell`` yields a deterministic slug from a stable title
and ``search`` reads it back) — the B3 version pin is satisfied, so this leg does
NOT escalate.

**Sequence (driven by the pipeline, ledger barriers around it):**
``ledger relay-pending`` → :meth:`emit` → :meth:`exists` readback → ``ledger
relay-done``. On restart a still-pending (or absent-done) relay re-emits the
byte-identical directive and reconciles — idempotent because the slug is
identical.

**Privacy.** A directive carries only opaque tokens: the ``outbox_key`` and the
``rule_id``. Never a subject, address, snippet, or body.

The coord invocation is injected as a ``run(argv) -> (rc, stdout)`` callable so
the whole leg is unit-testable without a real bus. Collect injects the bundled
engine with its captured authenticated transport and exact-file readback.
There is no implicit subprocess or default credential fallback.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .rules import Rule

_log = logging.getLogger("fulcra_gmail.relay")

#: Directive sender identity — opaque, non-PII.
RELAY_SENDER = "gmail-relay"
#: Fallback priority when a relay rule omits ``relay_priority``.
DEFAULT_RELAY_PRIORITY = "P2"


@dataclass(frozen=True)
class RelayDirective:
    """The byte-stable directive derived from a relay ``outbox_key``.

    ``title``/``summary``/``next_action``/``assignee`` are coord-engine's
    identity payload; ``priority`` is delivery metadata (outside the dedup
    identity, so a priority change alone does not re-deliver).
    """

    outbox_key: str
    title: str
    summary: str
    next_action: str
    assignee: str
    priority: str


def build_directive(outbox_key: str, rule: Rule) -> RelayDirective:
    """Build the deterministic directive for one relay.

    Every text field is a pure function of ``outbox_key`` + the rule's opaque
    identity — no message content ever enters it. ``assignee`` = ``rule.relay_to``
    (which recipient); ``priority`` = ``rule.relay_priority`` (or the default).
    """
    ident = f"{rule.id}@{rule.version}"
    return RelayDirective(
        outbox_key=outbox_key,
        title=f"Gmail selected email relay {outbox_key}",
        summary=(
            f"A selected email matched rule {ident}. "
            f"Fetch it from Fulcra Files (outbox {outbox_key})."
        ),
        next_action=f"Process relayed email for rule {ident} (outbox {outbox_key})",
        assignee=rule.relay_to or "",
        priority=rule.relay_priority or DEFAULT_RELAY_PRIORITY,
    )


@dataclass(frozen=True)
class RelayResult:
    ok: bool
    slug: str | None = None
    reason: str | None = None


class RelayEmitterProtocol(Protocol):
    """The relay surface the pipeline depends on. Production is
    :class:`CoordEngineRelayEmitter`; tests inject a fake."""

    def emit(self, directive: "RelayDirective") -> "RelayResult": ...
    def exists(self, directive: "RelayDirective") -> bool: ...


class CoordEngineRelayEmitter:
    """Emits + reads back relay directives through coord-engine.

    ``team`` is the coord space the directive lands in. ``run`` is the injected
    invocation seam. Collect explicitly injects its authenticated engine adapter.
    """

    def __init__(
        self,
        team: str,
        *,
        run: Callable[[list[str]], tuple[int, str]],
        sender: str = RELAY_SENDER,
        readback: Callable[[str, RelayDirective], bool] | None = None,
    ) -> None:
        self._team = team
        self._run = run
        self._sender = sender
        self._readback = readback
        self._emitted_slugs: dict[RelayDirective, str] = {}

    def emit(self, directive: RelayDirective) -> RelayResult:
        """Deliver the directive via ``coord-engine tell``.

        Returns ``ok=True`` when coord-engine reports delivery OR an
        already-delivered dedupe (both rc 0). A re-emit of the same directive
        is idempotent — the engine converges it onto the one canonical slug.
        """
        if not directive.assignee:
            _log.warning(
                "gmail relay: directive %s has no assignee (rule relay_to unset)"
                " — skipping emit", directive.outbox_key,
            )
            return RelayResult(ok=False, reason="no_assignee")
        argv = [
            "tell", self._team, directive.assignee, directive.title,
            "-s", directive.summary,
            "-n", directive.next_action,
            "-p", directive.priority,
            "--from", self._sender,
        ]
        rc, out = self._run(argv)
        if rc != 0:
            _log.warning("gmail relay: tell failed rc=%s for outbox %s",
                         rc, directive.outbox_key)
            return RelayResult(ok=False, reason=f"tell_rc_{rc}")
        slug = _parse_slug(out)
        if not slug:
            return RelayResult(ok=False, reason="unverified_slug")
        self._emitted_slugs[directive] = slug
        _log.debug("gmail relay: emitted outbox=%s slug=%s", directive.outbox_key, slug)
        return RelayResult(ok=True, slug=slug)

    def exists(self, directive: RelayDirective) -> bool:
        """Readback-verify the canonical directive exists (B3).

        Collect reads the exact emitted slug through its bound transport.
        An explicitly injected compatibility runner can use a healthy search
        response, but only a row matching that emitted slug/title/recipient
        counts. Empty envelopes and degraded markers never confirm delivery.
        """
        slug = self._emitted_slugs.get(directive)
        if self._readback is not None:
            return bool(slug and self._readback(slug, directive))
        rc, out = self._run(["search", self._team, directive.outbox_key, "--json"])
        if rc != 0:
            return False
        try:
            rows = json.loads(out or "[]")
        except (ValueError, TypeError):
            return False
        if isinstance(rows, dict):
            if rows.get("contract") != 2 or rows.get("health") != "DATA" or rows.get("degraded") != []:
                return False
            rows = rows.get("rows")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or str(row.get("type", "")).endswith("-degraded")
            for row in rows
        ):
            return False
        slug = self._emitted_slugs.get(directive)
        if not slug:
            return False
        return any(
            row.get("name", row.get("slug")) == slug
            and row.get("title") == directive.title
            and row.get("assignee") == directive.assignee
            for row in rows
        )


def _parse_slug(stdout: str) -> str | None:
    """Pull the slug out of ``tell`` output.

    Handles both ``directive <slug> -> <assignee>`` (fresh delivery) and
    ``directive <slug> already delivered`` (idempotent dedupe).
    """
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("directive "):
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return None
