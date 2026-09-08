#!/usr/bin/env python3
"""Answers scratchpad bridge — bus-backed, Linear-viewed.

Directions (each single-purpose, to avoid bidirectional state-sync bugs):
  capture   bus->Linear (one way): record an answer as a durable bus shard AND a
            Linear card in the configured Answers project. Idempotent by id.
  promote   Linear->bus (one way, operator-triggered): cards the operator labeled `promote`
            become a bus backlog task via `coord-engine later`; the bus slug is
            commented back and the card gets `filed` + moved to Done.
  list      read-only: open answer cards, for terminal reference.

Check-off is Linear-only (mark Done) — deliberately NOT synced back, so there is
no fragile two-way state channel.

Account IDs from --config / ANSWERS_LINEAR_CONFIG; credentials from
LINEAR_API_KEY or an explicitly selected ANSWERS_LINEAR_ENV file. No setup I/O
happens at import time. Keep all account configuration outside version control.
Exit: 0 ok, 2 degraded (stderr says which step).
"""
import argparse, hashlib, json, os, re, ssl, subprocess, sys, urllib.request

API = "https://api.linear.app/graphql"
KEY = ""
IDS = {}
TEAM = ""
SENDER = "user"
WORKSTREAM = "answers"
PROMOTION_SOURCE = "Answers"


class SetupError(ValueError):
    """An actionable local setup failure, with no private values in its text."""


def _load_key():
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if key:
        return key
    path = os.environ.get("ANSWERS_LINEAR_ENV", "").strip()
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as source:
                for line in source:
                    name, sep, value = line.strip().partition("=")
                    if sep and name.strip() == "LINEAR_API_KEY":
                        key = value.strip()
                        if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
                            key = key[1:-1].strip()
                        if key:
                            return key
        except (OSError, UnicodeError):
            raise SetupError("Cannot read ANSWERS_LINEAR_ENV; select a readable UTF-8 credential file.") from None
    raise SetupError("Set LINEAR_API_KEY or select a credential file with ANSWERS_LINEAR_ENV.")


def configure(config_path=None):
    """Load only explicitly selected local files, before any external call."""
    global IDS, KEY, TEAM, SENDER, WORKSTREAM, PROMOTION_SOURCE
    path = config_path or os.environ.get("ANSWERS_LINEAR_CONFIG", "").strip()
    if not path:
        raise SetupError("Select an account configuration with --config or ANSWERS_LINEAR_CONFIG; see answers-linear-ids.example.json.")
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as source:
            ids = json.load(source)
    except (OSError, UnicodeError, ValueError):
        raise SetupError("Cannot read configuration: select a readable UTF-8 JSON object with --config or ANSWERS_LINEAR_CONFIG.") from None
    required = {
        None: ("project_id", "team_id"),
        "states": ("open", "done"),
        "labels": ("qa-answer", "type:factual", "type:future-work", "type:both", "promote", "filed"),
    }
    if not isinstance(ids, dict):
        raise SetupError("Account configuration must be a JSON object; see the synthetic example.")
    for section, keys in required.items():
        values = ids if section is None else ids.get(section)
        if not isinstance(values, dict) or any(
                not isinstance(values.get(k), str) or not values[k].strip() for k in keys):
            raise SetupError("Account configuration needs nonempty project/team IDs, open/done states, and all six label IDs; see the synthetic example.")
    sender, workstream = ids.get("sender", "user"), ids.get("workstream", "answers")
    promotion_source = ids.get("promotion_source", "Answers")
    team = os.environ.get("COORD_TEAM", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", team):
        raise SetupError("Set COORD_TEAM to your bus team name (letters, digits, dots, underscores or hyphens).")
    if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v)
           for v in (sender, workstream)):
        raise SetupError("Configuration sender and workstream must be names using letters, digits, dots, underscores or hyphens.")
    if not isinstance(promotion_source, str) or not promotion_source.strip() or any(
            ord(c) < 32 for c in promotion_source):
        raise SetupError("Configuration promotion_source must be nonempty single-line text.")
    key = _load_key()
    IDS, KEY, TEAM, SENDER, WORKSTREAM = ids, key, team, sender, workstream
    PROMOTION_SOURCE = promotion_source


def gql(query, variables=None):
    req = urllib.request.Request(
        API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": KEY})
    ctx = ssl.create_default_context()
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

    Idempotent WITHOUT local state, by the engine's own delivery contract: a
    directive's path carries a payload hash over (title, summary, next,
    assignee) — never time — so identical payloads converge on ONE shard;
    re-delivery prints `already delivered` and returns 0, with absence
    confirmed store-side by the engine's listing (cli._write_directive). This
    tool keeps the payload bit-stable per card (fixed title, summary keyed by
    the card's immutable Linear identifier + URL, fixed assignee), so simply
    re-running `later` for every still-unfiled card is safe across EVERY
    partial-failure window: it either creates or dedupes. The card's `filed`
    label is the only completion marker; a failed Linear finalize retries the
    whole idempotent sequence on the next pass. Known edge (accepted +
    intentional): editing a card's title between a failure and its retry
    changes the message identity and files a task for the NEW title — the
    title IS part of the payload by engine contract."""
    filed_id = IDS["labels"]["filed"]
    n_done = 0
    degraded = False
    for n in _project_issues():
        names = {l["name"] for l in n["labels"]["nodes"]}
        if "promote" not in names or "filed" in names:
            continue
        title = n["title"]
        cp = subprocess.run(
            ["coord-engine", "later", TEAM, title[:160], "-w", WORKSTREAM,
             "-s", f"Promoted from {PROMOTION_SOURCE} card {n['identifier']} ({n['url']})",
             "--from", SENDER],
            capture_output=True, text=True, timeout=60)
        if cp.returncode != 0:
            print(f"DEGRADED: later failed for {n['identifier']}: {cp.stderr.strip()[-160:]}",
                  file=sys.stderr)
            degraded = True
            continue
        # Two success shapes: `directive <slug> -> @backlog` (created) and
        # `directive <slug> already delivered` (deduped retry). Both are safe.
        m = re.search(r"^directive\s+(\S+?)(?:\s*->|\s+already delivered)",
                      cp.stdout, re.M)
        slug = m.group(1) if m else ""
        try:
            cur = [l["id"] for l in n["labels"]["nodes"]] + [filed_id]
            gql("mutation($i:String!,$in:IssueUpdateInput!){issueUpdate(id:$i,input:$in){success}}",
                {"i": n["id"], "in": {"labelIds": cur, "stateId": IDS["states"]["done"]}})
            gql("mutation($in:CommentCreateInput!){commentCreate(input:$in){success}}",
                {"in": {"issueId": n["id"], "body": f"Filed to bus backlog: `{slug or title[:60]}` (workstream {WORKSTREAM})."}})
        except Exception as e:
            print(f"DEGRADED: Linear finalize failed for {n['identifier']} ({e}); "
                  f"card stays unfiled — next pass re-runs the idempotent file",
                  file=sys.stderr)
            degraded = True
            continue
        print(f"promoted {n['identifier']} -> bus task {slug or '(slug?)'}")
        n_done += 1
    print(f"promote: {n_done} card(s) filed" + (" [DEGRADED]" if degraded else ""))
    return 2 if degraded else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="local account JSON path (or ANSWERS_LINEAR_CONFIG)")
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="record an answer on the bus and in Linear")
    capture.add_argument("--q", required=True)
    capture.add_argument("--a", required=True)
    capture.add_argument("--by", default="?")
    capture.add_argument("--type", choices=tuple(TYPE_LABEL), default="factual")
    capture.add_argument("--id")
    capture.add_argument("--ts", default="")
    commands.add_parser("list", help="list open answer cards")
    commands.add_parser("promote", help="file promoted cards into the bus backlog")
    commands.add_parser("check-config", help="validate local setup without contacting either service")
    args = vars(parser.parse_args(argv))
    if args["command"] == "capture" and (not args["q"].strip() or not args["a"].strip()):
        parser.error("capture needs nonempty --q and --a")
    try:
        configure(args.pop("config"))
    except SetupError as exc:
        print(f"SETUP: {exc}", file=sys.stderr)
        return 2
    command = args.pop("command")
    if command == "check-config":
        print("Configuration valid locally; no services contacted.")
        return 0
    return {"capture": cmd_capture, "list": cmd_list, "promote": cmd_promote}[command](args)


if __name__ == "__main__":
    sys.exit(main())
