"""Fulcra API client for fulcra-attention.

A thin layer over `fulcra_common.BaseFulcraClient`: it adds the Attention
DurationAnnotation definition handling, the three-axis tag vocabulary
(machine / category / identity), and the attention-specific ingest_batch.
Auth, the httpx client, tag lookup, and soft-delete come from the base.
"""
from __future__ import annotations

from fulcra_common import BaseFulcraClient
from fulcra_common import wire

from .state import State


def sanitize_tag_value(value: str) -> str:
    """Reduce a free-text axis value to chars Fulcra's tag API accepts.

    Empirically Fulcra accepts `[a-z0-9._:-]` in tag names and rejects
    `@` and most other punctuation. Lowercase, collapse any disallowed
    run to a single `-`, trim leading/trailing `-`. The colon between
    axis and value is added by the caller, not here. Empty input
    returns an empty string (caller must guard).
    """
    import re
    out = re.sub(r"[^a-z0-9._\-]+", "-", value.strip().lower())
    out = re.sub(r"-{2,}", "-", out)  # collapse runs of `-`
    return out.strip("-")


TAG_NAME_MAX = 30  # Fulcra's tag.name validation cap (HTTP 422 above this).


def build_tag_name(axis: str, value: str) -> str:
    """Compose `<axis>:<sanitized-value>` so the result fits TAG_NAME_MAX.

    If the sanitized value alone would push the name past the limit, we
    truncate and append a deterministic 6-char sha256 suffix so distinct
    long values don't collide. axis must already be a safe slug
    (caller's responsibility — these are hard-coded in this package).
    """
    safe_value = sanitize_tag_value(value)
    if not safe_value:
        raise ValueError(f"axis={axis!r} value sanitises to empty: {value!r}")
    prefix = f"{axis}:"
    budget = TAG_NAME_MAX - len(prefix)
    if budget <= 0:
        raise ValueError(f"axis prefix {prefix!r} already exceeds {TAG_NAME_MAX}")
    if len(safe_value) <= budget:
        return f"{prefix}{safe_value}"
    # Truncate + 6-char hash suffix derived from the full sanitized value
    # (not the raw input — so case/whitespace differences collapse the
    # same way for lookup and creation).
    import hashlib
    suffix = hashlib.sha256(safe_value.encode()).hexdigest()[:6]
    head_budget = budget - 1 - len(suffix)  # 1 for the `-` separator
    if head_budget < 1:
        # Pathological: axis name so long there's no room for any value
        # bytes. Fall back to just the hash so the tag is still unique.
        return f"{prefix}{suffix[:budget]}"
    head = safe_value[:head_budget].rstrip("-")
    return f"{prefix}{head}-{suffix}"


# Tier 2 vocabulary, mirrored in chrome/src/categorize.ts. Pre-created at
# bootstrap so users can build filters/timelines against a known set even
# before they've categorized any domains. Add new slugs to both sides.
CATEGORY_VOCAB: tuple[str, ...] = (
    "search",
    "webmail",
    "ai-chat",
    "dm",
    "doc-editor",
    "reddit-thread",
    "calendar",
    "banking",
    "brokerage",
    "crypto",
    "tax",
    "healthcare",
    "password-manager",
    "mental-health",
    "dating",
    "adult",
    "job-hunting",
)


class FulcraClient(BaseFulcraClient):
    USER_AGENT = "fulcra-attention/0.1"

    def ensure_tag(self, name: str, state: State) -> str:
        """Look up / create a tag, caching the id in `state.tag_ids`."""
        if name in state.tag_ids:
            return state.tag_ids[name]
        tag_id = self._resolve_tag(name)
        state.tag_ids[name] = tag_id
        return tag_id

    def ensure_definitions(self, state: State) -> None:
        # ensure_tag is cache-first so safe to call on every bootstrap.
        # Always re-ensures vocab tags even if the def already exists, so a
        # bootstrap on an old account back-fills the new tag schema.
        attention = self.ensure_tag("attention", state)
        web = self.ensure_tag("web", state)
        # Pre-create category tags (Tier 2 vocabulary).
        for slug in CATEGORY_VOCAB:
            # Vocab slugs are hand-picked to be short + ascii so they
            # never need hashing — but route through build_tag_name for
            # consistency and to enforce the length cap.
            self.ensure_tag(build_tag_name("category", slug), state)
        if state.attention_definition_id:
            return
        # A second machine's bootstrap must ADOPT the account's existing
        # "Attention" definition, not POST a parallel one. This used to be
        # create-only, so every new machine spawned a duplicate definition
        # (a duplicate "Attention" row in Fulcra). Look it up by name first.
        existing = self._find_attention_definition()
        if existing is not None:
            state.attention_definition_id = existing
            return
        body = wire.duration_definition_payload(
            name="Attention",
            description="What the user paid attention to (browsing).",
            tags=[attention, web],
        )
        r = self._client().post(
            "/user/v1alpha1/annotation",
            json=body,
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        state.attention_definition_id = r.json()["id"]

    def list_attention_definitions(self) -> list[dict]:
        """Every annotation definition named "Attention" on the account,
        live and soft-deleted. Powers the `defs` CLI and duplicate cleanup."""
        r = self._client().get(
            "/user/v1alpha1/annotation",
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        return [d for d in r.json() if d.get("name") == "Attention"]

    def _find_attention_definition(self) -> str | None:
        """Return the id of the live "Attention" duration definition.

        None if none exists. If duplicates exist — an older create-only
        bootstrap made parallel ones — returns the oldest by created_at,
        so every machine deterministically converges on the same one.
        Soft-deleted definitions (non-null deleted_at) are ignored.
        """
        matches = [
            d for d in self.list_attention_definitions()
            if d.get("annotation_type") == "duration"
            and not d.get("deleted_at")
        ]
        if not matches:
            return None
        matches.sort(key=lambda d: d.get("created_at") or "")
        return matches[0]["id"]

    def ensure_machine_tag(self, hostname: str, state: State) -> str:
        """Create / look up the `machine:<hostname>` tag. Called by `setup`."""
        return self.ensure_tag(build_tag_name("machine", hostname), state)

    def ingest_batch(self, events: list[dict]) -> None:
        """POST a JSONL batch of already-built events to /ingest/v1/record/batch.

        Each event must be a dict with `specversion`, `data`, `metadata` keys
        (the wire format documented in the spec). Source-id idempotency is the
        caller's responsibility — building the deterministic source-id lives
        in ingest.py.
        """
        if not events:
            return
        body = wire.encode_batch(events)
        r = self._client().post(
            "/ingest/v1/record/batch",
            content=body,
            headers={
                **self._authed_headers(),
                "content-type": "application/x-jsonl",
            },
        )
        r.raise_for_status()
