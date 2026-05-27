# Onboarding: "How to gather and update your data" screen

**Status:** spec — design approved 2026-05-27, awaiting implementation plan
**Author of this round:** session 2026-05-27
**Touches:** `packages/web-ui/dist/index.html`, `packages/web-ui/dist/static/onboarding.js`, plus a unit test in `packages/web-ui/tests/` (or its established location).

## Problem

Onboarding currently sends users straight from sign-in into the pick-plugins list with no context on how Fulcra Collect actually captures data. Two ideas the user said new readers consistently miss:

1. Some plugins are **one-shot historical imports** (takeouts: Apple Music, Netflix, Spotify Extended, Apple Takeout, etc.). They give you years of past data in a single load but don't update afterwards.
2. Other plugins are **live capture** — they keep gathering new events as they happen (Last.fm, Trakt, Apple Podcasts on-device, Attention extension, Plex/Jellyfin webhooks, etc.).

Combining a historical source with its live counterpart in the same category gives users a continuous timeline back to whenever the takeout reaches and forward indefinitely. The cross-source dedup work that landed in refactor #55 made this safe — two sources writing the same listen / watch are merged into a single annotation via the shared fingerprint.

Today, none of this is visible in the UI. A user sees Last.fm and Apple Music Takeout side by side and has no way to know they're complementary rather than redundant.

## Scope

Add one new phase to the onboarding state machine, between `signin` and `pick_plugins`. The new phase is a single static screen — no per-plugin dynamic enumeration, no API calls.

Out of scope for this spec:

- Changing the pick_plugins list itself (no new tile badges, no category-level tips). Considered and rejected during brainstorming in favour of a dedicated screen.
- A "where the Attention extension is paired right now" affordance. The Attention callout on this screen is informational; pairing happens later in that plugin's setup wizard.
- An in-app docs page for the plugin-contract. The "build your own" closing paragraph references it but the actual doc landing is its own task.
- Persisting "user already saw this screen" so re-running onboarding can skip it. We always show it; the screen is short.

## Placement

```
welcome → signin → collect_modes → pick_plugins → configure → done
                   ^^^^^^^^^^^^^
                   new phase
```

Narrative beat: user just connected their account, now learns how Fulcra captures data, then picks which sources to wire up. The framing immediately informs the choice on the next screen.

## Screen content

**Title**

> How to gather and update your data with Fulcra Collect

**Lede** (1–2 short paragraphs, slate-700 body text):

> Some plugins import a one-time export — your **historical** data. Others capture new events as they happen — your **live** data. They're safe to mix: when sources overlap, Fulcra deduplicates them so you don't get double counts.
>
> Here are some examples of how historical and live sources fit together.

**Examples grid** — 2×2 on desktop (`md:` breakpoint and above), single column on narrow viewports. Each card has an icon, a category label, a "live" line, a "historical" line, and the result.

| icon | category | live | historical | combined |
|---|---|---|---|---|
| 🎵 | Music | **Last.fm** — scrobbles every play, going forward | **Apple Music takeout** — years of past listens from your account export | one unified **Listened** track |
| 🎬 | TV & Movies | **Trakt** — scrobbles new watches in real time | **Netflix takeout** — your full Netflix watch history | one unified **Watched** track |
| 🎙️ | Podcasts | **Apple Podcasts (on-device)** — polls the local Podcasts database every 6 hours | **Apple Podcasts (Time Machine recovery)** — pulls episodes from older Time Machine snapshots of the same database | one unified **Listened** track |
| 🍿 | Movies via Apple | **Trakt** — scrobbles new watches in real time | **Apple takeout** — your iTunes / Apple TV purchase and rental history | one unified **Watched** track |

Visual: each card uses the existing card pattern (`border border-slate-200 rounded-lg p-4 space-y-2`). Header row is icon + category name (`font-semibold text-sm`). Body rows use a small "Live:" / "Historical:" label in `text-slate-500 text-xs uppercase tracking-wider`, then the plugin description. Bottom row uses `text-emerald-700 text-xs font-medium` for the "→ one unified … track" summary.

**Attention callout** (between the grid and the closing paragraph; distinct treatment so it doesn't blend into the grid):

> 🌐 **A special case: Attention.** The Fulcra Attention browser extension captures live tab activity as you browse — and can backfill from your existing browser history when you install it. You can pair the same Attention track from multiple browsers across multiple machines (Arc, Chrome, work laptop, home), and every paired instance feeds the same unified track.

Visual: `rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700`. Emoji as a visual marker; no separate icon column.

**Closing encouragement** (last block before the buttons):

> **Don't see what you need?** Fulcra Collect is open and extensible — every plugin we ship implements a documented contract. You can write your own plugin to capture whatever data matters to your future self, whether that's something we haven't built yet or something only you have.

Visual: matches the lede's text style. Once an in-app plugin-contract docs page exists, the phrase "documented contract" links to it. Until then, plain text.

**Footer note** (small text below the closing paragraph):

> These are just examples — every plugin works on its own too, and most categories have additional sources we didn't show here.

Visual: `text-xs text-slate-500`.

**Navigation buttons**

- Back — returns to `signin`. Matches the existing onboarding back-button pattern (left side, slate-styled secondary button).
- Next — advances to `pick_plugins`. Matches the existing primary button pattern (right side, violet-styled).
- The existing "Skip onboarding" link in the header bar stays available throughout, unchanged.

## Implementation surface

### `packages/web-ui/dist/static/onboarding.js`

1. The phase state-machine constant set (wherever it lives — likely inline in `phase` initialization and possibly a constant list elsewhere) gains `"collect_modes"`. Order in any list: between `"signin"` and `"pick_plugins"`.
2. The transition out of `signin` switches to `phase = "collect_modes"` instead of `phase = "pick_plugins"`.
3. Two new methods on the returned object:
   - `goToPickPlugins()` — sets `this.phase = "pick_plugins"`.
   - `backToSignin()` — sets `this.phase = "signin"`. (Match whatever naming convention the existing back transitions use; if there's already a generic `goBack()` driven off the current phase, prefer that.)

### `packages/web-ui/dist/index.html`

A new `<template x-if="phase === 'collect_modes'">` block inserted between the existing `phase === 'signin'` block and the `phase === 'pick_plugins'` block, around line 213 in the current file. Static markup only — no Alpine reactivity beyond the existing button click handlers.

### Tests

Add a single onboarding-side test asserting the phase value is in the legal set and that transitions out of `signin` land on `collect_modes`. If the test file for onboarding.js doesn't exist yet, follow the location convention used for the other static-JS tests in the repo (a quick check on `packages/web-ui/tests/` or wherever `node --check`-based tests already live; if no JS test runner is wired up, this becomes a manual verification step).

### Visual verification (manual, not automatable in this session)

1. Walk the full flow: welcome → signin → collect_modes → pick_plugins → configure → done. Confirm each transition's back button lands you where you expect.
2. Desktop viewport (~1200 px wide): cards render as 2×2 grid. Lede + grid + Attention callout + closing paragraph all visible without horizontal scroll.
3. Narrow viewport (~480 px wide): cards collapse to single column. No overflow, no awkward wrapping.
4. Tab through with keyboard: focus order goes lede → cards → Attention callout → closing → Back → Next.

## Open questions and follow-ups

- "Documented contract" link target. There's no in-app docs page for the plugin-contract yet. Land this screen with plain text for now; once the docs page exists, that phrase becomes a link.
- A second pass might add a fifth combo (e.g., Goodreads + a future "physical books takeout") once the second source exists. Not relevant now.
- If a user has already enabled some plugins (re-running onboarding from the dashboard), the screen still appears. Considered making it conditional; rejected because the screen takes ~15 seconds to scan and the cost of re-showing it is lower than the cost of branching the state machine.

## Acceptance

- The new phase renders with the exact title, lede, four combo cards, Attention callout, closing paragraph, footer note, and Back/Next buttons described above.
- Welcome → signin → collect_modes → pick_plugins → configure → done navigates correctly forward and back.
- The pick_plugins phase still works exactly as it does today; this change does not touch the plugin list itself.
- Existing tests still pass (1443 baseline at session start).
