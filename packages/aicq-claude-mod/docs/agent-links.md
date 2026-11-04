# Agent links (`agent-links/1`)

An agent's **links** are the addresses that open the session it runs in: an
**app** link that opens the desktop or mobile app, and, where the platform has
one, a **web** link that opens the same session in a browser. AICQ uses them so
that one place (the `/aicq` "Waiting on you" view and the universe map) can jump
straight to any of the owner's agents, whichever platform or machine it runs on.

Status: interim convention, 2026-10-10. It is a file in the team store so that
every agent (coord v3, Coord v5, any platform) can publish today. The intent is
for Coord v5 to carry the same `links` array on join, role-claim and presence
events; when it does, the file becomes a fallback.

## Where

One file per agent identity in the owner's Fulcra file store:

```
team/<team>/agent-links/<identity>.json
```

`<identity>` is the name other agents address you by: your `FULCRA_COORD_AGENT`
on the coord bus, or your Coord v5 `logical_agent_id`. If you have both, use one
for the file name and list the other in `aliases`.

## Shape

This example is deliberately synthetic; replace its identities, machine and links with your own observed values.

```json
{
  "schema": "agent-links/1",
  "identity": "example-assistant",
  "aliases": ["example-maintainer"],
  "platform": "claude-code",
  "machine": "ExampleLaptop",
  "updated_at": "2026-10-10T21:00:00Z",
  "links": [
    { "kind": "app", "url": "claude://claude.ai/epitaxy/local_00000000-0000-4000-8000-0000000000e2" },
    { "kind": "web", "url": "https://claude.ai/code/session_Example01" }
  ]
}
```

- `platform`: `claude-code`, `claude`, `codex`, `chatgpt`, or `other`.
- `machine`: your host name, or `cloud`.
- `links`: zero or more `{kind, url}`; `kind` is `app` or `web`. List only links
  you have actually observed. A link you cannot determine is left out, never
  guessed.
- `updated_at`: when you last checked them. Refresh the file whenever a link
  changes (a new session, Remote Control turned on or off) and at session start.

## Finding your own links

| Platform | app link | web link |
|---|---|---|
| Claude Code, desktop app | `claude://claude.ai/epitaxy/<desktop session id>`: the `link` field of the desktop session tool's `get_session("self")`, or `sessionId` in your record under `~/Library/Application Support/Claude/claude-code-sessions/*/*/` | `https://claude.ai/code/<id>`, where `<id>` is the last entry of `bridgeSessionIds` in that same record. It only works while Remote Control is on for the session. |
| Claude Code, terminal only | none | the Remote Control URL that `/remote-control` prints, if it is on |
| Claude (chat) | `claude://` + the chat URL's path | the `https://claude.ai/...` URL of the conversation |
| Codex desktop | `codex://threads/<thread id>`; a Remote Control thread adds `?hostId=remote-control:<env>` exactly as Codex's own "copy link" gives it | none known |
| ChatGPT | none | `https://chatgpt.com/c/<conversation id>`. ChatGPT cannot see its own conversation id, so ask your human to paste the conversation URL once. |

## Rules readers apply

- **Own account only.** These files live in the owner's store and describe the
  owner's own agents. A link to another person's session does not open for the
  owner anyway, so cross-account peers never send one and readers drop any that
  arrive.
- **Allowlist.** A reader opens only these shapes and ignores everything else:
  `claude://claude.ai/…`, `https://claude.ai/…`, `codex://threads/<uuid>` with an
  optional `hostId` parameter, and `https://chatgpt.com/c/<id>`. A file is
  written by an agent, so it must never turn into a one-click link to anything
  else.
- **Precedence.** An owner's pin wins, including a saved choice on a folded alias
  (the canonical identity's choice wins if both exist), then what the local desktop apps record
  (they vouch for it, and it is current), then the agent's own file.
- **Surface.** The terminal and desktop panes offer a web link when one exists;
  a local macOS host can open an app link through the OS. The AICQ mobile pane
  is not implemented; it currently directs the owner to ask their agent.

## Writing it

```bash
fulcra file upload links.json team/<team>/agent-links/<identity>.json   # re-uploading replaces it
```

Then read it back once (`fulcra file download team/<team>/agent-links/<identity>.json -`)
before saying it is published.
