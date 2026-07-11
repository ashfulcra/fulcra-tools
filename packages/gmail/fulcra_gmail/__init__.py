"""fulcra-gmail — the local Gmail relay for Fulcra.

A read-only, multi-account Gmail poller that runs entirely on the operator's
machine: it authorizes N Gmail accounts (Google OAuth, ``gmail.readonly``
only), polls each account with local filter rules, and relays selected
emails into Fulcra. Accounts are a first-class dimension keyed by a stable,
opaque ``account_id`` (uuid4); the email address is metadata, never a path or
key segment. Nothing leaves the machine except the artifacts the operator's
rules choose.

Task 1 (this package's initial slice) ships the foundation only:

* :mod:`fulcra_gmail.client` — a per-account httpx wrapper around Gmail REST
  v1 (fully-paginated ``list_message_ids``, ``get_message``, ``get_profile``)
  with refresh-on-401 and fail-soft ``invalid_grant`` handling.
* :mod:`fulcra_gmail.accounts` — the account registry: opaque ``account_id``
  ↔ email, keychain-held secrets, and the B4 OAuth ``state``-nonce lifecycle
  with ``users.getProfile`` account binding.

Task 2 adds the pure-local processing layer (no daemon, no network):

* :mod:`fulcra_gmail.rules` — parse rules, build the server ``q`` (24h
  overlap / 7d first-run / backfill), and the local post-filter
  effective-match decision with privacy-safe reason codes (B2).
* :mod:`fulcra_gmail.convert` — Gmail ``messages.get(full)`` payload →
  deterministic selected-email JSON (header subset, text/html bodies,
  attachments as metadata only).
* :mod:`fulcra_gmail.ledger` — append-only per-account privacy ledger
  (metadata + hashes only), processed-set keyed by
  ``(message_id, rule_id, rule_version)``, torn-line tolerance, and the
  deterministic relay outbox key.

The Fulcra Files writer, bus relay emitter, and the collect plugin land in
Task 3.

Credit: the original design of the Gmail relay is ArcBot's (openclaw). Its
June MVP was unrecoverable; this is a clean-room rebuild on current main that
preserves ArcBot's architecture (daemon-owned polling + secrets, local rules,
privacy ledger, bus relay).
"""
from __future__ import annotations

__all__ = ["accounts", "client", "convert", "ledger", "rules"]
