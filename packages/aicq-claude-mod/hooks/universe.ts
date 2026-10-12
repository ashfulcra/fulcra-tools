// The owner's agent universe as a graph: machine/env → platform/runtime →
// identity/session, plus the meshes between them (workspaces, the V5 bus,
// cross-account mesh peers) and blocked-on-owner links. Pure: inputs are
// what the hooks module read (coord-engine JSON, V5 configs, workspace
// index, mesh peers, the desktop apps' session records); placement and
// session pins come from the owner.

export type Liveness = 'live' | 'idle' | 'stale' | 'lapsed' | 'unknown'

export type BlockedItem = { id: string; title: string; blockedOn: string; nextAction: string; priority: string; since: string }

export type UniverseNode = {
  key: string
  kind: 'agent' | 'peer'
  label: string
  machine: string
  platform: string
  liveness: Liveness
  lastSeen: string | null
  summary: string
  annotation: string
  open: Record<string, number>
  blocked: BlockedItem[]
  /** Blocked items waiting on the owner (user:<owner>). */
  blockedOnOwner: number
  meshes: string[]
  /** Fulcra user id for a cross-account peer. */
  userId: string | null
  /** The desktop session this identity runs in, when it is known exactly (never guessed). */
  session: NodeSession | null
}

/** A local Claude desktop Code session or Codex desktop thread, as the app itself records it. */
export type SessionRef = {
  app: 'claude' | 'codex'
  /** What the app's deep link opens: the desktop id (`local_…`) for Claude, the thread id for Codex. */
  id: string
  /** Another id the same session is known by: Claude Code's own CLI session id. */
  cliId: string | null
  title: string
  cwd: string
  /** Last activity, ISO, when the app records it. */
  at: string | null
  archived: boolean
  /** The desktop instance that records it: '' for the main app, else its `~/.claude-instances/<name>`. */
  instance: string
  /** The same session on the web, when the app records one (a Claude session's Remote Control page). */
  web: string | null
}

/** An owner's explicit choice of session for a node: `{ app, id }`, or `none` to suppress a binding's link. */
export type SessionPins = Record<string, { app: 'claude' | 'codex'; id: string } | 'none'>

/**
 * How to open the session a node runs in. `href` is the app link ('' when only a web link is known);
 * `web` opens the same session in a browser, which is the one every surface can click.
 */
export type NodeSession = { app: 'claude' | 'codex' | 'chatgpt' | 'other'; href: string; web: string | null; title: string; via: 'pinned' | 'v5 binding' | 'self-reported' }

/** An agent's own report of its links (`team/<team>/agent-links/<identity>.json`, schema agent-links/1). */
export type AgentLinks = { identity: string; aliases: string[]; platform: string; machine: string; updatedAt: string; app: string | null; web: string | null }

export type Mesh = { key: string; label: string; kind: 'workspace' | 'v5' | 'cross-account'; members: string[] }

export type Machine = { name: string; reconciledAt: string | null; stale: boolean | null }

export type Universe = {
  builtAt: string
  machines: Machine[]
  nodes: UniverseNode[]
  meshes: Mesh[]
  /** Sources that could not be read: shown, never silently empty. */
  degraded: string[]
}

export type Placement = Record<string, { machine?: string; platform?: string }>

type Json = Record<string, unknown>
const str = (v: unknown): string => (typeof v === 'string' ? v : '')
/** A decoded JSON record we can read fields from: `null`, arrays and scalars are valid JSON but not records. */
const isRecord = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v)

const PLATFORMS: [RegExp, string][] = [
  [/^claude-code|claude/i, 'Claude Code'],
  [/^codex|codex/i, 'Codex'],
  [/^openclaw|openclaw/i, 'OpenClaw'],
  [/hermes/i, 'Hermes'],
  [/chatgpt|gpt/i, 'ChatGPT'],
  [/grok/i, 'Grok'],
  [/coord-|collect-|maintainer|boss/i, 'Coord service'],
]

export function platformOf(name: string, hint = ''): string {
  for (const [re, label] of PLATFORMS) if (re.test(name) || re.test(hint)) return label
  return 'Unknown runtime'
}

/** Canonical machine names from reconcile hosts ("coord-reconcile:Ashs-MBP-Work" → "Ashs-MBP-Work"). */
export function machinesFrom(health: Json | null): Machine[] {
  const hosts = Array.isArray(health?.hosts) ? (health!.hosts as Json[]) : []
  const out: Machine[] = []
  for (const h of hosts) {
    const host = str(h.host)
    const m = /^coord-reconcile:(.+)$/.exec(host)
    if (!m) continue
    out.push({ name: m[1]!, reconciledAt: str(h.last_reconcile) || null, stale: typeof h.stale === 'boolean' ? h.stale : null })
  }
  return out
}

function canonicalMachine(raw: string, machines: readonly string[]): string {
  const t = raw.trim().replace(/\.localdomain$|\.local$/i, '')
  if (!t) return ''
  const hit = machines.find(m => m.toLowerCase() === t.toLowerCase() || m.toLowerCase().replace(/[^a-z0-9]/g, '') === t.toLowerCase().replace(/[^a-z0-9]/g, ''))
  if (hit) return hit
  if (/^mac$/i.test(t)) return 'Mac'
  // An instance id like "codex-desktop-singularity-home-infra" names its machine by prefix.
  const tokens = t.toLowerCase().split(/[^a-z0-9]+/).filter(x => x.length >= 6)
  const byToken = machines.find(m => tokens.some(tok => m.toLowerCase().replace(/[^a-z0-9]/g, '').startsWith(tok)))
  if (byToken) return byToken
  return t
}

/** Infers machine and platform from an identity's name and any free-text hints. */
export function placeIdentity(name: string, machines: readonly string[], hints: { machine?: string; text?: string }): { machine: string; platform: string } {
  const parts = name.split(':')
  let platform = platformOf(name, hints.text ?? '')
  let machine = hints.machine ? canonicalMachine(hints.machine, machines) : ''
  if (parts.length >= 3) {
    platform = platformOf(parts[0]!)
    // The middle segment is a machine only when it names one; otherwise it is a namespace ("openclaw:arc:main-comms").
    const maybe = canonicalMachine(parts[1]!, machines)
    if (machines.includes(maybe) || maybe === 'Mac') machine ||= maybe
  } else if (parts.length === 2) {
    platform = platformOf(parts[0]!)
    const maybe = canonicalMachine(parts[1]!, machines)
    if (machines.includes(maybe)) machine ||= maybe
  }
  const text = hints.text ?? ''
  if (!machine) {
    const named = machines.find(m => text.toLowerCase().includes(m.toLowerCase()))
    if (named) machine = named
    else if (/cloud/i.test(text)) machine = 'Cloud'
  }
  return { machine: machine || 'Unplaced', platform }
}

/** "- name — description" member lines from a workspace index.md. */
export function workspaceMembers(indexMd: string): { name: string; text: string }[] {
  const out: { name: string; text: string }[] = []
  let inMembers = false
  for (const line of indexMd.split('\n')) {
    if (/^##\s+Members/i.test(line)) inMembers = true
    else if (/^##\s+/.test(line)) inMembers = false
    else if (inMembers) {
      const m = /^-\s+([A-Za-z0-9_.:-]+)\s+—\s+(.*)$/.exec(line.trim())
      if (m) out.push({ name: m[1]!, text: m[2]! })
    }
  }
  return out
}

// Deep links into the desktop apps. Both apps register their scheme with macOS (Claude.app: `claude`,
// Codex.app: `codex`); these are the routes each app itself uses to open one session or thread.
export function deeplink(s: Pick<SessionRef, 'app' | 'id'>): string {
  return s.app === 'claude' ? `claude://claude.ai/epitaxy/${encodeURIComponent(s.id)}` : `codex://threads/${encodeURIComponent(s.id)}`
}

/**
 * Desktop session ids are `<kind>_<id>`: `local_<uuid>` for a session on this machine, other kinds for
 * sessions the local app shows but that run elsewhere. The app links every kind the same way.
 */
const DESKTOP_SESSION_ID = /^[a-z]+_[0-9A-Za-z-]{8,}$/

/** One Claude desktop session file (`claude-code-sessions/<account>/<org>/<id>.json`); null when it is not one. */
export function parseClaudeSession(text: string, instance = ''): SessionRef | null {
  let d: unknown
  try {
    d = JSON.parse(text)
  } catch {
    return null
  }
  if (!isRecord(d)) return null
  const id = str(d.sessionId)
  if (!DESKTOP_SESSION_ID.test(id)) return null
  const at = typeof d.lastActivityAt === 'number' ? new Date(d.lastActivityAt).toISOString() : null
  // The newest Remote Control id is the session's web page; it opens only while Remote Control is on.
  const bridges = Array.isArray(d.bridgeSessionIds) ? d.bridgeSessionIds.filter((b): b is string => typeof b === 'string' && /^session_[A-Za-z0-9]{8,}$/.test(b)) : []
  const web = bridges.length ? `https://claude.ai/code/${bridges[bridges.length - 1]}` : null
  return { app: 'claude', id, cliId: str(d.cliSessionId) || null, title: str(d.title), cwd: str(d.cwd), at, archived: d.isArchived === true, instance, web }
}

/**
 * Codex's `session_index.jsonl`: one row per rename, so the newest row per thread id wins. `bad` counts
 * nonblank lines that are not a thread row (torn, partly written or foreign), so a reader can say the
 * inventory is partial instead of presenting what survived as complete.
 */
export function parseCodexIndex(text: string): { sessions: SessionRef[]; bad: number } {
  const byId = new Map<string, SessionRef>()
  let bad = 0
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    let d: unknown
    try {
      d = JSON.parse(line)
    } catch {
      bad += 1
      continue
    }
    if (!isRecord(d)) {
      bad += 1
      continue
    }
    const id = str(d.id)
    if (!/^[0-9a-f-]{36}$/.test(id)) {
      bad += 1
      continue
    }
    const at = str(d.updated_at) || null
    const prev = byId.get(id)
    if (prev && prev.at && at && prev.at > at) continue
    byId.set(id, { app: 'codex', id, cliId: null, title: str(d.thread_name), cwd: '', at, archived: false, instance: '', web: null })
  }
  return { sessions: [...byId.values()], bad }
}

// The only link shapes AICQ ever opens or draws as clickable. Links come from agents' own reports, so
// anything else (a lookalike host, another scheme) must never become a one-click link.
const ALLOWED_LINKS: readonly RegExp[] = [
  /^claude:\/\/claude\.ai\/[A-Za-z0-9_\-./]+$/,
  /^https:\/\/claude\.ai\/[A-Za-z0-9_\-./]+$/,
  /^codex:\/\/threads\/[0-9a-f-]{36}(\?hostId=remote-control(%3A|:)[A-Za-z0-9_-]+)?$/,
  /^https:\/\/chatgpt\.com\/c\/[A-Za-z0-9-]+$/,
]

export function isAllowedLink(url: string): boolean {
  return url.length <= 2048 && !url.includes('..') && ALLOWED_LINKS.some(re => re.test(url))
}

/** One `agent-links/1` file; null when it is not one. Links outside the allowlist are dropped, not kept. */
export function parseAgentLinks(text: string): AgentLinks | null {
  let d: unknown
  try {
    d = JSON.parse(text)
  } catch {
    return null
  }
  if (!isRecord(d) || d.schema !== 'agent-links/1' || !str(d.identity)) return null
  const links = Array.isArray(d.links) ? d.links.filter(isRecord) : []
  const pick = (kind: string) => links.map(l => (str(l.kind) === kind ? str(l.url) : '')).find(u => u && isAllowedLink(u)) ?? null
  const aliases = Array.isArray(d.aliases) ? d.aliases.map(str).filter(Boolean) : []
  return { identity: str(d.identity), aliases, platform: str(d.platform), machine: str(d.machine), updatedAt: str(d.updated_at), app: pick('app'), web: pick('web') }
}

function appOf(url: string, platform: string): NodeSession['app'] {
  if (url.startsWith('codex:')) return 'codex'
  if (url.startsWith('claude:') || url.startsWith('https://claude.ai/')) return 'claude'
  if (url.startsWith('https://chatgpt.com/')) return 'chatgpt'
  return /codex/i.test(platform) ? 'codex' : /chatgpt/i.test(platform) ? 'chatgpt' : /claude/i.test(platform) ? 'claude' : 'other'
}

/**
 * The session a node opens, in precedence order: the owner's pin; then an exact match of its V5 binding's
 * session id against what this machine's apps record (current, and vouched for by the app); then the
 * agent's own report. No name or folder matching, so a link never opens the wrong session.
 */
export function sessionFor(key: string, boundSessionId: string, pins: SessionPins, sessions: readonly SessionRef[], reported: readonly AgentLinks[] = []): NodeSession | null {
  const pin = pins[key]
  if (pin === 'none') return null
  const name = key.replace(/^agent:/, '')
  const own = reported.find(l => l.identity === name || l.aliases.includes(name))
  if (pin) {
    const hit = sessions.find(s => s.app === pin.app && s.id === pin.id)
    return { app: pin.app, href: deeplink(pin), web: hit?.web ?? null, title: hit?.title ?? '', via: 'pinned' }
  }
  const id = boundSessionId.replace(/^local_/, '')
  const hit = id ? sessions.find(s => s.cliId === id || s.id === id || s.id === `local_${id}`) : undefined
  if (hit) return { app: hit.app, href: deeplink(hit), web: hit.web ?? own?.web ?? null, title: hit.title, via: 'v5 binding' }
  if (own && (own.app || own.web)) {
    return { app: appOf(own.app ?? own.web ?? '', own.platform), href: own.app ?? '', web: own.web, title: '', via: 'self-reported' }
  }
  return null
}

const REPORTED_PLATFORM: Record<string, string> = { 'claude-code': 'Claude Code', claude: 'Claude', codex: 'Codex', chatgpt: 'ChatGPT' }

/**
 * Where an agent says it runs, from its own links report: its declaration beats a guess from its name.
 * Empty fields mean "not said", so the guess stands for that part.
 */
export function reportedPlacement(l: AgentLinks | undefined, machines: readonly string[]): { machine: string; platform: string } {
  if (!l) return { machine: '', platform: '' }
  const m = l.machine.trim()
  const machine = !m ? '' : /^cloud$/i.test(m) ? 'Cloud' : (machines.find(x => x.toLowerCase() === m.toLowerCase()) ?? m)
  return { machine, platform: REPORTED_PLATFORM[l.platform.trim().toLowerCase()] ?? '' }
}

const LIVENESS: readonly Liveness[] = ['live', 'idle', 'stale', 'lapsed', 'unknown']

/** A readable label: drop the platform prefix and a machine segment, keep namespaces ("arc/main-comms"). */
export function labelOf(name: string, machines: readonly string[]): string {
  if (name.startsWith('coord-reconcile:')) return 'reconcile host'
  const parts = name.split(':')
  if (parts.length < 2) return name
  const rest = parts.slice(1)
  if (rest.length > 1 && (machines.some(m => m.toLowerCase() === rest[0]!.toLowerCase()) || /^mac$/i.test(rest[0]!))) rest.shift()
  return rest.join('/')
}

export type UniverseInputs = {
  builtAt: string
  /** `coord-engine agents <team> --json` (null when unreadable). */
  agents: Json[] | null
  /** `coord-engine board <team> --json`. */
  board: Json | null
  /** `coord-engine health <team> --json`. */
  health: Json | null
  /** V5 actor bindings found on this machine. */
  v5: { logicalAgentId: string; instanceId: string; workspaceId: string; sessionId: string }[]
  /** Local Claude desktop sessions and Codex threads, as the apps record them. */
  sessions: SessionRef[]
  sessionPins: SessionPins
  /** Agents' own link reports (`agent-links/1`). */
  agentLinks?: AgentLinks[]
  /** Workspaces: name → index.md text. */
  workspaces: Record<string, string>
  /** Cross-account mesh peers. */
  peers: { userId: string; label: string }[]
  owner: string
  thisAgent: { name: string; machine: string; platform: string }
  placement: Placement
  degraded: string[]
}

export function buildUniverse(i: UniverseInputs): Universe {
  const machines = machinesFrom(i.health)
  const names = machines.map(m => m.name)
  // A reconcile daemon's host is a machine by definition, even when health no longer lists it.
  for (const row of i.agents ?? []) {
    const m = /^coord-reconcile:(.+)$/.exec(str(row.agent))
    const host = m ? canonicalMachine(m[1]!, names) : ''
    if (host && !names.includes(host)) {
      machines.push({ name: host, reconciledAt: null, stale: null })
      names.push(host)
    }
  }
  if (i.thisAgent.machine && !names.includes(i.thisAgent.machine)) {
    machines.unshift({ name: i.thisAgent.machine, reconciledAt: null, stale: null })
    names.unshift(i.thisAgent.machine)
  }
  const blockedRows = Array.isArray(i.board?.blocked) ? (i.board!.blocked as Json[]) : []
  const waitingRows = Array.isArray(i.board?.waiting) ? (i.board!.waiting as Json[]) : []
  const blockedBy = new Map<string, BlockedItem[]>()
  for (const r of [...blockedRows, ...waitingRows.filter(w => str(w.blocked_on))]) {
    const item: BlockedItem = {
      id: str(r.id), title: str(r.title).replace(/-[0-9a-f]{8}$/, '').replace(/-/g, ' ').slice(0, 140),
      blockedOn: str(r.blocked_on), nextAction: str(r.next_action).slice(0, 300), priority: str(r.priority),
      since: str(r.timestamp) || str(r.mtime),
    }
    for (const who of new Set([str(r.owner), str(r.assignee)].filter(Boolean))) {
      blockedBy.set(who, [...(blockedBy.get(who) ?? []), item])
    }
  }

  const memberText = new Map<string, string>()
  const meshes: Mesh[] = []
  for (const [ws, md] of Object.entries(i.workspaces)) {
    const members = workspaceMembers(md)
    for (const m of members) memberText.set(m.name, m.text)
    meshes.push({ key: `ws:${ws}`, label: `Workspace ${ws}`, kind: 'workspace', members: members.map(m => `agent:${m.name}`) })
  }
  const v5ByWorkspace = new Map<string, string[]>()
  for (const a of i.v5) v5ByWorkspace.set(a.workspaceId, [...(v5ByWorkspace.get(a.workspaceId) ?? []), `agent:${a.logicalAgentId}`])
  for (const [wid, members] of v5ByWorkspace) meshes.push({ key: `v5:${wid}`, label: `V5 workspace ${wid.slice(0, 8)}`, kind: 'v5', members })

  const nodes = new Map<string, UniverseNode>()
  const add = (name: string, row: Json | null) => {
    if (name.startsWith('@') || name === i.owner || name === `user:${i.owner}` || name === 'human') return
    const key = `agent:${name}`
    const v5 = i.v5.find(a => a.logicalAgentId === name)
    const placed = name === i.thisAgent.name
      ? { machine: i.thisAgent.machine, platform: i.thisAgent.platform }
      : placeIdentity(name, names, { machine: v5?.instanceId, text: `${memberText.get(name) ?? ''} ${str(row?.summary)} ${v5?.instanceId ?? ''}` })
    const over = i.placement[key] ?? {}
    // Owner's correction, then the agent's own report, then the guess from its name.
    const said = reportedPlacement((i.agentLinks ?? []).find(l => l.identity === name || l.aliases.includes(name)), names)
    const live = str(row?.liveness)
    const blocked = blockedBy.get(name) ?? []
    nodes.set(key, {
      key, kind: 'agent', label: labelOf(name, names),
      machine: over.machine || said.machine || placed.machine, platform: over.platform || said.platform || placed.platform,
      liveness: (LIVENESS.includes(live as Liveness) ? live : str(row?.state) === 'lapsed' ? 'lapsed' : 'unknown') as Liveness,
      lastSeen: null, summary: str(row?.summary), annotation: str(row?.annotation),
      open: (row?.open && typeof row.open === 'object' ? row.open : {}) as Record<string, number>,
      blocked, blockedOnOwner: blocked.filter(b => b.blockedOn === `user:${i.owner}`).length,
      meshes: [], userId: null,
      session: sessionFor(key, v5?.sessionId ?? '', i.sessionPins, i.sessions, i.agentLinks ?? []),
    })
  }
  for (const row of i.agents ?? []) add(str(row.agent), row)
  for (const m of meshes) for (const k of m.members) if (!nodes.has(k)) add(k.replace(/^agent:/, ''), null)
  if (!nodes.has(`agent:${i.thisAgent.name}`)) add(i.thisAgent.name, null)
  for (const [who] of blockedBy) if (!nodes.has(`agent:${who}`)) add(who, null)

  // One agent, one entry: an agent's own report names the other identities it goes by (a v3 bus name and a V5
  // id, say), so those nodes fold into the reported identity instead of showing the same agent twice.
  for (const l of i.agentLinks ?? []) {
    const primary = nodes.get(`agent:${l.identity}`)
    if (!primary) continue
    for (const alias of l.aliases) {
      const aliasKey = `agent:${alias}`
      const dup = nodes.get(aliasKey)
      if (!dup || dup === primary) continue
      primary.blocked = [...primary.blocked, ...dup.blocked.filter(b => !primary.blocked.some(p => p.id === b.id))]
      primary.blockedOnOwner = primary.blocked.filter(b => b.blockedOn === `user:${i.owner}`).length
      for (const [k, c] of Object.entries(dup.open)) primary.open[k] = (primary.open[k] ?? 0) + c
      if (LIVENESS.indexOf(dup.liveness) < LIVENESS.indexOf(primary.liveness)) primary.liveness = dup.liveness
      primary.summary ||= dup.summary
      primary.annotation ||= dup.annotation
      primary.session ??= dup.session
      nodes.delete(aliasKey)
      for (const m of meshes) {
        if (!m.members.includes(aliasKey)) continue
        m.members = [...new Set(m.members.map(k => (k === aliasKey ? primary.key : k)))]
      }
    }
  }

  const peerMesh: Mesh = { key: 'mesh:cross-account', label: 'Cross-account mesh', kind: 'cross-account', members: [] }
  for (const p of i.peers) {
    const key = `peer:${p.userId}`
    peerMesh.members.push(key)
    const over = i.placement[key] ?? {}
    nodes.set(key, {
      key, kind: 'peer', label: p.label, machine: over.machine || 'Friends’ accounts', platform: over.platform || platformOf(p.label),
      liveness: 'unknown', lastSeen: null, summary: '', annotation: '', open: {}, blocked: [], blockedOnOwner: 0,
      meshes: [], userId: p.userId, session: null,
    })
  }
  if (peerMesh.members.length) meshes.push(peerMesh)
  for (const m of meshes) for (const k of m.members) nodes.get(k)?.meshes.push(m.key)

  return { builtAt: i.builtAt, machines, nodes: [...nodes.values()], meshes, degraded: i.degraded }
}

/** machine → platform → nodes, machines in a stable order (this machine first, unplaced last). */
export function grouped(u: Universe, firstMachine: string): { machine: string; platforms: { platform: string; nodes: UniverseNode[] }[] }[] {
  const byMachine = new Map<string, Map<string, UniverseNode[]>>()
  for (const n of u.nodes) {
    const pm = byMachine.get(n.machine) ?? new Map<string, UniverseNode[]>()
    pm.set(n.platform, [...(pm.get(n.platform) ?? []), n])
    byMachine.set(n.machine, pm)
  }
  const rank = (m: string) => (m === firstMachine ? 0 : m === 'Unplaced' ? 3 : m === 'Friends’ accounts' ? 2 : 1)
  const order = (l: Liveness) => LIVENESS.indexOf(l)
  return [...byMachine.entries()]
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]))
    .map(([machine, pm]) => ({
      machine,
      platforms: [...pm.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([platform, list]) => ({
        platform,
        nodes: [...list].sort((a, b) => b.blocked.length - a.blocked.length || order(a.liveness) - order(b.liveness) || a.label.localeCompare(b.label)),
      })),
    }))
}
