# Custom GPT setup — turnkey hosted ChatGPT against fulcra-coord

This is the copy-paste runbook for standing up a **hosted Custom GPT** that reads
and writes the `fulcra-coord` coordination bus through the facade. Everything the
GPT needs is in this directory:

| File | Use |
| --- | --- |
| `INSTRUCTIONS.md` | Paste into the GPT's **Instructions** field. |
| `openapi.yaml` | Import as the GPT's **Action schema** (after setting the server URL). |
| `SETUP.md` | This runbook. |

The GPT talks only to the **facade** (`../facade/`), which wraps the
`fulcra_coord` package so a milestone write does the task upload + view rebuild
server-side. The facade holds no Fulcra credential of its own — it uses the
**host's** `fulcra-api` login for all outbound Fulcra I/O. See
`../facade/README.md` for the facade design and the two-leg auth model; this
runbook does not duplicate the app code.

## 1. Run the facade

The facade must run on a host that is itself `fulcra-api`-authenticated (it
performs every write with the host's login). Verify with `fulcra-coord doctor`
(expect `Remote access: OK`) before starting.

```bash
cd adapters/chatgpt/facade
pip install -r requirements.txt
pip install -e ../../..                       # make fulcra_coord importable

export FULCRA_COORD_FACADE_TOKEN="$(openssl rand -hex 32)"   # SAVE THIS — the GPT needs it
./run-demo.sh                                 # serves on :8787, root /coordination-demo
# overrides: FACADE_PORT=9000 ./run-demo.sh  |  FULCRA_COORD_REMOTE_ROOT=/other ./run-demo.sh
```

`run-demo.sh` reads the inbound bearer token from `FULCRA_COORD_FACADE_TOKEN` and
**fails closed** if it is unset — no secret is ever hardcoded. Keep this token;
it is the Bearer value you paste into the GPT in step 4.

## 2. Expose the facade over HTTPS with a tunnel

ChatGPT must reach the facade over public HTTPS. Use either:

```bash
cloudflared tunnel --url http://localhost:8787
# -> prints a https://<random>.trycloudflare.com URL
```
or
```bash
ngrok http 8787
# -> prints a https://<random>.ngrok-free.app URL
```

Copy the printed public URL — this is your `{{FACADE_BASE_URL}}`.

## 3. Create the Custom GPT and import the schema

In the ChatGPT GPT builder → **Configure**:

1. **Instructions:** paste the block from `INSTRUCTIONS.md`.
2. **Actions → Schema:** take `openapi.yaml` from this directory and replace the
   `{{FACADE_BASE_URL}}` placeholder in `servers[0].url` with the public tunnel
   URL from step 2, then import it. (Both operations — `reportMilestone` and
   `getCoordinationStatus` — are served from that one facade URL.)

## 4. Set the Bearer token

In **Actions → Authentication**: choose **API Key → Bearer**, and set the value
to the `FULCRA_COORD_FACADE_TOKEN` you generated in step 1. This satisfies the
`facadeBearer` scheme both operations require.

## 5. Verify

Ask the GPT:

> *"What's the team working on, what's blocked, anything falling through the
> cracks?"*

It calls `getCoordinationStatus` through the facade and names the active work
(and the stale backfill task if you seeded the demo bus), matching the CLI
`fulcra-coord agents` digest. Then have it report a milestone — *"log that I
finished the schema import"* — and confirm the returned `task_id` shows up in the
status read.

## Notes

- **Token rotation:** restart the facade with a new `FULCRA_COORD_FACADE_TOKEN`
  and update the GPT Action's Bearer value to match.
- **Tunnel URLs change** on free-tier restarts — re-point `servers[0].url` in the
  imported schema if you restart the tunnel.
- **Best-effort by design:** ChatGPT has no deterministic session start/end hook,
  so the start-of-session read and milestone writes are model-chosen. The
  server-side heartbeat reconciler is the safety net that re-flags abandoned
  active tasks (see the project README and `../facade/README.md`).
