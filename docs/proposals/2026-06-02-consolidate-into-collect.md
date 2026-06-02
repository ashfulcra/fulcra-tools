# Consolidate-into-Collect: three open decisions

**Date:** 2026-06-02 · **Branch:** `docs/consolidate-into-collect-spec` · **For:** Ash (go/no-go)

This is a decision doc, not an implementation plan. Each section is
Problem → Options (with effort + risk) → **RECOMMENDATION**. Every claim
cites a real file/grep finding so you can spot-check.

---

## What's already done (baseline)

The product vision is settled: **Fulcra Collect is the single shippable
product; the earlier standalone projects (attention, media-helpers, dayone,
csv-importer) are its plugins.** Two PRs already landed on `main`:
**#8** (`2660eef`) fixed onboarding auth recovery + added daemon logging, and
**#9** (`676dd3a`) was the consolidation-tidy: it fixed the credentials-status
bug, the extension-pairing silent no-op, removed dead-relay copy, fixed the
Attention CLI wording, reframed `README.md` around Collect, and **single-sourced
the Attention definition match-spec** — `ATTENTION_SPEC` in
`packages/attention/fulcra_attention/collect_plugin.py:40` is now a projection
of `wire.duration_definition_payload(...)` via
`packages/attention/fulcra_attention/definition_spec.py`. Separately, the legacy
standalone `fulcra-attention` repo, its `:8771` relay, and its pipx CLI were
removed from the machine, and that GitHub repo was archived. **Net: the README
and docs say "Collect," but no code, package, repo, or path name was renamed —
that is exactly what Decision 1 is about.**

---

## Decision 1 — Repo / package rename to "Collect"

### Problem
#9 rebranded only prose. The actual identifiers still carry mixed naming. The
question is how far to push "Collect" into the names, given that some of those
names are baked into **installed users' on-disk and keychain state**.

### Impact analysis (grep evidence)

| Surface | Current value | Evidence | Already "Collect"? |
|---|---|---|---|
| GitHub repo | `ashfulcra/fulcra-tools` | `git remote -v`; `gh repo view` → `ashfulcra/fulcra-tools` (private) | No |
| Clone URLs in code/docs | `github.com/ashfulcra/fulcra-tools.git` | `README.md:31`, `docs/TESTING.md:35`, **baked into a wizard setup step** `attention/.../collect_plugin.py:201,207-209`, `chrome/README.md:11`, chrome-release workflow | No |
| Workspace root | "fulcra-tools monorepo" | `pyproject.toml:1` | No |
| Daemon package | `fulcra-collect` / module `fulcra_collect` | `packages/collect/pyproject.toml:name`; 99 files / 455 lines ref `fulcra_collect` | **Yes** |
| Console script (daemon) | `fulcra-collect` | `packages/collect/pyproject.toml:31` | **Yes** |
| Other package dirs | `attention`, `media-helpers`, `dayone`, `csv-importer`, `fulcra-common`, `menubar`, `web-ui` | `ls packages/` | dir names are neutral |
| Import modules | `fulcra_media` (75f/708L), `fulcra_attention` (18f/74L), `fulcra_dayone` (12f/24L), `fulcra_csv` (26f/53L), `fulcra_common` (34f/74L), `fulcra_menubar` (29f/47L) | grep counts | No (plugin modules) |
| Console scripts (plugins) | `fulcra-attention`, `fulcra-media`, `fulcra-dayone`, `fulcra-csv` | `[project.scripts]` in each pyproject | No |
| Plugin entry-point group | `fulcra_collect.plugins` | `attention/dayone/media-helpers pyproject` + egg-info | (named after collect; fine) |
| **launchd label** | `com.fulcra.collect` | `service_manager.py:12`, `menubar/.../daemon_lifecycle.py:49`, README/TESTING/AGENTS, tests | already "collect" |
| **Config dir** | `~/.config/fulcra-collect/` | `config.py:20` (`FULCRA_COLLECT_HOME` override), `state.py:5-6`, web/control/menubar | already "collect" |
| **Keychain service prefix** | `fulcra-collect:` (`fulcra-collect:user`) | `credentials.py:19,110`; AGENTS.md:26 | already "collect" |
| Web port / UI | `127.0.0.1:9292`, `web-url`/`web-token` files | `config.py:40`, AGENTS.md:20 | neutral |
| Docs/AGENTS | "fulcra-tools" repo name only | `AGENTS.md`, `fulcra-common` README/`__init__`/`client.py:42` (`USER_AGENT="fulcra-tools/0.1"`) | No |

**Key finding:** the three *user-data-affecting* surfaces — launchd label,
`~/.config` dir, and keychain service prefix — are **already** `fulcra-collect` /
`com.fulcra.collect`. So the highest-risk part of a rename is **not on the table
unless you go out of your way to change those strings** (which there is no reason
to do — `collect` is already on-brand). The remaining gap is the repo name and
the *plugin* module/script names.

### Options

| | Scope | Effort | Risk | User-data-affecting? |
|---|---|---|---|---|
| **A** | Docs/product rebrand only — leave all code/repo names | ~0 (done in #9) | None | No |
| **B** | Rename GitHub repo `fulcra-tools`→`fulcra-collect`; keep package/module names | Low (½ day) | Low–med: must update baked-in clone URLs + chrome-release; GitHub auto-redirects old URL | No |
| **C** | Full rename incl. plugin package dirs + modules (`fulcra_media`→…, `fulcra_attention`→…) and their scripts | High (multi-day; ~1000+ line touches, egg-info, entry points, `tool.uv.sources`, imports in tests) | High churn, broad blast radius | **Only if** you also touched label/config/keychain — see below |

**Notes on B:** the repo name leaks into runtime user-facing copy: the Attention
wizard's "Install the extension" step literally prints
`git clone https://github.com/ashfulcra/fulcra-tools.git` and
`cd ~/Developer/fulcra-tools/...` (`collect_plugin.py:201-209`), and the
chrome-release workflow names its asset `fulcra-attention-chrome.zip` on
`attention-v*` tags. GitHub 301-redirects the old repo URL after a rename, so
existing clones/CI keep working, but the in-app instructions would be *stale*
(still say `fulcra-tools`) until updated. So B = rename + sweep those literals.

**Notes on C:** renaming the daemon (`fulcra_collect`) is pointless — it's
already "collect." Renaming the *plugin* modules (`fulcra_media` etc.) buys
almost nothing for users (no one imports them by hand) at high cost.
Critically, the data-orphaning hazard people associate with "full rename" comes
from changing the launchd label / `~/.config/fulcra-collect` / `fulcra-collect:`
keychain prefix — and **those are already correct**. If a future C-variant *did*
touch them, it would orphan every installed user's daemon registration, config,
state DB, and stored Fulcra bearer token unless shipped with a migration
(rename plist + `launchctl bootout`/`bootstrap`, move config dir, copy keychain
items under the new service). Do not do that.

### RECOMMENDATION
**B, scoped tightly.** Rename the GitHub repo to `fulcra-collect`, sweep the
baked-in clone URLs (`collect_plugin.py:201-209`, `README.md`, `docs/TESTING.md`,
`chrome/README.md`), update `pyproject.toml:1` comment and the
`fulcra-common` description/`USER_AGENT`. **Leave all Python package/module and
console-script names as-is** and **do not touch** the launchd label, config dir,
or keychain prefix (already on-brand; changing them orphans users). Defer C
indefinitely — it's churn without user benefit.

---

## Decision 2 — Attention CLI ↔ daemon resolver consolidation

### Problem
Two code paths can establish/select the Attention definition:
- **CLI** `fulcra-attention bootstrap`/`defs`/`adopt` (`cli.py:32-127`), backed by
  `FulcraClient.ensure_definitions` / `list_attention_definitions` /
  `_find_attention_definition` in `fulcra.py:105-197`. These write
  `state.json` (`attention_definition_id`).
- **Daemon** `ctx.resolved_definition_id(ATTENTION_SPEC, canonical_name="Attention")`
  in `collect_plugin.py:82`, which delegates to
  `RunContext.resolved_definition_id` (`collect/.../plugin.py:325`) →
  `fulcra_common.definitions.resolve_definition_id` (`definitions.py:63`).

They overlap on the find-or-adopt-or-create decision. Both adopt the oldest
existing "Attention" def by `created_at`, so accounts converge either way
(`fulcra.py:181-197` vs `definitions.py:98-108`) — but the logic is duplicated.

**Dependency check:** the CLI commands are exercised only by their own tests
(`packages/attention/tests/test_cli.py`, `test_fulcra_tags_defs.py`) and
documented in `attention/README.md:21,47` and `attention/AGENTS.md:3`. **No
daemon, wizard, route, or other package imports or shells out to them** (grep
found no callers outside attention/ tests + docs). There is **no headless
deployment story in the repo** — no CI job, script, or doc that runs
`fulcra-attention bootstrap` as part of a real multi-machine setup. The wizard
(daemon) is the actual onboarding path.

### Options

| | Approach | Effort | Risk |
|---|---|---|---|
| **A** | Keep both; document the boundary (CLI = headless/scriptable/multi-machine; daemon = interactive). CLI docstring already says this (`cli.py:5-15`) | ~0 | None, but duplication persists |
| **B** | CLI delegates to the **same** resolver (`resolve_definition_id`) the daemon uses; CLI becomes a thin wrapper. One source of truth for find/adopt/create | Med (½–1 day): build the `_FulcraDefinitionAdapter` interface against `FulcraClient`, keep tag pre-creation which the resolver doesn't do) | Med: must preserve `CATEGORY_VOCAB`/`attention`+`web` tag seeding (`fulcra.py:109-116`), which the generic resolver has no concept of |
| **C** | Deprecate/remove `bootstrap`/`defs`/`adopt`; keep only `status`/`setup`/`reset` if useful | Low | Low (no external callers) but loses the multi-machine cleanup tool (`defs` lists duplicate defs incl. soft-deleted — the resolver can't) |

**Tension:** `bootstrap` does two jobs — (1) resolve/create the def (overlaps the
daemon) and (2) pre-seed the `attention`/`web`/category-vocab tags
(`fulcra.py:109-116`), which the daemon path does **not** do. `defs` provides
duplicate-cleanup visibility the resolver lacks. So "remove the overlap" is not
the same as "remove the commands."

### RECOMMENDATION
**A now, with a thin slice of B later — not C.** There's no headless story today,
so consolidating is not urgent; but the duplicated adopt/create logic is a real
drift risk. Concretely: (1) **keep all CLI commands** (low cost, and `defs`
gives duplicate-cleanup the daemon can't), and (2) when you next touch this code,
have `ensure_definitions`' *def-resolution step* call
`fulcra_common.definitions.resolve_definition_id` (via a small adapter over
`FulcraClient`) instead of its bespoke `_find_attention_definition`, while
**keeping the tag-seeding in the CLI**. That removes the duplicated
find/adopt/create logic without removing the CLI's unique value. Do **not** do C
— removing `defs`/`adopt` loses the only multi-machine duplicate-cleanup surface.

---

## Decision 3 — Attention definition-creation unification

### Problem (pre-existing, currently benign)
When the **daemon resolver** is first to create the Attention def, it passes only
the structural spec (`ATTENTION_SPEC` = `annotation_type` + `measurement_spec`).
The create adapter `_FulcraDefinitionAdapter.create_definition`
(`worker.py:55-75`) then defaults `description=""`, `tags=[]`
(`worker.py:68`). The **CLI** path
(`fulcra.py:130` → `attention_create_payload([attention, web])`) creates the def
with a real description (`"What the user paid attention to (browsing)."`,
`definition_spec.py:48`) **and** the `attention`/`web` tags.

Both paths adopt-by-name first (`fulcra.py:123-126`, `definitions.py:94-108`), and
adoption keys only on `annotation_type` + `measurement_spec` — so accounts
**converge on one def**. But a *resolver-first* user gets a sparser def
(empty description, no tags) than a *CLI-first* user. `definition_spec.py:22-32`
documents this exact gap and deliberately scopes itself to single-sourcing only
the *structural* match-spec.

### Options

| | Approach | Effort | Risk / data impact |
|---|---|---|---|
| **A** | Leave as-is (converges; gap is cosmetic) | 0 | None. Resolver-first users get description=""/tags=[] |
| **B** | Thread canonical description + tags through the resolver create path so both produce identical defs | Med | **Data-affecting**: changes what's written to Fulcra accounts; tags need resolved tag ids the daemon must look up at create time |

**Why B is non-trivial:** the resolver's generic `create_definition`
(`worker.py:55`) is shared by *every* plugin and is intentionally schema-agnostic
— it has no notion of Attention's `attention`/`web` tags and no tag-resolution
step (it just sets `tags=[]`). To make the resolver-created Attention def match
the CLI one, you'd need to (a) pull the canonical description from
`definition_spec.ATTENTION_CANONICAL["description"]`, and (b) resolve the
`attention`/`web` tag **ids** on the account before create — which means giving
the daemon path the tag-ensuring logic that currently lives only in
`FulcraClient.ensure_tag`/`ensure_definitions` (`fulcra.py:97-116`). That writes
new tags to user accounts, so it needs care: idempotent ensure, and it only
matters on *first* create (adopted defs are untouched, so no migration of
existing defs).

### RECOMMENDATION
**A for now; B only if Attention exits private beta and the resolver-first path
becomes the common onboarding.** The gap is genuinely benign today (accounts
converge, and the *interactive wizard* is the dominant path which already runs
the resolver — so most real users land on the sparse def, but it's still a valid
Attention def). If you choose B: source the description from
`definition_spec.ATTENTION_CANONICAL["description"]` and add an
Attention-specific create hook (not a change to the shared `worker.py`
adapter — keep that generic) that resolves `attention`/`web` tag ids via the
existing `ensure_tag` path and passes the full `attention_create_payload(...)`.
Scope it to first-create only; no backfill/migration of already-created defs is
needed because adoption never rewrites an existing def.

---

## Recommended sequencing

1. **Safe, do first:** Decision **1B** (repo rename + URL sweep) and Decision
   **2A** (document the CLI/daemon boundary — mostly already in docstrings).
   Neither touches user data.
2. **Needs care, do opportunistically:** Decision **2B**'s thin slice (point the
   CLI's def-resolution at the shared resolver, keep tag-seeding) — fold it into
   the next change that already touches `attention/fulcra.py`.
3. **Data-affecting, defer until justified:** Decision **3B** (unify create
   payload) — only when resolver-first becomes the common path. Decision **1C**
   (full module rename) — defer indefinitely; no user benefit, high churn, and a
   careless variant that touches the launchd label / `~/.config/fulcra-collect` /
   `fulcra-collect:` keychain prefix would **orphan installed users' state and
   credentials**. Those three strings are already on-brand — leave them alone.

**One-line verdict:** rename the repo, document the boundary, leave names and
paths alone, and treat the two Attention-internal unifications (2B, 3B) as
cleanup-on-touch, not standalone projects.
