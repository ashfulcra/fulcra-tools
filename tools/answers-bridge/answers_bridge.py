#!/usr/bin/env python3
"""Ash Answers scratchpad bridge — bus-backed, Linear-viewed.

Directions (each single-purpose, to avoid bidirectional state-sync bugs):
  capture   bus->Linear (one way): record an answer as a durable bus shard AND a
            Linear card in the "Ash · Answers" project. Idempotent by id.
  promote   Linear->bus (one way, ash-triggered): cards Ash labeled `promote`
            become a bus backlog task via `coord-engine later`; the bus slug is
            commented back and the card gets `filed` + moved to Done.
  list      read-only: open answer cards, for terminal reference.

Check-off is Linear-only (mark Done) — deliberately NOT synced back, so there is
no fragile two-way state channel.

IDs from answers-linear-ids.json; creds from linear.env (never on the bus/git).
Exit: 0 ok, 2 degraded (stderr says which step).
"""
import hashlib, json, os, re, ssl, subprocess, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.linear.app/graphql"
CA = "/root/.ccr/ca-bundle.crt"
TEAM = os.environ.get("COORD_TEAM", "fulcra")
# creds (secret) never live in the repo — read from $ANSWERS_LINEAR_ENV, else the
# session scratchpad, else next to this script. IDs (non-secret) sit beside it.
_ENV_CANDIDATES = [
    os.environ.get("ANSWERS_LINEAR_ENV", ""),
    "/tmp/claude-0/-home-user-fulcra-tools/a07b97e8-9d5f-59f3-8df6-9ceba3d40af6/scratchpad/linear.env",
    os.path.join(HERE, "linear.env"),
]

def _load_env():
    p = next((c for c in _ENV_CANDIDATES if c and os.path.exists(c)), None)
    if not p:
        raise SystemExit("linear.env not found (set ANSWERS_LINEAR_ENV)")
    env = {}
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env

ENV = _load_env()
KEY = ENV.get("LINEAR_API_KEY", "")
IDS = json.load(open(os.path.join(HERE, "answers-linear-ids.json")))


def gql(query, variables=None):
    req = urllib.request.Request(
        API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": KEY})
    ctx = ssl.create_default_context(cafile=CA if os.path.exists(CA) else None)
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        out = json.load(r)
    if out.get("errors"):
        raise RuntimeError(out["errors"][0].get("message", "graphql error"))
    return out["data"]


def bus_write(path, content):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(content)
        local = fh.name
    try:
        cp = subprocess.run(["fulcra-api", "file", "upload", local, path],
                            capture_output=True, text=True, timeout=60)
        return cp.returncode == 0
    finally:
        try: os.unlink(local)
        except OSError: pass


def mk_id(question, by):
    stem = re.sub(r"[^a-z0-9]+", "-", (question or "").lower()).strip("-")[:40]
    h = hashlib.sha1(f"{question}|{by}".encode()).hexdigest()[:8]
    return f"{stem}-{h}" if stem else f"ans-{h}"


TYPE_LABEL = {"factual": "type:factual", "future": "type:future-work",
              "future-work": "type:future-work", "both": "type:both"}


def bus_read(path):
    """Read a bus file; None on failure. NEVER use None to infer absence —
    absence is only proven by a successful bus_list that omits the name."""
    cp = subprocess.run(["fulcra-api", "file", "download", path, "-"],
                        capture_output=True, text=True, timeout=60)
    return cp.stdout if cp.returncode == 0 else None


def bus_list(dirpath):
    """List a bus directory. Returns the set of entry names on success, or
    None on ANY failure — callers must fail closed on None (a degraded listing
    is UNKNOWN existence, never absence)."""
    cp = subprocess.run(["fulcra-api", "file", "list", dirpath],
                        capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        return None
    names = set()
    for line in cp.stdout.splitlines():
        parts = line.split()
        if parts:
            names.add(parts[-1])
    return names


def _board_has_title(title):
    """True/False if the board fold definitively shows/omits a task with this
    exact title; None if the fold is unavailable (callers fail closed)."""
    cp = subprocess.run(["coord-engine", "board", TEAM, "--json"],
                        capture_output=True, text=True, timeout=180)
    if cp.returncode != 0:
        return None
    try:
        d = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return None
    for lane in d.values() if isinstance(d, dict) else []:
        if isinstance(lane, list):
            for row in lane:
                if isinstance(row, dict) and row.get("title") == title:
                    return True
    return False


def _project_issues():
    out, cursor = [], None
    while True:
        page = gql("query($p:ID!,$c:String){issues(filter:{project:{id:{eq:$p}}},first:100,after:$c)"
                   "{nodes{id identifier title url description state{name} labels{nodes{id name}}} "
                   "pageInfo{hasNextPage endCursor}}}", {"p": IDS["project_id"], "c": cursor})["issues"]
        out += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return out


def _find_card_by_aid(aid):
    """Existing card for a bus answer id, keyed by the shard path in the card
    body (the durable identity), never the title."""
    needle = f"answers/{aid}.md"
    for n in _project_issues():
        if needle in (n.get("description") or ""):
            return n
    return None


def cmd_capture(a):
    """Create/refresh one answer card + bus shard. Idempotent by answer id:
    a re-run (or a retry after a lost Linear response) UPDATES the existing
    card found via the shard path in its body instead of creating a duplicate."""
    aid = a.get("id") or mk_id(a["q"], a.get("by", "?"))
    typ = TYPE_LABEL.get((a.get("type") or "factual").lower(), "type:factual")
    lbls = [IDS["labels"]["qa-answer"], IDS["labels"][typ]]
    title = a["q"].strip()
    if len(title) > 200:
        title = title[:197] + "…"
    body = (f"**Q:** {a['q'].strip()}\n\n**A:** {a['a'].strip()}\n\n"
            f"---\nanswered by: {a.get('by','?')}  ·  type: {typ.split(':')[1]}"
            f"  ·  bus: `team/{TEAM}/answers/{aid}.md`\n"
            f"(check off = mark Done · label `promote` to turn this into a bus task)")
    # bus shard (durable record; same-path write is naturally idempotent)
    shard = (f"---\ntype: Answer\nid: {aid}\nby: {a.get('by','?')}\n"
             f"answer_type: {typ.split(':')[1]}\nts: {a.get('ts','')}\n---\n"
             f"Q: {a['q'].strip()}\n\nA: {a['a'].strip()}\n")
    ok = bus_write(f"team/{TEAM}/answers/{aid}.md", shard)
    if not ok:
        print(f"DEGRADED: bus shard write failed for {aid}", file=sys.stderr)
    existing = _find_card_by_aid(aid)
    if existing:
        gql("mutation($i:String!,$in:IssueUpdateInput!){issueUpdate(id:$i,input:$in){success}}",
            {"i": existing["id"], "in": {"title": title, "description": body, "labelIds": lbls}})
        print(f"refreshed {aid} -> {existing['identifier']} {existing['url']}")
        return 0 if ok else 2
    inp = {"teamId": IDS["team_id"], "projectId": IDS["project_id"],
           "title": title, "description": body,
           "stateId": IDS["states"]["open"], "labelIds": lbls}
    r = gql("mutation($in:IssueCreateInput!){issueCreate(input:$in){issue{id identifier url}}}",
            {"in": inp})["issueCreate"]["issue"]
    print(f"carded {aid} -> {r['identifier']} {r['url']}")
    return 0 if ok else 2


def cmd_list(a):
    for n in _project_issues():
        names = {l["name"] for l in n["labels"]["nodes"]}
        if n["state"]["name"] == "Done":
            continue
        mark = "‣" if "promote" in names else "○"
        typ = next((x.split(":")[1] for x in names if x.startswith("type:")), "?")
        print(f"{mark} [{typ}] {n['identifier']}: {n['title']}")
    return 0


def _receipt_body(card, status, slug=""):
    return (f"---\ntype: PromotionReceipt\ncard: {card}\nstatus: {status}\n"
            f"slug: {slug}\n---\n")


def cmd_promote(a):
    """Cards labeled `promote` and not yet `filed` -> bus backlog task, link back.

    Retry-safe with DURABLE dedupe across every partial-failure window, via a
    two-phase receipt (team/<team>/answers/_promotions/<card>.md, keyed by the
    card's immutable Linear identifier):
      intent  (status: pending) written BEFORE `coord-engine later` runs
      filed   (status: filed, slug) written after `later` succeeds
      finalize (Linear label+Done+comment) runs only with a filed receipt
    Recovery rules: receipt existence comes from ONE directory listing per run
    (a degraded listing skips the whole pass fail-closed — UNKNOWN is never
    absence); an unreadable individual receipt skips that card; `pending` with
    no slug resolves via the board fold (task present -> adopt as filed; absent
    -> re-run `later`; fold unavailable -> skip fail-closed); `filed` skips
    `later` and retries the finalize only. No path re-runs `later` while the
    task's existence is unknown."""
    promote_id = IDS["labels"]["promote"]; filed_id = IDS["labels"]["filed"]
    receipts_dir = f"team/{TEAM}/answers/_promotions/"
    existing_receipts = bus_list(receipts_dir)
    if existing_receipts is None:
        print("DEGRADED: promotions listing unavailable — skipping promote pass "
              "(existence UNKNOWN, fail closed)", file=sys.stderr)
        print("promote: 0 card(s) filed [DEGRADED]")
        return 2
    n_done = 0
    degraded = False
    for n in _project_issues():
        names = {l["name"] for l in n["labels"]["nodes"]}
        if "promote" not in names or "filed" in names:
            continue
        title = n["title"]
        receipt_name = f"{n['identifier']}.md"
        receipt_path = receipts_dir + receipt_name
        status, slug = None, ""
        if receipt_name in existing_receipts:
            raw = bus_read(receipt_path)
            if raw is None:
                print(f"DEGRADED: receipt for {n['identifier']} exists but is unreadable — "
                      f"skipping card (never re-file on an unreadable receipt)", file=sys.stderr)
                degraded = True
                continue
            sm = re.search(r"^status:[ \t]*(\S+)", raw, re.M)
            gm = re.search(r"^slug:[ \t]*(\S+)", raw, re.M)
            status = sm.group(1) if sm else "pending"  # legacy receipts = filed pre-status
            if not sm and gm:
                status = "filed"
            slug = gm.group(1) if gm else ""
        if status == "pending" and not slug:
            # Ambiguous window: intent written, unknown whether `later` ran.
            # Resolve against the board — never blind-retry.
            present = _board_has_title(title[:160])
            if present is None:
                print(f"DEGRADED: board fold unavailable resolving {n['identifier']} — "
                      f"skipping card fail-closed", file=sys.stderr)
                degraded = True
                continue
            if present:
                status = "filed"
                bus_write(receipt_path, _receipt_body(n["identifier"], "filed", ""))
            else:
                status = None  # proven absent -> safe to (re)create below
        if status is None:
            # Phase 1: durable intent BEFORE the task write. If the intent
            # can't land, we don't create — the dedupe state must exist first.
            if not bus_write(receipt_path, _receipt_body(n["identifier"], "pending")):
                print(f"DEGRADED: intent receipt write failed for {n['identifier']} — "
                      f"not creating task", file=sys.stderr)
                degraded = True
                continue
            existing_receipts.add(receipt_name)
            # Phase 2: create. Engine contract: nonzero rc = nothing written.
            cp = subprocess.run(
                ["coord-engine", "later", TEAM, title[:160], "-w", "ash-answers",
                 "-s", f"Promoted from Ash Answers card {n['identifier']} ({n['url']})",
                 "--from", "ash"],
                capture_output=True, text=True, timeout=60)
            if cp.returncode != 0:
                bus_write(receipt_path, _receipt_body(n["identifier"], "pending"))
                print(f"DEGRADED: later failed for {n['identifier']}: {cp.stderr.strip()[-160:]}",
                      file=sys.stderr)
                degraded = True
                continue
            m = re.search(r"directive\s+(\S+)\s*->", cp.stdout)  # `directive <slug> -> @backlog`
            slug = m.group(1) if m else ""
            # Phase 3: mark filed. If THIS write fails the receipt stays
            # `pending` — the next run resolves via the board (finds the task,
            # adopts it); it can never blind-re-run `later`.
            if not bus_write(receipt_path, _receipt_body(n["identifier"], "filed", slug)):
                print(f"DEGRADED: filed-receipt write failed for {n['identifier']} "
                      f"(task {slug or title[:60]} created; next run adopts via board)",
                      file=sys.stderr)
                degraded = True
                continue
        else:
            print(f"retrying finalize for {n['identifier']} (task already filed: {slug or '?'})")
        try:
            cur = [l["id"] for l in n["labels"]["nodes"]] + [filed_id]
            gql("mutation($i:String!,$in:IssueUpdateInput!){issueUpdate(id:$i,input:$in){success}}",
                {"i": n["id"], "in": {"labelIds": cur, "stateId": IDS["states"]["done"]}})
            gql("mutation($in:CommentCreateInput!){commentCreate(input:$in){success}}",
                {"in": {"issueId": n["id"], "body": f"Filed to bus backlog: `{slug or title[:60]}` (workstream ash-answers)."}})
        except Exception as e:
            print(f"DEGRADED: Linear finalize failed for {n['identifier']} ({e}); "
                  f"receipt retained — next run retries finalize only", file=sys.stderr)
            degraded = True
            continue
        print(f"promoted {n['identifier']} -> bus task {slug or '(slug?)'}")
        n_done += 1
    print(f"promote: {n_done} card(s) filed" + (" [DEGRADED]" if degraded else ""))
    return 2 if degraded else 0


def main():
    if not KEY:
        print("LINEAR_API_KEY missing", file=sys.stderr); return 2
    if len(sys.argv) < 2:
        print("usage: answers_bridge.py {capture|list|promote} ...", file=sys.stderr); return 2
    cmd = sys.argv[1]
    if cmd == "capture":
        # capture --q Q --a A --by WHO --type factual|future|both [--id ID] [--ts TS]
        args = {}
        it = iter(sys.argv[2:])
        for k in it:
            if k.startswith("--"):
                args[k[2:]] = next(it, "")
        if not args.get("q") or not args.get("a"):
            print("capture needs --q and --a", file=sys.stderr); return 2
        return cmd_capture(args)
    if cmd == "list":
        return cmd_list({})
    if cmd == "promote":
        return cmd_promote({})
    print(f"unknown command {cmd}", file=sys.stderr); return 2


if __name__ == "__main__":
    sys.exit(main())
