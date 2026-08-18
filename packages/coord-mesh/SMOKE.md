# coord-mesh live smoke procedure (two accounts)

**Audience: whoever runs the live legs — not the author.** Every command is
literal, every expected shape is stated, and every failure mode says what it
means. If a step's actual output does not match, stop and record it; do not
interpret.

**Why this document exists:** the author's host has a safety classifier that
refuses cross-account verbs (`share create --user-id`, `get-records --user-id`),
so the package's unit tests have never met a second account. Everything below is
the part that only a host holding those grants can prove.

## Vocabulary

| symbol | meaning |
|---|---|
| `$ME` | the running account's own Fulcra uid |
| `$PEER` | the other account's uid (operator-named) |
| `$CH` | the outbox channel, `MomentAnnotation/<uuid>` |
| `$PEER_CH` | the PEER's outbox channel data type |

## Before anything: what "pass" means here

A green exit code is **not** a pass. Every leg below names the *evidence* that
counts. The package's whole thesis is that a verification surface must not claim
more than it measured — these steps hold the runner to the same standard.

---

## Leg 1 — `mesh init` mints a scoped share and proves it

```
coord-mesh --channel "$CH" init "$PEER" --name mesh-smoke --reports reports/
```

**Expect (rc 0):**
```
mesh init: granted <CH> -> <PEER> (data type read-back verified in share 'mesh-smoke'; reports prefix verified as 'file:/reports/')
```

### Before you run anything: your client version decides the outcome

This leg is **version-sensitive**, and two rounds were lost to not knowing it.

| installed `fulcra-api` | what a reports prefix can do |
|---|---|
| ≤ 0.1.39 | **Impossible by any path.** No `--file` option, and `--data-type file:/reports/` is refused by the client's own validation (`Invalid data type(s)`). |
| ≥ 0.1.40 | Works. `--data-type file:/reports/` is proven end-to-end; `--file` also exists as sugar and is deliberately not used. |

`fulcra-api` exposes **no version surface** — no `--version`, no `version`
subcommand — so you cannot ask the binary what it is, and three hosts each
believed they ran 0.1.40 while two of them did not. Check the installer:

```
uv tool list | grep fulcra-api
```

`coord-mesh init` does not trust you on this. It probes the installed client and
**refuses** a reports prefix it cannot deliver, rather than quietly minting a
channel-only share and calling it granted.

**What changed since the first 2026-08-18 run, and why.** That run died here in
argparse: `transport.py` appended `--file <prefix>` to a 0.1.39 client that had
no such option. The fix routes the prefix through the repeatable `--data-type`
as `file:/reports/` — the form it actually takes on a real share row. The
prefix is also now **verified** rather than disclaimed: an earlier version of
this document said a stderr caveat about an unverifiable prefix was correct and
expected. It was a 0.1.39 artifact. If you see that caveat, you are running
pre-fix code.

**Failure modes and what they mean:**

| output | meaning |
|---|---|
| `REFUSED: named-uid rail` | `$PEER` is not UUID-shaped. The rail is working; fix the input. |
| rc 2 `the installed fulcra-api cannot express a file grant` | Your client is ≤ 0.1.39. The fence is working. Upgrade (watch it) and re-run; nothing was created. |
| rc 3 `could not establish what the installed fulcra-api can do` | The capability probe itself failed. Not a share failure and not a client-too-old verdict — nothing was created. Record the probe's error. |
| rc 2 `--reports is whitespace only` | A typo, refused rather than crashed. Pass a path or omit the flag. |
| `No such option '--file'` | Pre-fix code. You are not running this document's package head. |
| rc 3 `create returned 0 but no share named 'mesh-smoke' ... is in list-outgoing` | The platform accepted the create but the share is not visible. **This is the interesting failure** — record the full `fulcra-api share list-outgoing` output. |
| rc 3 `CHANNEL is granted, the REPORTS PREFIX is not confirmed` | The share exists and carries the channel, but no `file:` data type came back. Events will flow and every `ptr` body will 404. **Record the row verbatim** — it would mean outgoing rows do not echo file grants the way the captured incoming row does, which is a platform fact this package has not yet measured directly. |
| rc 3 `read-back UNKNOWN` | The roster read failed. Not a share failure; retry before concluding. |

**Note:** if `$PEER` already holds a broad share from this account, leg 1 must
still report the scoped share by name. A pass that would have passed *before*
the create is not a pass — that exact defect was r2/r3 on this package.

## Leg 2 — `mesh send` writes and proves the NEW record

```
coord-mesh --channel "$CH" send --to-user "$PEER" --slug smoke-$(date +%s) --kind claim
```

Use a **fresh slug** each run (the `$(date +%s)` suffix does this). Expect rc 0:
```
mesh send: claim smoke-… -> <PEER> (read-back verified, new record <id>)
```

The `new record <id>` is the evidence. The verb snapshots the channel before
writing and requires an id absent from that snapshot, so a stale same-slug event
cannot satisfy it.

| output | meaning |
|---|---|
| rc 3 `refusing to write` | The pre-write snapshot was unreadable, so a read-back could not tell new from old. Correct behaviour; retry. |
| rc 3 `NO NEW record matching this event` | Write returned 0 but nothing new landed. **Record this** — it is the write-vs-claim gap the package is built to catch. |
| rc 3 `DRY RUN` | `--dry-run` was passed. Nothing was written; this is never a pass. |

## Leg 3 — the cross-account READ (the leg that has never run)

From the account that RECEIVES `$PEER`'s outbox:

```
coord-mesh --channel "$PEER_CH" queue --me "$ME" --peer "$PEER" --no-advance
```

`--no-advance` first: it proves the read without moving cursors, so the leg is
repeatable.

**Expect (rc 0):** zero or more lines of the form
```
  [P2] response some-slug from <agent-or-UNKNOWN-author> ptr=<path or ->
```

**What actually needs recording:** whether any line corresponds to the event
written in leg 2 from the other side. That — an event crossing the account
boundary — is the M1/M2 exit criterion. **A rc 0 with no matching line is not a
pass; it is a successful read that found nothing.**

| output | meaning |
|---|---|
| rc 3 `peer <uid>: UNKNOWN — cannot claim clear` | The cross-account read failed (classifier, grant, or transport). Paste the `detail` verbatim — it distinguishes those three. |
| rc 3 `row with no id` | A record arrived that cannot be deduped. Record it; it means the cursor cannot safely advance. |
| `UNKNOWN-author` in a line | Expected for platform-projection rows, which carry only reverse-DNS sources. Not a bug. |

Then repeat **without** `--no-advance` and run it twice: the second run should
show fewer (or zero) events, proving the cursor advanced.

## Leg 4 — `mesh doctor`

```
coord-mesh --channel "$CH" doctor
```
Expect rc 0 and three `ok` lines (incoming roster, own channel readable, peer
registry readable). Any `!` line is a real UNKNOWN — paste it whole.

---

## What to hand back

1. The verbatim output of each leg, including exit codes (`echo rc=$?`).
2. For leg 3, an explicit yes/no: **did an event written on one account appear
   on the other?** With the matching line if yes.
3. Anything that failed in a way this document did not predict — that is the
   most valuable thing you can return, because it means the package's model of
   its own failure modes is incomplete.

Do **not** summarise a leg as "worked". The package's thesis is that a claim
must carry its evidence; the transcript is the evidence.
