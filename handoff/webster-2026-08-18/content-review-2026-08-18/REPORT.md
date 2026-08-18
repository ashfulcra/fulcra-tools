# Content-review skill — first test run (2026-08-18)

Skill: `skills/fulcra-content-integration/SKILL.md`
Targets: 2 most recent Cookbook recipes + 2 most recent Blog items.
Originals untouched (no update calls were made against them). Each copy was
created as a **new draft item** (`isDraft: true`, never published), named
"… — review draft" with slug suffix `-review-draft`.

| Original (id) | Draft copy (id) |
|---|---|
| Dinner ordering recipe `6a5901281e510df832d4a057` | `6a846de8dd26f180acf5a1c5` |
| Fitness & movement recipe `6a46d1b4b04bb2c373b07008` | `6a846e2b6cee41517465f5ed` |
| Blog: What Should Your Agent Check on Every Loop? `6a7638325cc166d634cde749` | `6a846e690aca93d90c9ff8ef` |
| Blog: Why Run Agents in Loops? (was itself a draft) `6a8374b2b055847b93d9cf4e` | `6a846e690aca93d90c9ff8f1` |

Full field-level before/after: `before/*.json` vs `after/*.json` in this directory.

## Sources actually used for verification

- **Live Fulcra MCP (Webster account)** — exercised `get_data_updates`,
  `create_data_type`, `record_data`, `get_records` (incl. cross-user), and
  confirmed `get_sleep` / `get_workouts` / `get_calendar_events` schemas.
- **Fresh clone of `fulcradynamics/agent-skills`** (public, read-only).
- NOT available this session (FulcraBot/webflow-bot unattachable):
  `canonical/FULCRA-FOR-AGENTS-2026-08-18.md`, `v2-payloads/*` surface docs.
  Consequence: no claims were sourced from them; additions stay inside each
  piece's existing voice and only state capabilities verified live.

## Per-piece review

### Recipe: dinner ordering
Goal: hand the dinner decision to an agent, and test that it beats you.
Fulcra inventory: one bolted-on mention ("Platforms like Fulcra … exist").
Changes: (1) mention rewritten to be operational — agent reads sleep/
workouts/calendar over MCP and **records the nightly meal rating back as its
own data type**, closing the article's own feedback loop; (2) Tracking-method
bullet: both experiment logs live in the Fulcra store; (3) copy-paste prompt
had ~15 words run together from lost line breaks ("byevening",
"whateveris") — rewritten cleanly with the connected-data and rating-record
clauses added.

### Recipe: fitness & movement
Goal: teach the agent your real movement life (truth over plan).
Fulcra inventory: zero mentions; FAQ even suggested manually feeding tracker
summaries. Changes: (1) new paragraph — when a wearable saw the week, the
agent pulls the actual record via Fulcra and you supply only the felt layer;
(2) debriefs saved as timeline annotations so history survives sessions;
(3) smartwatch FAQ flipped from "feed summaries periodically" to direct
querying (subjective-experience caveat kept); (4) "What to do right now" and
prompt updated to start from the record when connected. Fulcra is now the
mechanism of the piece's own thesis: the record is the truth source.

### Blog: What Should Your Agent Check on Every Loop?
Changes: (1) delta-call description corrected to match the delivered
artefact — data types **with counts** plus changed files (original claimed
"each with a path and a timestamp"); named as data-updates; (2)
`fulcra-agent-teams` → **`fulcra-workspaces`** (upstream renamed it; old link
lands on a deprecation stub); (3) "ships the routine as a skill" now names
and links `fulcra-situational-awareness` (verified: it requires explicit
user consent); (4) two truncated list items completed; (5) h1/h2 → h3 per
design system (Blog rich text is H3).

### Blog: Why Run Agents in Loops?
Already the most integral piece. Changes: (1) `fulcra-agent-teams` →
`fulcra-workspaces` with corrected deep link; (2) "delta awareness" FAQ made
concrete (one query answers "what changed since my last pass"); (3) h1/h2 →
h3 per design system.

## Claims verification table

| Claim in drafts | Artefact | How verified |
|---|---|---|
| Agent queries sleep/workouts/calendar over MCP | live Fulcra MCP | tool schemas + calls this session |
| Agent records ratings/debriefs back as data types / annotations | live Fulcra MCP | `create_data_type` + `record_data` + `get_records` round-trip performed |
| data-updates returns data types w/ counts + file changes | live Fulcra MCP | `get_data_updates` call, response inspected |
| fulcra-workspaces gives each agent a team inbox | agent-skills clone | `skills/fulcra-workspaces/SKILL.md` (inbox namespace documented) |
| situational-awareness skill = the wake-up routine, asks consent first | agent-skills clone | `skills/fulcra-situational-awareness/SKILL.md` |
| Pre-existing external stats (Oracle 4x/15x, Weizmann, STRRIDE, 22:00-dinner trial) | — | UNCHANGED, not re-verified this pass |
| "the CLI" as an access path (pre-existing) | — | UNVERIFIED-HERE: PyPI blocked in this container; wheel hash present in fulcra-tools uv.lock only |

## Standing rules — status

- Staging only, nothing published: all four items `isDraft: true`,
  `lastPublished: null`. Publishing remains a manual user action.
- CHANGELOG-SITE.md + gates (`ops/changelog-gate.js`, `ops/publish-gate.js`)
  and `qa-rig/` live in FulcraBot/webflow-bot, which this session could not
  attach. Ready-to-paste changelog entries:
  - `2026-08-18 cms/cookbook: added draft "…What to Order You for Dinner — review draft" (6a846de8dd26f180acf5a1c5), Fulcra-integral rework of 6a5901281e510df832d4a057; original untouched.`
  - `2026-08-18 cms/cookbook: added draft "…Fitness and Movement — review draft" (6a846e2b6cee41517465f5ed), Fulcra-integral rework of 6a46d1b4b04bb2c373b07008; original untouched.`
  - `2026-08-18 cms/blog: added draft "What Should Your Agent Check on Every Loop? — review draft" (6a846e690aca93d90c9ff8ef); fixes deprecated fulcra-agent-teams reference → fulcra-workspaces; original untouched.`
  - `2026-08-18 cms/blog: added draft "Why Run Agents in Loops? — review draft" (6a846e690aca93d90c9ff8f1); fixes deprecated fulcra-agent-teams reference → fulcra-workspaces; original untouched.`
- Visual/link QA: pending — drafts have no rendered staging URL until
  published, and qa-rig is in the unattachable repo.

## Follow-ups surfaced by this run

1. The two **published** blog posts still link the deprecated
   `fulcra-agent-teams` path — worth fixing in the originals once reviewed
   (that edit touches live content, so it waits for approval).
2. The published dinner recipe's copy-paste prompt has the run-together-word
   defect in production.
3. Blog rich text on the originals uses h1/h2 against the H3 convention.

## Pass 2 (2026-08-18, after Ash review)

Ash flagged the loops draft as near-identical — fair: pass 1 made only three
correctness fixes there because the piece was already the most
Fulcra-integral of the four (the skill's own don't-stuff rule). Pass 2 adds
real improvements to both blog drafts, applied to the CMS drafts:

- loops: broken third "ask" question repaired ("what is the loop issue" →
  "whether the loop can see"); the observability paragraph now states the
  one-question/one-call mechanic; the "easiest way to start" FAQ links
  fulcra-situational-awareness (verified artefact) as the packaged check;
  stray "&nbsp;" doubles removed. Still missing: a featured image (field is
  empty on the original stub too — needs an editorial pick).
- check-loop: stray "&nbsp;" doubles removed; "No, history" FAQ opener fixed.

## Pass 3 (2026-08-18) — the how-to pass, after skill v2

Ash's ruling: capability talk without reader-executable steps is a
name-drop. Skill upgraded to v2 (two tests: integral AND executable; built
from canonical/FULCRA-FOR-AGENTS-2026-08-18.md, the served docs, and the
live-surface audit), then re-applied to all four drafts:

- Both recipes: a "connecting takes minutes" block at the point of need —
  /connect/claude, /connect/chatgpt, MCP URL https://mcp.fulcradynamics.com/mcp
  (details /platform/mcp), CLI path (pip install fulcra-api / uvx fulcra-api,
  fulcra auth login) — plus the literal asks ("read last night's sleep",
  "read my workouts, steps, and sleep for the last two weeks", "record
  tonight's meal rating as its own data type"). Both copy-paste prompts now
  handle the unconnected reader: the agent walks the user through connecting
  first. Canonical vocabulary in place (context lake, owned by you).
- Check-loop blog: watermark discipline added (canonical Feed-Driven
  Reactive Loop language); final FAQ now gives all three connect paths and
  names `fulcra data-updates` as the delta call itself.
- Loops blog: connect paths inline in the easiest-way FAQ; watermark
  language in the cost FAQ.

### Claims-and-steps table (pass 3 additions)

| Step in drafts | Artefact | How verified |
|---|---|---|
| /connect/claude, /connect/chatgpt, /platform/mcp | rendered production pages | HTTP 200 + rendered text read, 2026-08-18 |
| MCP URL https://mcp.fulcradynamics.com/mcp | fulcra-connect skill (agent-skills clone) | 2026-08-18 |
| pip install fulcra-api / uvx fulcra-api | docs.fulcradynamics.com/agent-get-started.txt as served | 2026-08-18 |
| fulcra auth login device flow | same + flow exercised live this session | 2026-08-18 |
| fulcra data-updates as the delta call | served CLI help in agent-get-started.txt + live MCP get_data_updates | 2026-08-18 |
| watermark discipline | canonical doc (Feed-Driven Reactive Loop) | 2026-08-18 |

## Pass 4 (2026-08-18) — newcomer-first prompts

Ash's ruling: the pass-3 prompts were bad for someone who has never used
Fulcra — they front-loaded protocol names, URLs, and install commands at
the human. Skill updated ("know your how-to's audience"): page copy speaks
to the human; a copy-paste prompt speaks to the AGENT, and must deliver
value first with zero setup, offer the connected upgrade in plain words
after the first useful pass, and delegate the connection to the agent by
pointing it at docs.fulcradynamics.com/agent-get-started.txt (the served
agent-onboarding artefact) rather than making the human parse URLs.
Both recipe prompts rewritten to that shape and updated in the CMS.
