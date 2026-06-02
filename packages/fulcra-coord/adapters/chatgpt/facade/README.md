# Fulcra Coordination write facade

A thin HTTP service that gives the ChatGPT Custom GPT Action a **real write**:
`POST /coordination/report` (and a matching `GET /coordination/status`). It is
the clean answer to the gap the read-only Action left open — a milestone write
can't be a raw Fulcra Files call, because a correct write must upload a task
file **and** rebuild every materialized view with optimistic concurrency. That
logic already lives in the `fulcra_coord` package; this facade just exposes it
over HTTP.

## What it does

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /coordination/report` | facade bearer | Create-or-update a coordination task for `(agent_id, session_key)` (or a given `task_id`), then upload + rebuild views. |
| `GET /coordination/status` | facade bearer | Return the coordination index (counts + active + recent done), optionally filtered by `agent_id` / `workstream`. |
| `GET /healthz` | none | Liveness probe (no Fulcra dependency). |

`POST /coordination/report` body:

```json
{
  "agent_id": "chatgpt:fulcra-coord:ash",
  "session_key": "20260601T1730Z-r7q2",
  "summary": "Built the write facade",
  "next_action": "wire OAuth",
  "title": "ChatGPT write facade",       // used only when creating
  "workstream": "fulcra",                  // used only when creating (default: general)
  "status": "active",                      // optional transition: active | waiting only
  "task_id": "TASK-..."                    // optional: update this exact task
}
```

Response: `{ "task_id", "status", "created", "needs_reconcile" }`.

### Accepted `status` values

Only `active` and `waiting` are accepted. `done`, `block`, and `abandon` are
**rejected at the request schema (422)** because they can't be satisfied from
the facade's find-or-create flow: `done` needs `evidence` + `verification_level`
the facade never supplies, and `block`/`abandon` need a reason and/or are
illegal transitions out of a freshly-`proposed` task. Use the `fulcra-coord`
CLI for those. (Previously the OpenAPI advertised those values and the facade
400'd at the transition engine — now the contract is honest at the schema
layer.)

### Field constraints

To keep one milestone from bloating every materialized view, the request fields
are length-capped (422 on overflow): `summary` ≤ 4000, `title` ≤ 256,
`next_action` ≤ 2000, `agent_id`/`session_key` ≤ 200, `workstream` ≤ 100. A
whitespace-only `summary` or `title` is also rejected (422) — `min_length` alone
would let a string of spaces through.

### Find-or-create (deterministic, race-safe)

With no `task_id`, the facade find-or-creates the task for this working session.
The task **id itself is derived deterministically** from a hash of
`(agent_id, session_key)` —
`TASK-<YYYYMMDD>-<slug>-<hex8 of sha256(agent_id\x00session_key)>` — so two
*concurrent* first reports for the same session compute the **same** remote task
path and the package's optimistic-concurrency/merge layer collapses them into a
single task instead of racing into duplicates. A later report for the same
`(agent_id, session_key)` updates that same task. Supply `task_id` to target a
specific task explicitly.

### Read path: outage vs empty

`GET /coordination/status` distinguishes a real Fulcra outage from a genuinely
empty bus. If the backend is unreachable (host `fulcra-api` unauthenticated /
Fulcra down) it returns **503**, not a misleading `200` + empty index — so the
GPT never reports "no in-flight work" when it simply couldn't see the work. A
reachable-but-empty bus still returns `200` with an empty `active` list.

## Design: wraps the package, doesn't reimplement it

The facade calls the **same `fulcra_coord` functions the CLI uses** —
`schema.make_task`, `schema.apply_transition` / `schema.apply_update`, and
`cli._write_task_and_views` (task upload + `views.build_all_views` fan-out +
stat-based optimistic concurrency). It contains no task-write or view-rebuild
logic of its own.

**Import, not subprocess.** It imports `fulcra_coord` in-process rather than
shelling out to the `fulcra-coord` CLI, because:

- the package is pure-stdlib and importable wherever the facade runs;
- `cli._write_task_and_views` returns a bool and raises typed exceptions
  (`ConflictError` → HTTP 409, `NeedsReconcile` → 200 + `needs_reconcile`),
  which map onto HTTP far more cleanly than parsing CLI stdout/exit codes;
- the package's `remote` layer **already** shells out to `fulcra-api` for the
  real Fulcra I/O, so the "facade uses the host's Fulcra credentials" boundary
  holds either way — importing just removes a redundant second process hop.

## Auth model (two distinct legs — keep them separate)

1. **Inbound (GPT → facade):** a single shared bearer token in the
   `FULCRA_COORD_FACADE_TOKEN` env var, checked **constant-time**
   (`hmac.compare_digest`) on every request. Missing/wrong/unset → `401`. This
   is the secret you paste into the Custom GPT Action's auth config
   (`facadeBearer` in `../openapi.yaml`).
2. **Outbound (facade → Fulcra):** the facade holds **no** Fulcra credential.
   It uses whatever `fulcra-api` login already exists **on the host it runs
   on** (the `fulcra_coord.remote` layer invokes `fulcra-api file ...`). So the
   facade must run somewhere `fulcra-api` is authenticated
   (`fulcra-api auth login` / `fulcra-coord doctor` to verify).

If `FULCRA_COORD_FACADE_TOKEN` is unset the facade **fails closed** (all
authenticated requests → 401) rather than running open.

## Run it

```bash
# 1. Install deps (separate from the stdlib-only core package).
pip install -r requirements.txt          # or: pip install -e .

# 2. Make fulcra_coord importable — install the repo or set PYTHONPATH.
pip install -e ../../..                   # installs fulcra-coord from repo root
# (or)  export PYTHONPATH=/path/to/repo-root

# 3. Make sure THIS host can reach Fulcra (the facade uses host creds).
fulcra-api auth login
fulcra-coord doctor                       # expect Remote access: OK

# 4. Set the inbound token the GPT will send.
export FULCRA_COORD_FACADE_TOKEN="$(openssl rand -hex 32)"

# 5. Serve.
uvicorn app:app --host 0.0.0.0 --port 8080
```

Then point the Custom GPT Action's `servers[0]` (the facade base URL in
`../openapi.yaml`) at this host, and set the Action's auth to **API Key →
Bearer** with the same token.

## Demo deploy — hosted ChatGPT against `/coordination-demo`

This is the hosted-ChatGPT leg of the **three-agent coordination demo** (see
`docs/demo/2026-06-02-three-agent-coordination-demo.md`). It lets a Custom GPT
read and write the same `/coordination-demo` bus the CLI agents use.

**The facade host must itself be fulcra-api-authed** — the facade holds no Fulcra
credential of its own and performs every write with the host's `fulcra-api`
login. Verify with `fulcra-coord doctor` (expect `Remote access: OK`) before
starting.

### 1. Start the facade with the demo env

`run-demo.sh` starts the service pointed at `/coordination-demo`. It reads the
inbound bearer token from `FULCRA_COORD_FACADE_TOKEN` and **fails closed** if it
is unset — no secret is ever hardcoded.

```bash
cd adapters/chatgpt/facade
pip install -r requirements.txt
pip install -e ../../..                       # make fulcra_coord importable

export FULCRA_COORD_FACADE_TOKEN="$(openssl rand -hex 32)"   # save this — the GPT needs it
./run-demo.sh                                 # serves on :8787
# overrides: FACADE_PORT=9000 ./run-demo.sh   |   FULCRA_COORD_REMOTE_ROOT=/other ./run-demo.sh
```

### 2. Expose it over HTTPS with a tunnel

ChatGPT must reach the facade over public HTTPS. Either:

```bash
cloudflared tunnel --url http://localhost:8787
# -> prints a https://<random>.trycloudflare.com URL
```
or
```bash
ngrok http 8787
# -> prints a https://<random>.ngrok-free.app URL
```

Copy the printed public URL — that is the GPT Action's `server`.

### 3. Point a copy of the OpenAPI at the tunnel URL

Copy `../openapi.yaml`, and in the copy set the **first** `servers[0].url`
(the facade base URL placeholder `https://REPLACE-ME.facade.example.com`) to the
public tunnel URL from step 2. Leave the second server
(`https://api.fulcradynamics.com`, direct reads) untouched.

### 4. Create the Custom GPT

In the ChatGPT GPT builder → **Configure → Actions**:

1. **Instructions:** paste `../INSTRUCTIONS.md`.
2. **Schema:** import the edited OpenAPI copy from step 3.
3. **Authentication:** **API Key → Bearer**, value = the
   `FULCRA_COORD_FACADE_TOKEN` from step 1 (this satisfies the `facadeBearer`
   scheme on the report/status operations).

Now ask the GPT *"what's the team working on, what's blocked, anything falling
through the cracks?"* — it calls `GET /coordination/status` through the facade
and names the stale backfill task, matching the CLI digest.

> **Token rotation:** restart the facade with a new `FULCRA_COORD_FACADE_TOKEN`
> and update the GPT Action's Bearer value. Tunnel URLs from the free tiers
> change on each restart — re-point `servers[0]` if you restart the tunnel.

## Test

```bash
pip install -r requirements.txt
pytest tests -v
```

The tests run the facade against a **stateful local fake** of `fulcra-api file`
(`tests/fake_fulcra_backend.py`, wired in via `FULCRA_COORD_BACKEND`) so the
real `_write_task_and_views` path — task upload + view rebuild + concurrency —
executes end-to-end without touching live Fulcra. They cover: create + view
rebuild, same-session dedupe (update not duplicate), deterministic session task
id + single-task-under-race, status read, 503 on an unreachable backend vs 200
on a reachable-but-empty one, restricted `status` enum (422 on `done`/`blocked`),
length caps (422 on overflow), whitespace-only rejection, 401 on missing/bad
token, and 422 on malformed body.

## Deploy / open concerns

- **Hosting:** the facade is a running service — it needs a host where
  `fulcra-api` is authenticated and that ChatGPT can reach over HTTPS. That is
  the remaining operational departure from "zero-infrastructure" (carried in
  the design spec).
- **Token rotation:** `FULCRA_COORD_FACADE_TOKEN` is a shared secret; rotate it
  by restarting the facade with a new value and updating the GPT Action config.
- **Outbound identity:** every write the facade makes is attributed on the bus
  to `agent_id` from the request body, but performed with the **host's** Fulcra
  identity — fine for a single-tenant facade; multi-tenant would need per-user
  Fulcra auth (the same Auth0 question the read Action's OAuth raises).

## Files

| File | What |
| --- | --- |
| `app.py` | The FastAPI app (endpoints, auth, find-or-create, package wiring). |
| `run-demo.sh` | Starts the facade against `/coordination-demo` for the three-agent demo (reads token from env, fails closed if unset). |
| `requirements.txt` / `pyproject.toml` | Facade-only deps (FastAPI/uvicorn/pydantic) — separate from the stdlib-only core package. |
| `tests/test_facade.py` | Pytest suite using `fastapi.testclient.TestClient`. |
| `tests/fake_fulcra_backend.py` | Stateful local fake of `fulcra-api file` for the tests. |
