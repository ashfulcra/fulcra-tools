// The owner's agent universe as a graph: machine/env → platform/runtime →
// identity/session, plus the meshes between them (workspaces, the V5 bus,
// cross-account mesh peers) and blocked-on-owner links. Pure: inputs are
// what the hooks module read (coord-engine JSON, V5 configs, workspace
// index, mesh peers); placement overrides come from the owner.

export type Liveness = 'live' | 'idle' | 'stale' | 'lapsed' | 'unknown'

export type BlockedItem = { id: string; title: string; blockedOn: string; nextAction: string; priority: string }

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
}

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
  v5: { logicalAgentId: string; instanceId: string; workspaceId: string }[]
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
    const live = str(row?.liveness)
    const blocked = blockedBy.get(name) ?? []
    nodes.set(key, {
      key, kind: 'agent', label: labelOf(name, names),
      machine: over.machine || placed.machine, platform: over.platform || placed.platform,
      liveness: (LIVENESS.includes(live as Liveness) ? live : str(row?.state) === 'lapsed' ? 'lapsed' : 'unknown') as Liveness,
      lastSeen: null, summary: str(row?.summary), annotation: str(row?.annotation),
      open: (row?.open && typeof row.open === 'object' ? row.open : {}) as Record<string, number>,
      blocked, blockedOnOwner: blocked.filter(b => b.blockedOn === `user:${i.owner}`).length,
      meshes: [], userId: null,
    })
  }
  for (const row of i.agents ?? []) add(str(row.agent), row)
  for (const m of meshes) for (const k of m.members) if (!nodes.has(k)) add(k.replace(/^agent:/, ''), null)
  if (!nodes.has(`agent:${i.thisAgent.name}`)) add(i.thisAgent.name, null)
  for (const [who] of blockedBy) if (!nodes.has(`agent:${who}`)) add(who, null)

  const peerMesh: Mesh = { key: 'mesh:cross-account', label: 'Cross-account mesh', kind: 'cross-account', members: [] }
  for (const p of i.peers) {
    const key = `peer:${p.userId}`
    peerMesh.members.push(key)
    const over = i.placement[key] ?? {}
    nodes.set(key, {
      key, kind: 'peer', label: p.label, machine: over.machine || 'Friends’ accounts', platform: over.platform || platformOf(p.label),
      liveness: 'unknown', lastSeen: null, summary: '', annotation: '', open: {}, blocked: [], blockedOnOwner: 0,
      meshes: [], userId: p.userId,
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
