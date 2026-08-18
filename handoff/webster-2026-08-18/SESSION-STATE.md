# Session state — living handoff. Update after EVERY completed step.

## Identity / access
- Fulcra account: support@fulcradynamics.com, uid c936a72a-02a5-44de-8f30-40b2bb18f08d
- Ash's uid: d64bbe9b-4902-42e9-a607-7db51ebc6379
- Peer name on the mesh: "webster"
- Tycho (coord-boss) outbox: MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041
  (already shared to us as "mesh-webster")
- Webster outbox: MomentAnnotation/0939d4fb-861c-4321-9bcc-0ce84392478f (created 2026-08-18)
- Outgoing share: mesh-webster-outbox, datashare 63758b70-f72f-4015-9081-efd71b4dfa8a, to Ash's uid only
- Mesh cursor: Tycho channel read through 2026-08-18T10:25 ET; 0 events addressed to webster; no handled ids yet
- Protocol doc: raw.githubusercontent.com/ashfulcra/fulcra-tools/main/docs/coord/MESH-PEER-QUICKSTART.md
  (github.com HTML is proxy-blocked; raw works)

## Mesh event shape
{"v":1,"to":"coord-boss","to_user":"d64bbe9b-4902-42e9-a607-7db51ebc6379",
 "kind":"directive","pri":"P2","slug":"upstream-issue-<name>","body":"<text>"}
Write with: fulcra record "MomentAnnotation/<uuid>" --note='...'
If it says "No input provided", pipe the JSON as one line instead.
Poll Tycho's channel for "to":"webster". At-least-once, no server ack —
keep cursor = last-seen timestamp PLUS handled record ids.
Never --share-all. Never share to any uid but Ash's.

## Status
- [x] Mesh joined 2026-08-18 (outbox created, share to Ash only, Tycho read verified 320 recs, announce + share-live events sent)
- [x] fulcra-upstream-report skill written — skills/fulcra-upstream-report/ (this repo, main)
- [x] content-integration skill written — skills/fulcra-content-integration/ (renamed: fulcra-tools already had a different fulcra-content-review). v2: integral AND reader-executable; v2.2: prompts speak to the AGENT — value first, connect later via docs.fulcradynamics.com/agent-get-started.txt
- [~] 2 Recipes + 2 Blog CMS items copied to NEW drafts and improved — 4 passes done, record in content-review/2026-08-18/. OPEN: blog how-to blocks ruled bad (URL/command dumps at the reader) — rework pending; loops draft needs a featured image
- [x] /agents page built + rebuilt from canonical doc, passing QA
- [x] developer pages content pass, passing QA
- [x] docs-site link repoint (3 links left: /platform/context-app, /product/start-for-free)

## Session 2026-08-18 addenda
- THE PRIOR WORK LIVES IN FulcraBot/webflow-bot BRANCH claude/webster-home-v2-styles-4pcuom
  (46 commits ahead of main; REVIEW-LIST.md, CHANGELOG-SITE.md, canonical/, upstream/,
  v2-payloads/, BACKLOG.md B7-B10, ops gates, qa-rig scripts). A session seeded on an
  ashfulcra repo CANNOT attach FulcraBot repos (cross-owner unsupported) — to touch that
  repo, START the session on it. This repo (ashfulcra/Fulcra-Webflow) holds this session's
  recreated state; RECONCILE the two — webflow-bot 4pcuom is authoritative for anything older.
- CMS draft ids: dinner 6a846de8dd26f180acf5a1c5, fitness 6a846e2b6cee41517465f5ed,
  check-loop 6a846e690aca93d90c9ff8ef, loops 6a846e690aca93d90c9ff8f1 (all isDraft, unpublished; originals untouched)
- CLI cannot install in the fulcra-tools-seeded cloud container (PyPI blocked). Fulcra access
  = Webster MCP server, or direct API with ops/device_auth.py credentials (device flow, Ash approves).
- .claude/settings.json here has only the ten fulcra verbs; the four non-fulcra base entries
  from the original 14 are in webflow-bot 4pcuom — copy them over when readable.

## Corrections that will cause errors if missed
- The 5 issues in upstream/ are ALREADY FILED. Historical. Do not re-send.
- Production = the 2026-08-07 build. /agents, /developers, /platform/cli,
  /platform/python-sdk are 404 in production until Ash publishes.
- Two live Fulcra auth sessions still need revoking (Ash).

## Hard-won rules
- Verify the artefact a reader RECEIVES: installed binary, downloaded wheel,
  served spec, rendered page. Never the README, the repo, or the docs page.
  Two wrong claims shipped this month from breaking this.
- Never evidence: unauthenticated endpoint probes (real and fake paths both
  401 — auth precedes routing); raw-HTML string matching (strip tags first).
- Every content/structure change → CHANGELOG-SITE.md; node ops/changelog-gate.js
  must pass before any publish. Also node ops/publish-gate.js. Staging only.
- QA everything touched: qa-rig/visualcheck.js (desktop AND --mobile) + linkcheck.js
- Container rolls back often. Push after every step. Recover with
  git fetch origin <branch> && git reset --hard origin/<branch>; rebase if rejected.
