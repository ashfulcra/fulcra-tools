#!/usr/bin/env python3
import json, subprocess, sys, datetime, re, os

# router-watchdog — TWO independent checks on a wake router.
#
# Set COORD_ROUTER_ROOT to your team's router directory in the file store.
#
#   A. LIVENESS      — is the router process on the VPS still running at all?
#   B. ASSOCIATION   — do the configured session_refs still point at live sessions?
#
# B without A is the trap this script was born from: in one live incident every binding
# in config.json checked green while the router itself had been dead for 70h.
# A fresh config.json proves an AGENT wrote it, not that the ROUTER read it.
# So liveness is measured ONLY on router-WRITTEN artifacts (cursor.json,
# delivered.json), never on config.json or on this script's own mtime.
#
# usage: router-watchdog.py <config.json> [assoc_max_age_h] [router_max_silence_h]

ROOT = os.environ.get("COORD_ROUTER_ROOT", "team/<team>/_coord/router")
ROUTER_WRITTEN = ["cursor.json", "delivered.json"]   # NOT config.json — agents write that
ASSOC_MAX_H  = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
SILENCE_MAX_H = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0   # cursor writes every ~4min
FULCRA = os.environ.get("FULCRA_CLI_COMMAND", "fulcra-api")
now = datetime.datetime.now(datetime.timezone.utc)
alarms = 0

# ---- A. LIVENESS -----------------------------------------------------------
newest_write, newest_name = None, None
unreadable = []
for name in ROUTER_WRITTEN:
    try:
        out = subprocess.run([FULCRA, "file", "stat", f"{ROOT}/{name}"],
                             capture_output=True, text=True, timeout=90).stdout
    except Exception as e:
        unreadable.append(f"{name}: {e}"); continue
    m = re.search(r"^Uploaded:\s*(\S+)", out, re.M)
    if not m:
        unreadable.append(f"{name}: no Uploaded line"); continue
    ts = datetime.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    if newest_write is None or ts > newest_write:
        newest_write, newest_name = ts, name

if newest_write is None:
    # fail closed: cannot read the artifacts => cannot claim the router is alive
    alarms += 1
    print(f"  [UNKNOWN] router liveness unreadable ({'; '.join(unreadable)}) — failing closed")
else:
    silence_h = (now - newest_write).total_seconds() / 3600
    if silence_h > SILENCE_MAX_H:
        alarms += 1
        print(f"  [DOWN  ] wake router silent {silence_h:.1f}h "
              f"(> {SILENCE_MAX_H}h) — newest router-written artifact "
              f"{newest_name} @ {newest_write.isoformat()}")
        print(f"           directed wakes have NOT been delivered since then; "
              f"listeners are the only live path")
    else:
        print(f"  [ok    ] wake router alive — {newest_name} written "
              f"{silence_h*60:.0f}min ago")

# ---- B. ASSOCIATION --------------------------------------------------------
# Recency, not mere presence: a dead session's id lingers forever in the commits
# it made before dying, so "does it appear at all" is a false pass.
cfg = json.load(open(sys.argv[1]))
log = subprocess.run(["git", "-C", os.environ.get("COORD_REPO", "/home/user/fulcra-tools"),
                      "log", "--since=7 days ago", "origin/main", "--format=%cI%b"],
                     capture_output=True, text=True).stdout
seen_newest, cur = {}, None
for tok in re.findall(r"^\d{4}-\d{2}-\d{2}T[\d:+\-]+|session_[A-Za-z0-9]{20,}", log, re.M):
    if tok.startswith("20"):
        cur = tok
    elif cur and (tok not in seen_newest or cur > seen_newest[tok]):
        seen_newest[tok] = cur

checked = 0
skipped: list = []
if not seen_newest:
    alarms += 1
    print("  [UNKNOWN] no session evidence in 7d of git history — failing closed")
else:
    for agent, conf in cfg.items():
        if agent == "executors" or not isinstance(conf, dict):
            continue
        ref = (conf.get("adapter_args") or {}).get("session_ref")
        if not ref:
            # NO SILENT NARROWING. Agents routed by `executor` rather than
            # `session_ref` have nothing for this check to compare, and the
            # first version just skipped them — so it printed a confident
            # "0 stale" about 2 of 5 bindings and said nothing about the other
            # 3. A watchdog that quietly shrinks its own scope reports green
            # for a population it never looked at.
            skipped.append(f"{agent} (routes by executor, no session_ref)")
            continue
        checked += 1
        seen = seen_newest.get(ref)
        if not seen:
            alarms += 1
            print(f"  [STALE ] {agent} -> {ref}  (no artifact in 7d)")
            continue
        age = (now - datetime.datetime.fromisoformat(seen)).total_seconds() / 3600
        if age > ASSOC_MAX_H:
            alarms += 1
            print(f"  [STALE ] {agent} -> {ref}  newest artifact {age:.1f}h old "
                  f"(> {ASSOC_MAX_H}h) — identity moved")
        else:
            print(f"  [ok    ] {agent} -> {ref}  newest artifact {age:.1f}h old")

pool = set(cfg.get("executors") or [])
for agent, conf in cfg.items():
    if agent == "executors" or not isinstance(conf, dict):
        continue
    ex = conf.get("executor")
    if ex and ex not in pool:
        alarms += 1
        print(f"  [STALE ] {agent} -> executor {ex!r} not in pool {sorted(pool)}")

# positive heartbeat: prove it RAN, with counts, every run — not only on failure
for s_ in skipped:
    print(f"  [unchecked] {s_}")
print(f"router-watchdog: liveness=1 check, associations={checked} checked / "
      f"{len(skipped)} unchecked, {alarms} alarm(s)")
sys.exit(1 if alarms else 0)
