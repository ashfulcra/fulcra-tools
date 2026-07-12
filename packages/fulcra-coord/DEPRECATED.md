# DEPRECATED — except `annotations.py`

`fulcra-coord` (the original coordination bus) is superseded by the **coord** layer now canonical in
this repo: `packages/coord-engine` + the `skills/fulcra-agent-*` skills. Do not build new
coordination features here; the old bus is in sunset.

**Explicit carve-out:** `fulcra_coord/annotations.py` (daily Agent Tasks / Digest timeline
annotations, with the 0.15.17/0.15.18 pinned-canonical-definition machinery) REMAINS LIVE and
scheduled. It stays here, unchanged, until fulcra-api's CLI ships an annotation record-write verb,
at which point it gets a single clean port to `packages/fulcra-annotations` with a thin CLI
transport (tripwire: 2026-08-15). **`annotations.py` is the only supported surface in this
package** — everything else is retained for provenance only.
