# Other-Side Claude Code Test Plan

This plan verifies that a Claude Code session in a different environment can coordinate through Fulcra Files without Tailscale, SSH, shared disk, or direct calls to the originating agent. It was updated after passes 4 and 5 to document the real cross-machine risks that were fixed and to make pass/fail criteria unambiguous.

## Test Root

Use a disposable Fulcra Files root created by the first-side smoke test:

```bash
export FULCRA_COORD_REMOTE_ROOT=/coordination-smoke/arc-493-20260601
```

The first-side session seeded this task for discovery:

```text
TASK-20260601-other-side-claude-code-c-229ec9e5
```

---

## How the key operations work

Understanding these semantics is required to diagnose failures correctly.

### `status`

Calls `_load_all_tasks`, which:
1. Downloads `index.json` from the remote root.
2. Downloads `search-index.json` and **writes it to the local cache** — so a subsequent `search` command on the same machine sees a fresh remote copy, not stale local data.
3. Downloads every task referenced in the remote index (and search-index) that is not already cached locally.

On a fresh machine with no local cache, `status` is what populates the task and search-index caches. Run `status` before `search` on a fresh machine to avoid stale results.

### `search`

Uses the cached `search-index` if one exists. If no cache exists (never ran `status` or `reconcile`), falls back to `_load_all_tasks` and searches in memory. **On a fresh machine, run `status` at least once before `search`** to guarantee the search-index reflects the current remote state.

### Write operations (`start`, `update`, `done`, `pause`, `block`)

Each write uses a **two-cache merge check** before uploading:

1. **Pre-stat** the remote task file (live network call).
2. Compare the pre-stat against the **cached meta** (last-known stat from the most recent write or download on this machine).
3. If either of the following is true, download the latest remote version and attempt a structured merge:
   - The cached meta is absent (fresh machine, no prior write history) **but the file already exists remotely** — this prevents silent overwrite of concurrent changes from other agents.
   - The cached meta exists but differs from the pre-stat (another agent wrote the file since this machine last touched it).
4. If both sides independently changed the task's status, the merge is rejected as unsafe (`ConflictError`). Run `fulcra-coord reconcile`, inspect the task history, and retry.
5. Upload the task file, then post-stat to record the new version in the local meta cache.

After the task file is uploaded, the write path calls **`_load_all_tasks`** (not the local cache alone) before regenerating views. This ensures a fresh machine that previously loaded only one task via `_load_task` pulls all remote tasks into cache before fan-out — without this, a single-task session would build views from a truncated set and silently drop tasks it never individually fetched.

### `reconcile`

Like `status` (calls `_load_all_tasks`), then re-uploads every view. Also clears pending operation markers for repairs that succeed. If view uploads fail, markers are left in place for the next reconcile run.

---

## Files-capable CLI requirement

The `doctor` command probes the **file subcommand** specifically (`fulcra-api file --help`), not the base CLI. A Fulcra CLI build that lacks Files support will pass a base-CLI presence check but fail every actual coordination write with a cryptic error. The doctor probe catches this before any write is attempted.

**Backend resolution order** (how `fulcra-coord` finds the CLI):

1. `FULCRA_COORD_BACKEND` env var — testing only; splits on whitespace and uses as the full command.
2. `FULCRA_CLI_COMMAND` env var — e.g. `my-wrapper fulcra-api`; the package appends `file` automatically.
3. `fulcra-api` found on `PATH` — standard install.
4. `uv tool run fulcra-api` — automatic fallback when `fulcra-api` is not on PATH but is installed as a uv tool.

If step 4 is triggered and `uv` is not present, the CLI check will fail. Install via `pip install fulcra-coord` or `uv tool install fulcra-coord`, or set `FULCRA_CLI_COMMAND` explicitly.

The Files-capable Fulcra CLI requirement is documented in [`docs/fulcra-cli-branch.md`](fulcra-cli-branch.md). If the installed `fulcra-api` exposes the `file` command group, use it directly. Otherwise point `FULCRA_CLI_COMMAND` at another Files-capable build.

---

## Preconditions

- Fresh local, cloud, or ephemeral Claude Code environment.
- Python 3.10+.
- A Files-capable Fulcra CLI build. Verify with:

  ```bash
  fulcra-api file --help
  ```

  If the `file` subcommand is missing, install a build that includes Fulcra Files support, or point to one via:

  ```bash
  export FULCRA_CLI_COMMAND="<path-to-files-capable-fulcra-cli>"
  ```

  Arc's local reference build is `arc/integrated-cli-prs` at commit `c164ad6`, but the portable requirement is the CLI surface, not that local branch. The currently verified remote branch is `file-management` at remote head `ab3090c` as of 2026-06-01. Do not use `file-commands` unless it has since gained the `restore` subcommand.

- Do not paste tokens into chat or logs. Use device auth, a platform secret store, or an already-authenticated CLI credential file.

---

## Install

### From package (preferred)

```bash
pip install fulcra-coord
# or, as a standalone tool outside a Python project:
uv tool install fulcra-coord
```

### From repo (fallback / development)

```bash
git clone <repo-url> fulcra-coord
cd fulcra-coord
pip install -e .
# or:
uv tool install -e .
```

After either install, verify the entry point resolves:

```bash
fulcra-coord --help
```

If `fulcra-coord` is not on `PATH` after install, use `python3 -m fulcra_coord` or run `fulcra-coord install-shim` to write a launcher to `~/.local/bin/`.

---

## Steps

### 1. Authenticate the Fulcra CLI

```bash
fulcra-api auth login --no-browser
```

Open the device URL on any trusted browser, enter the code, and approve access. Auth is stored in the CLI credential file (not in environment variables).

**Pass:** `fulcra-api auth status` shows an authenticated account.
**Fail:** Re-run device flow. In ephemeral environments, restore the credential file from a secret store before running (see `docs/auth.md`).

---

### 2. Verify the coordination backend

```bash
fulcra-coord doctor
```

The `doctor` command checks:
- **CLI reachable**: probes `fulcra-api file --help` — catches builds that lack Files support.
- **Remote access**: stats `index.json` at the configured root — catches auth and root mismatches.
- **Pending ops**: reports any operation markers that need reconcile.
- **Cache state**: reports locally cached task count.

**Pass:** Both `CLI reachable: OK` and `Remote access: OK` are printed.
**Fail:**
- `CLI reachable: FAIL` → install a Files-capable build or set `FULCRA_CLI_COMMAND`.
- `Remote access: FAIL` → check auth (`fulcra-api auth status`) and `FULCRA_COORD_REMOTE_ROOT`.
- Fresh root, no tasks yet → `Remote access: FAIL` on `index.json` is expected until `start` initializes the root; skip to step 3 if this is the source-side.

---

### 3. Confirm the seeded task is visible

Run `status` first — this downloads `index.json` and `search-index.json` from the remote and caches them locally. Without this step, `search` may see an empty or stale local cache.

```bash
fulcra-coord status --format json
fulcra-coord search "Other-side Claude Code"
```

**Pass:** `TASK-20260601-other-side-claude-code-c-229ec9e5` appears with status `proposed`, priority `P2`, workstream `devops`.
**Fail:**
- Task missing from `status` JSON: confirm `FULCRA_COORD_REMOTE_ROOT` matches the first-side value and auth is for the same Fulcra account.
- Task missing from `search` but present in `status` JSON: the search-index cache is stale — re-run `status` to refresh it, or run `fulcra-coord reconcile`.

---

### 4. Claim the task from the other side

```bash
fulcra-coord update TASK-20260601-other-side-claude-code-c-229ec9e5 \
  --agent other-side-claude-code \
  --status active \
  --summary "Other-side Claude Code can read and claim the Fulcra-backed task." \
  --next "Write a remote-side evidence update, then pause or complete."
```

Internally this triggers the two-cache merge check. Since this is a fresh machine with no prior write history, `cached_meta` is absent. If the task exists remotely (it does — it was seeded by the first side), the merge check downloads the latest version and merges rather than overwriting blindly.

**Pass:** Command exits 0, prints `Updated: TASK-20260601-...`, no `ConflictError`.
**Fail:**
- `ConflictError`: another agent updated the task between your `status` and this `update`. Run `fulcra-coord reconcile` and retry.
- Upload fail: run `fulcra-coord doctor` to check connectivity, then `fulcra-coord reconcile`.

---

### 5. Write evidence from the other side

```bash
fulcra-coord update TASK-20260601-other-side-claude-code-c-229ec9e5 \
  --agent other-side-claude-code \
  --summary "Verified from a separate environment: doctor, status, search, and update all worked." \
  --next "First-side agent should reconcile and verify the update is visible."
```

**Pass:** Command exits 0, prints `Updated: TASK-20260601-...`.

---

### 6. Pause the task for first-side verification

```bash
fulcra-coord pause TASK-20260601-other-side-claude-code-c-229ec9e5 \
  --agent other-side-claude-code \
  --next "First-side agent should verify this paused task and mark done with evidence."
```

**Pass:** Command exits 0, prints `Paused: TASK-20260601-...`.

---

### 7. Reconcile and confirm views are consistent

```bash
fulcra-coord reconcile
fulcra-coord status
```

Reconcile calls `_load_all_tasks` (full remote sync), rebuilds all views (index, active, next, recently-done, search-index, workstream views, agent views), uploads them all, and clears any pending operation markers.

**Pass:**
- Reconcile exits 0, prints `Reconcile complete. N views refreshed.`
- `status` shows the task as `waiting` with the next_action set by step 6.
- `doctor` shows `Needs reconcile: 0`.

**Fail:**
- Partial view failures: `WARN: View upload failures: [...]` — run reconcile again when connectivity is stable.
- Remaining operation markers: `fulcra-coord doctor` reports `Needs reconcile: N` — do not mark done until markers are cleared; views may be stale.

---

## First-Side Verification

Back on the originating side, sync remote state:

```bash
fulcra-coord reconcile
fulcra-coord search "Other-side Claude Code"
```

**Pass:** The other-side update (`agent: other-side-claude-code`) and the `waiting` status are visible.

The task is now `waiting`. The valid transition path to `done` is `waiting → active → done`. Attempting `waiting → done` directly is rejected by the schema — `done` requires an `active → done` transition.

```bash
# Transition waiting → active
fulcra-coord update TASK-20260601-other-side-claude-code-c-229ec9e5 \
  --status active \
  --agent arc-local

# Transition active → done (requires evidence)
fulcra-coord done TASK-20260601-other-side-claude-code-c-229ec9e5 \
  --agent arc-local \
  --evidence "First side read back the other-side Claude Code update from Fulcra Files." \
  --verification-level agent-verified
```

**Pass:** `>>> Marked TASK-20260601-... done: First side read back...` printed to stdout.

---

## Cross-Machine Risk Matrix

| Risk | Mechanism | Test step |
|---|---|---|
| Fresh machine silently overwrites concurrent remote changes | Two-cache merge check triggers on absent `cached_meta` when file exists remotely | Step 4 |
| Fresh machine builds truncated views (drops tasks it never loaded) | Write fan-out calls `_load_all_tasks` not local cache | Steps 5–7 (view count in reconcile output) |
| Search sees stale results after remote update | `status` downloads and caches remote `search-index` | Step 3 (`status` before `search`) |
| `search` misses remotely updated task if run before `status` | Covered: step 3 requires `status` first | Step 3 ordering |
| `waiting → done` attempted directly | Schema rejects; only `active → done` is valid | First-side verification |
| Concurrent dual-agent status conflict | Merge logic returns `ConflictError` when both sides added status-change events | Step 4 failure path |
| View upload partial (fan-out not atomic) | Operation marker written; reconcile repairs | Step 7 |
| Base CLI present but Files subcommand absent | `doctor` probes `file --help`, not base CLI | Step 2 |

---

## Failure Diagnostics

- **`doctor` says file command missing**: install a Files-capable Fulcra CLI build or set `FULCRA_CLI_COMMAND`.
- **Auth succeeds but remote access fails**: confirm the same Fulcra account is used on both sides and `FULCRA_COORD_REMOTE_ROOT` matches exactly.
- **Update succeeds locally but remote is stale**: run `fulcra-coord reconcile`, then check `doctor` for pending markers.
- **`ConflictError`**: another agent updated the task concurrently. Run `fulcra-coord reconcile`, review the task event history (`fulcra-coord status --format json` or read the task file directly), and retry with the latest task state.
- **Search returns empty on fresh machine**: run `fulcra-coord status` first to populate the local search-index cache from the remote, then retry `search`.
- **`done` rejected on `waiting` task**: transition to `active` first (`update --status active`), then run `done`.

---

## Cleanup

After both sides pass, delete the disposable `/coordination-smoke/arc-493-20260601` files from Fulcra Files or keep them as a demo fixture until the public walkthrough is written.
