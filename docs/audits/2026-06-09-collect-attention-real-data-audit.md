# Collect + Attention real-data audit (2026-06-09/10)

**Method:** systematic-debugging against the live account's actual data — 60
days of `DurationAnnotation` records (157,594), the daemon's `plugin_state`,
the real Apple takeout zip, and the live Last.fm API — evidence first, then
root-cause, then fix. Every fix below was reproduced on real data before
implementation and verified after.

## Findings → fixes (PRs)

| # | Finding (evidence) | Root cause | Fix |
|---|---|---|---|
| 1 | Apple Music takeout imported **0 of 11,331 plays** while reporting `done` | `until = "6/1/26"` is unparseable; the plugin soft-logged and `return`ed — silent zero-import | Config corrected (`2026-06-01`); import landed (9,300 plays). Code fix (hard-fail on bad window) spawned as a follow-up task |
| 2 | 2 of 17 Sunday Last.fm scrobbles never landed: a 15:03:36 quick replay of a 15:00:17 play | `listened_fingerprint` 5-min bucket → identical fingerprint → `run_import`'s key-intersection skip conflated same-source replays with cross-source twins | **PR #134** — replay detection: fp claimed only by own-importer records → post with fp stripped; conservative skip otherwise |
| 3 | The other lost scrobble (15:38 Funkytown) was uploaded to Last.fm late and the watermark had moved past it | 1-hour rewind in `since_from_watermark` can't reach late offline-sync uploads | **PR #131** — rewind 1h → 24h (idempotent via det-id readback, ≤ ~1 extra API page) |
| 4 | **~0% of Apple Music plays have an artist** (3 of 20,000 rows fill `Container Artist Name`) — notes render " – Song", fingerprints get empty artist | Apple leaves the column empty; the importer had no other source | **PR #132** — title→artist enrichment from sibling takeout files; **79% (8,975/11,331) filled** on the real takeout, verified; det-id stability guaranteed (no re-import storm) |
| 5 | trakt at **6 consecutive 401s**; refresh token sitting unused in the keychain | The web-UI/keychain auth path has no refresh logic (the legacy file path does) | **PR #133** — reactive 401→refresh→persist-rotated-tokens-before-retry; new `RunContext.set_credential` seam |
| 6 | apple-takeout (video) also silently zero-importing | Same `until` typo as #1 | Config corrected alongside #1 |

## Attention: the storm residue (decision needed)

**Current pipeline is healthy.** The relayless v3 path produced 1 duplicate
out of 5,125 attention source_ids in 60 days.

**The account carries massive dead weight from the pre-relayless era:**
- **144,664 of 149,789 attention records (97%) are exact duplicate clones** —
  identical timestamps, same source_id ingested repeatedly (worst: one v2
  source_id × **2,729** on 2026-06-01).
- Entirely bounded to **2026-04-10 → 2026-06-01** (no attention records exist
  before April; the storm stopped at the relayless cutover). Excess by wire
  version: v1 = 68,445, v2 = 76,218, v3 = 1.
- Cost: every DurationAnnotation query drags ~30× dead records (the 60-day
  window is 205 MB raw); any attention analytics are meaningless without
  client-side dedup.

**Constraint:** Fulcra has **no per-event delete** (verified 2026-05-26 probe:
405/404 on every method against `/data/v1alpha1/event/...`; reconfirmed in
`fulcra_common.client.soft_delete_definition`'s docs — definition soft-delete
is the only primitive).

**Options:**
1. **Definition rotation (recommended, needs sign-off):** create a fresh
   Attention definition; re-ingest the 5,125 distinct visits bound to it;
   soft-delete the old definition so the UI's metric list drops the polluted
   history. Caveats: attention writers must re-resolve the definition id
   (extension caches it — needs a cache clear / re-onboard); API queries can
   still see orphaned records, so consumers should scope to live defs (the
   media pipeline already does exactly this in `run_import`).
2. **Server-side bulk delete:** ask the backend team (staff account) to
   delete the 144,664 excess record ids — a manifest can be generated from
   the dedup analysis on demand.
3. **Live with it** and dedup at query time everywhere (status quo; costs
   every consumer forever).

## Known-good baselines (for future audits)

- Music: Last.fm is the only live music path; Apple Music and Spotify both
  scrobble through it **unlabeled and lossy** (Apple Music→last.fm was fully
  broken until at least mid-May — entire sessions absent). The takeout (now
  fixed) is the authoritative Apple Music record; periodic re-pulls backfill
  scrobbler losses.
- Attention: v3 relayless clean; v1/v2 are legacy wire versions, no longer
  written.

## Minor / follow-ups

- Daemon logs unrotated (`daemon.err.log` 10.6 MB) — launchd redirects, so
  rotation needs newsyslog.d or a daemon-side rotating handler.
- `apple-podcasts-timemachine` error row in plugin_state is stale residue
  (plugin not enabled); harmless.
- Silent zero-import hard-fail (finding 1's code half) tracked separately.
