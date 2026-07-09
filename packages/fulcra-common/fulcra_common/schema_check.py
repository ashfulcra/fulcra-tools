"""Wire-shape vs served-schema drift detection for the typed-ingest endpoint.

The typed endpoint (``POST /ingest/v1/record/{base_type}``) SILENTLY strips
any key not in the served schema and SILENTLY defaults missing ones
(live-verified 2026-07-08) — so a drift between what ``wire.build_typed_record``
emits and what the catalog serves never errors at the API, it just quietly
loses data. This module fetches the served schema and checks a payload dict
against it with a stdlib-only shallow check (no jsonschema dependency):

  - every ``required`` key is present,
  - no unknown keys ride along (they would be silently stripped),
  - the primitive fields we know carry the right JSON type (``value`` a
    number, ``note`` a string, ``tags``/``sources`` arrays).

It is intentionally shallow: it catches the drift that actually bites (a
renamed/added/removed top-level field), not full JSON-Schema validation.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fulcra_common.client import BaseFulcraClient

#: Top-level fields whose JSON type we shallow-check, and a (predicate, noun)
#: pair describing what a VALID value looks like. A bool is deliberately not a
#: number: JSON distinguishes ``true`` from ``1`` and the metric endpoint
#: stores a numeric ``value``.
_TYPE_CHECKS: dict[str, tuple] = {
    "value": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
              "a number"),
    "note": (lambda v: isinstance(v, str), "a string"),
    "tags": (lambda v: isinstance(v, list), "an array"),
    "sources": (lambda v: isinstance(v, list), "an array"),
}


def fetch_record_schema(
    client: "BaseFulcraClient", base_type: str, *, timeout: float = 10.0,
) -> dict:
    """Fetch the served record schema for ``base_type``.

    GETs ``/data/v1/catalog/{base_type}/v1alpha1/schema`` through the shared
    client (same auth/base-url as every other Fulcra call) and raises
    ``httpx.HTTPStatusError`` on any non-2xx — so a caller distinguishes
    "schema drifted" from "could not fetch the schema" (e.g. an older API
    that does not serve the catalog yet).
    """
    r = client._client().get(
        f"/data/v1/catalog/{base_type}/v1alpha1/schema",
        headers=client._authed_headers(),
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def check_payload_against_schema(payload: dict, schema: dict) -> list[str]:
    """Return human-readable drift problems between ``payload`` and ``schema``.

    Empty list means the payload matches the served schema. Each problem is a
    one-line string safe to print in a diagnostic (no values echoed). Checks:
    missing required keys, unknown keys (which the typed endpoint silently
    strips), and wrong JSON type for the primitive fields we know.
    """
    problems: list[str] = []
    known = set(schema.get("properties") or {})
    for key in schema.get("required") or []:
        if key not in payload:
            problems.append(f"missing required key {key!r}")
    for key in payload:
        if key not in known:
            problems.append(
                f"unknown key {key!r} will be silently stripped by the "
                f"typed endpoint")
            continue
        check = _TYPE_CHECKS.get(key)
        if check is not None and not check[0](payload[key]):
            problems.append(
                f"key {key!r} must be {check[1]}, got "
                f"{type(payload[key]).__name__}")
    return problems
