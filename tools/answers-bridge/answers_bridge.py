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
    """Read a bus file; None if absent/unreadable (degraded reads as absent —
    callers must only use this where a false-absent is safe)."""
    cp = subprocess.run(["fulcra-api", "file", "download", path, "-"],
                        capture_output=True, text=True, timeout=60)
    return cp.stdout if cp.returncode == 0 else None


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


def cmd_promote(a):
    """Cards labeled `promote` and not yet `filed` -> bus backlog task, link back.

    Retry-safe across the bus/Linear boundary: a durable receipt shard
    (team/<team>/answers/_promotions/<card>.md) is written the moment the bus
    task exists, BEFORE the Linear finalize. If finalize fails, the next run
    reads the receipt, skips task creation, and only retries the finalize — a
    card can never file the same task twice. The receipt is keyed by the
    card's immutable Linear identifier (deterministic promotion key)."""
    promote_id = IDS["labels"]["promote"]; filed_id = IDS["labels"]["filed"]
    n_done = 0
    degraded = False
    for n in _project_issues():
        names = {l["name"] for l in n["labels"]["nodes"]}
        if "promote" not in names or "filed" in names:
            continue
        title = n["title"]
        receipt_path = f"team/{TEAM}/answers/_promotions/{n['identifier']}.md"
        receipt = bus_read(receipt_path)
        if receipt is not None:
            m = re.search(r"^slug:\s*(\S+)", receipt, re.M)
            slug = m.group(1) if m else ""
            print(f"retrying finalize for {n['identifier']} (task already filed: {slug or '?'})")
        else:
            cp = subprocess.run(
                ["coord-engine", "later", TEAM, title[:160], "-w", "ash-answers",
                 "-s", f"Promoted from Ash Answers card {n['identifier']} ({n['url']})",
                 "--from", "ash"],
                capture_output=True, text=True, timeout=60)
            if cp.returncode != 0:
                print(f"DEGRADED: later failed for {n['identifier']}: {cp.stderr.strip()[-160:]}", file=sys.stderr)
                degraded = True
                continue
            slug = ""
            m = re.search(r"directive\s+(\S+)\s*->", cp.stdout)  # `directive <slug> -> @backlog`
            if m: slug = m.group(1)
            if not bus_write(receipt_path,
                             f"---\ntype: PromotionReceipt\ncard: {n['identifier']}\n"
                             f"slug: {slug}\n---\n"):
                # Task exists but the receipt didn't land: FAIL LOUD and do NOT
                # finalize — finalizing would hide that a retry after this point
                # could double-file. Operator/next run resolves via the printed slug.
                print(f"DEGRADED: receipt write failed for {n['identifier']} after task "
                      f"{slug or title[:60]} was filed — NOT finalizing; resolve receipt first",
                      file=sys.stderr)
                degraded = True
                continue
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
