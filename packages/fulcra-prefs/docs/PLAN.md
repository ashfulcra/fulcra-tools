# fulcra-prefs v1 implementation plan — completed (historical)

> **Historical / executed.** This was the task-by-task TDD plan used to build
> v1. It has been fully implemented and shipped. Its embedded code blocks were a
> *sketch* and have since drifted from what was actually built (e.g. the store's
> `data_type` split on `/`, absolute-path normalization, tz-safe decay, parallel
> shard reads, and broad `except Exception` replaced by narrow transport
> catches). **Do not read the old code blocks as current.**
>
> For the durable picture, in order of authority:
> - **The code + tests** — `fulcra_prefs/` and `tests/` are the source of truth.
> - **[`SPEC.md`](SPEC.md)** — the reviewed design and the determinism/consent contracts.
> - **[`../README.md`](../README.md)** — usage and the honest v1 limitations.
> - **[`DESIGN.md`](DESIGN.md)** — the earlier v0 sketch (also historical).
>
> Kept as a short stub rather than ~1,900 lines of stale code; the full original
> plan remains in git history if the execution record is ever needed.

**What it delivered:** the event-sourced two-layer design — typed signals
(`schema`/`decay`) ingested as annotation records (`store`/`capture`/`outbox`),
deterministically compiled into per-platform preference docs (`compileprefs`),
a deterministic group-decision solver (`solver`), consent-gated export with a
disclosure ledger (`consent`), the `fulcra-prefs` CLI, and the agent skill with
tier-routed HTTP recipes — all under `packages/fulcra-prefs/`, TDD throughout.
