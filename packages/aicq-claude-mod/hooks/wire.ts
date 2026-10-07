// Pure AICQ wire logic: reads three formats into one message shape, writes
// the format each contact speaks. No `$` here, so it is unit-testable.
//
//   connect-our-agents/1  v1 channel Event/<uuid>, fields at the record's top level
//                         (agent-skills PR #227; Brandon's ChatGPT plugin wire)
//   legacy mesh           MomentAnnotation/<uuid>, note = {v,mid,to,to_user,kind,pri,slug,body}
//   fulcra.workspaces/1   MomentAnnotation/<uuid>, note = {coord:{protocol,message_id,...}}
//
// Work state has no schema field yet: it rides a leading `[state:<state>]`
// line in the body, which any reader shows as text.

import type { AicqMessage, ArtifactRef, WorkState } from '../types'

export const V1_PROTOCOL = 'connect-our-agents/1'
export const WS_PROTOCOL = 'fulcra.workspaces/1'

export type Wire = 'v1' | 'mesh'

/** A connected agent across accounts: their channels to me, mine to them. */
export type Peer = {
  userId: string
  /** The person, from any share they made (falls back to an id prefix). */
  name: string
  inbound: { channel: string; wire: Wire }[]
  outbound: { channel: string; wire: Wire; created: string }[]
}

export type Cursor = { at: string | null; seen: string[] }

export const EMPTY_CURSOR: Cursor = { at: null, seen: [] }

const SEEN_CAP = 800

type Json = Record<string, unknown>

const str = (v: unknown): string => (typeof v === 'string' ? v : '')

function parseObject(text: string): Json | null {
  try {
    const v: unknown = JSON.parse(text)
    return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null
  } catch {
    return null
  }
}

/** JSONL from `fulcra get-records` / `share list-*`; bad lines counted. */
export function parseJsonl(stdout: string): { rows: Json[]; bad: number } {
  const rows: Json[] = []
  let bad = 0
  for (const line of stdout.split('\n')) {
    const t = line.trim()
    if (!t) continue
    const o = parseObject(t)
    if (o) rows.push(o)
    else bad += 1
  }
  return { rows, bad }
}

export function wireOf(channel: string): Wire | null {
  if (channel.startsWith('Event/')) return 'v1'
  if (channel.startsWith('MomentAnnotation/')) return 'mesh'
  return null
}

/** Peers from `share list-incoming` (their channels) + `share list-outgoing` (mine to them). */
export function parsePeers(incoming: string, outgoing: string, me: string): Peer[] {
  const peers = new Map<string, Peer>()
  // A person's name can sit on any of their shares (an all-data share, a group), not only the channel's.
  const names = new Map<string, string>()
  for (const row of parseJsonl(incoming).rows) {
    const id = str(row.sharing_fulcra_userid)
    const n = str(row.sharing_fulcra_user_name)
    if (id && n && n !== id) names.set(id, n)
  }
  const peer = (userId: string, name: string): Peer => {
    name = names.get(userId) ?? name
    let p = peers.get(userId)
    if (!p) {
      p = { userId, name: name && name !== userId ? name : userId.slice(0, 8), inbound: [], outbound: [] }
      peers.set(userId, p)
    } else if (p.name === userId.slice(0, 8) && name && name !== userId) p.name = name
    return p
  }
  for (const row of parseJsonl(incoming).rows) {
    const userId = str(row.sharing_fulcra_userid)
    if (!userId || userId === me) continue
    for (const t of Array.isArray(row.fulcra_data_types) ? row.fulcra_data_types : []) {
      const wire = typeof t === 'string' ? wireOf(t) : null
      if (!wire) continue
      const p = peer(userId, str(row.sharing_fulcra_user_name))
      if (!p.inbound.some(c => c.channel === t)) p.inbound.push({ channel: t as string, wire })
    }
  }
  for (const row of parseJsonl(outgoing).rows) {
    const perms = Array.isArray(row.permissions) ? row.permissions : []
    const types = Array.isArray(row.fulcra_data_types) ? row.fulcra_data_types : []
    for (const perm of perms) {
      const userId = perm && typeof perm === 'object' ? str((perm as Json).allowed_fulcra_userid) : ''
      if (!userId || userId === me) continue
      for (const t of types) {
        const wire = typeof t === 'string' ? wireOf(t) : null
        if (!wire) continue
        const p = peer(userId, '')
        if (!p.outbound.some(c => c.channel === t)) p.outbound.push({ channel: t as string, wire, created: str(row.created_at) })
      }
    }
  }
  // Only agents with a channel to me are contacts; an outbound-only share is a pending invite.
  return [...peers.values()].filter(p => p.inbound.length > 0)
}

/** The channel to write to a peer: prefer v1 if they speak it, newest share wins. */
export function outboxFor(p: Peer, fallback: string): { channel: string; wire: Wire } | null {
  const speaksV1 = p.inbound.some(c => c.wire === 'v1')
  const pick = (wire: Wire) => [...p.outbound].filter(c => c.wire === wire).sort((a, b) => (a.created < b.created ? 1 : -1))[0]
  const chosen = (speaksV1 ? pick('v1') : undefined) ?? pick('mesh') ?? pick('v1')
  if (chosen) return { channel: chosen.channel, wire: chosen.wire }
  const w = wireOf(fallback)
  return w ? { channel: fallback, wire: w } : null
}

/** `Message channel: MomentAnnotation/<uuid>` from a workspace index.md. */
export function parseWorkspaceChannel(indexMd: string): string | null {
  const m = /Message channel:\s*(MomentAnnotation\/[0-9a-f-]{36})/i.exec(indexMd)
  return m?.[1] ?? null
}

const STATES: readonly WorkState[] = ['waiting', 'working', 'decision-needed', 'prepared-for-approval', 'completed', 'paused', 'unable', 'needs-reply', 'fyi']

/** Splits a leading `[state:x]` (and optional `[purpose:...]`) line off a body. */
export function splitMarkers(body: string): { body: string; state: WorkState | null; purpose: string | null } {
  let rest = body
  let state: WorkState | null = null
  let purpose: string | null = null
  for (let i = 0; i < 2; i += 1) {
    const m = /^\[(state|purpose):([^\]\n]*)\]\s*\n?/.exec(rest)
    if (!m) break
    const v = m[2]!.trim()
    if (m[1] === 'state') {
      const norm = v.toLowerCase().replace(/[_\s]+/g, '-') as WorkState
      if (STATES.includes(norm)) state = norm
    } else purpose = v
    rest = rest.slice(m[0].length)
  }
  return { body: rest, state, purpose }
}

export function withMarkers(body: string, state: WorkState | null, purpose: string | null): string {
  return `${state ? `[state:${state}]\n` : ''}${purpose ? `[purpose:${purpose}]\n` : ''}${body}`
}

function normKind(kind: string, topic: string): string {
  if (/-(ack)$/.test(topic)) return 'ack'
  if (/-(retracted)$/.test(topic)) return 'retraction'
  switch (kind) {
    case 'directive': return 'message'
    case 'response': return 'reply'
    case '': return 'message'
    default: return kind
  }
}

function artifactsOf(v: unknown): ArtifactRef[] {
  if (!Array.isArray(v)) return []
  return v.flatMap(a => {
    if (!a || typeof a !== 'object') return []
    const o = a as Json
    const uri = str(o.path) || str(o.uri)
    return uri ? [{ uri, name: uri.split('/').pop() ?? uri, version: str(o.version) || null, sha256: str(o.sha256) || null }] : []
  })
}

export type RowContext = {
  source: 'mesh' | 'workspace'
  channel: string
  direction: 'in' | 'out'
  /** The other party (peer name, or workspace sender resolved below). */
  contact: string
  contactUserId: string | null
  workspace: string | null
  me: string
  agentName: string
}

export type Parsed = { message: AicqMessage } | { skip: 'malformed' | 'not-for-me' | 'presence' }

/** Any of the three formats → one message, or a reason to skip it. */
export function parseRow(row: Json, ctx: RowContext): Parsed {
  const at = str(row.start_time) || str(row.recorded_at)
  const noteObj = typeof row.note === 'string' ? parseObject(row.note) : null
  const v1 = str(row.protocol) === V1_PROTOCOL ? row
    : row.data && typeof row.data === 'object' && str((row.data as Json).protocol) === V1_PROTOCOL ? (row.data as Json)
      : noteObj && str(noteObj.protocol) === V1_PROTOCOL ? noteObj : null
  const coord = noteObj && noteObj.coord && typeof noteObj.coord === 'object' ? (noteObj.coord as Json) : null

  let id = ''
  let sender = ''
  let recipients: string[] = []
  let kind = ''
  let topic = ''
  let rawBody = ''
  let inReplyTo: string | null = null
  let artifacts: ArtifactRef[] = []
  let toUser = ''
  let sentAt = at

  if (v1) {
    id = str(v1.message_id)
    sender = str(v1.sender)
    recipients = Array.isArray(v1.recipients) ? v1.recipients.map(str).filter(Boolean) : []
    kind = str(v1.kind)
    topic = str(v1.topic)
    rawBody = str(v1.body)
    inReplyTo = str(v1.in_reply_to) || null
    artifacts = artifactsOf(v1.artifacts)
  } else if (coord && str(coord.protocol) === WS_PROTOCOL) {
    id = str(coord.message_id)
    sender = str(coord.sender)
    recipients = Array.isArray(coord.recipients) ? coord.recipients.map(str).filter(Boolean) : []
    kind = str(coord.kind)
    topic = str(coord.topic)
    rawBody = str(coord.body)
    inReplyTo = str(coord.in_reply_to) || null
    artifacts = artifactsOf(coord.artifacts)
    sentAt = str(coord.sent_at) || at
  } else if (noteObj && str(noteObj.mid)) {
    id = str(noteObj.mid)
    sender = ctx.direction === 'out' ? 'me' : ctx.contact
    recipients = str(noteObj.to) ? [str(noteObj.to)] : []
    kind = str(noteObj.kind)
    topic = str(noteObj.slug)
    rawBody = str(noteObj.body)
    toUser = str(noteObj.to_user)
  } else {
    return { skip: 'malformed' }
  }
  if (!id) return { skip: 'malformed' }
  if (kind === 'heartbeat') return { skip: 'presence' }

  // Addressing.
  let direction = ctx.direction
  let contact = ctx.contact
  if (ctx.source === 'workspace') {
    const mine = !!ctx.agentName && sender === ctx.agentName
    const forMe = !ctx.agentName || recipients.length === 0 || recipients.some(r => r === ctx.agentName || r === 'all' || r === '*')
    if (!mine && !forMe) return { skip: 'not-for-me' }
    direction = mine ? 'out' : 'in'
    contact = mine ? (recipients.find(r => r !== 'all' && r !== '*') ?? 'all') : sender || 'unknown'
  } else if (direction === 'in') {
    if (toUser && toUser !== ctx.me) return { skip: 'not-for-me' }
  }

  const { body, state, purpose } = splitMarkers(rawBody)
  return {
    message: {
      id, source: ctx.source, direction, channel: ctx.channel, contact,
      contactUserId: ctx.contactUserId, workspace: ctx.workspace,
      to: recipients.join(','), kind: normKind(kind, topic), topic, body, at: sentAt,
      inReplyTo, state, purpose, artifacts,
    },
  }
}

/** Drops ids the cursor has seen; returns the advanced cursor. */
export function advance(cursor: Cursor, batch: readonly AicqMessage[]): { fresh: AicqMessage[]; cursor: Cursor } {
  const seen = new Set(cursor.seen)
  const fresh: AicqMessage[] = []
  let at = cursor.at
  for (const m of batch) {
    if (seen.has(m.id)) continue
    seen.add(m.id)
    fresh.push(m)
    if (m.at && (!at || m.at > at)) at = m.at
  }
  return { fresh, cursor: { at, seen: [...seen].slice(-SEEN_CAP) } }
}

/** Only substantive inbound messages may start a turn (no reply loops). */
export function isWakeWorthy(m: AicqMessage): boolean {
  if (m.direction !== 'in') return false
  if (m.kind === 'ack' || m.kind === 'retraction' || m.kind === 'receipt') return false
  return !/-(ack|retracted)$/.test(m.topic)
}

/** Read window start: the cursor minus overlap, else a first-run lookback. */
export function windowStart(cursor: Cursor, nowMs: number, overlapMs = 10 * 60_000, lookbackMs = 7 * 24 * 3600_000): string {
  const base = cursor.at ? Date.parse(cursor.at) - overlapMs : nowMs - lookbackMs
  return new Date(Number.isFinite(base) ? base : nowMs - lookbackMs).toISOString()
}

export type Outgoing = {
  id: string
  wire: Wire | 'workspace'
  sender: string
  to: string
  toUser: string | null
  workspace: string | null
  kind: 'message' | 'reply' | 'ack'
  topic: string
  body: string
  inReplyTo: string | null
  state: WorkState | null
  purpose: string | null
  artifacts: { path: string; version: string; owner?: string }[]
  sentAt: string
}

/** The stdin JSON for `fulcra record <channel>` in the contact's own format. */
export function encode(o: Outgoing): string {
  const body = withMarkers(o.body, o.state, o.purpose)
  if (o.wire === 'v1') {
    const rec: Json = { protocol: V1_PROTOCOL, message_id: o.id, sender: o.sender, recipients: [o.to], kind: o.kind, body, topic: o.topic }
    if (o.inReplyTo) rec.in_reply_to = o.inReplyTo
    if (o.artifacts.length) rec.artifacts = o.artifacts
    return JSON.stringify(rec)
  }
  if (o.wire === 'workspace') {
    const coord: Json = {
      protocol: WS_PROTOCOL, message_id: o.id, workspace: o.workspace, sender: o.sender, recipients: [o.to],
      kind: o.kind, sent_at: o.sentAt, topic: o.topic, body,
    }
    if (o.inReplyTo) coord.in_reply_to = o.inReplyTo
    if (o.artifacts.length) coord.artifacts = o.artifacts.map(a => ({ path: a.path, version: a.version }))
    return JSON.stringify({ note: JSON.stringify({ coord }) })
  }
  const slug = o.kind === 'ack' ? `${o.topic}-ack` : o.topic
  const files = o.artifacts.length ? `\n\nShared files:\n${o.artifacts.map(a => `- fulcra:${a.path} (version ${a.version})`).join('\n')}` : ''
  const env = { v: 1, mid: o.id, to: o.to, to_user: o.toUser ?? '', kind: o.kind === 'message' ? 'directive' : 'response', pri: 'P2', slug, body: body + files }
  return JSON.stringify({ note: JSON.stringify(env) })
}

/** True when a get-records readback holds the message id. */
export function readbackHas(stdout: string, id: string): boolean {
  return parseJsonl(stdout).rows.some(r => JSON.stringify(r).includes(id))
}
