// "Waiting on you": one list of everything waiting on the owner or the owner's agents, whoever is
// waiting. A friend's agent waiting for a reply in an AICQ thread and one of the owner's own agents
// blocked on `user:<owner>` on the coord bus are the same thing to the owner, so they rank together.
// Pure: the hooks module passes in the collaborations it shows and the universe it read.

import type { Collaboration } from './collab'
import type { NodeSession, Universe, UniverseNode } from './universe'
import type { WorkState } from '../types'

/** Collaboration states where the other side is waiting on the owner. */
export const WAITING_STATES: readonly WorkState[] = ['decision-needed', 'prepared-for-approval', 'needs-reply']

export type WaitingItem = {
  key: string
  /** An AICQ thread, or a task on the coord bus. */
  kind: 'thread' | 'task'
  /** Who is waiting: an agent label. */
  who: string
  whose: 'mine' | 'friend'
  platform: string
  machine: string
  /** What it is about: the thread topic or the task title. */
  title: string
  /** What it needs from the owner: the next action, or the latest message's gist. */
  ask: string
  /** When the wait started (ISO), when known. */
  since: string | null
  priority: string
  /** The thread's state, or `blocked` for a task. */
  state: WorkState | 'blocked'
  /** How to jump into the waiting agent's session, when it is known exactly. */
  session: NodeSession | null
  collabKey: string | null
  nodeKey: string | null
  taskId: string | null
}

const PRIORITY_RANK: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3 }

/** Most urgent first: priority, then the longest wait. */
export function rankWaiting(a: WaitingItem, b: WaitingItem): number {
  const pa = PRIORITY_RANK[a.priority] ?? 4
  const pb = PRIORITY_RANK[b.priority] ?? 4
  if (pa !== pb) return pa - pb
  const ta = a.since ? Date.parse(a.since) : Number.POSITIVE_INFINITY
  const tb = b.since ? Date.parse(b.since) : Number.POSITIVE_INFINITY
  return ta - tb
}

/** The universe node an own-account collaboration's counterpart is, when the map knows it by name. */
function nodeForCollab(c: Collaboration, nodes: readonly UniverseNode[]): UniverseNode | undefined {
  if (!c.workspace) return undefined
  return nodes.find(n => n.kind === 'agent' && (n.key === `agent:${c.contact}` || n.label === c.contact))
}

export function waitingOnYou(
  collabs: readonly Collaboration[],
  universe: Universe | null,
  owner: string,
  nameOf: (c: Collaboration) => string,
): WaitingItem[] {
  const nodes = universe?.nodes ?? []
  const out: WaitingItem[] = []
  for (const c of collabs) {
    if (!WAITING_STATES.includes(c.state)) continue
    const node = nodeForCollab(c, nodes)
    const last = [...c.messages].reverse().find(m => m.direction === 'in')
    out.push({
      key: `thread:${c.key}`, kind: 'thread', who: nameOf(c), whose: c.workspace ? 'mine' : 'friend',
      platform: node?.platform ?? '', machine: node?.machine ?? (c.workspace ? '' : 'Friends’ accounts'),
      title: c.topic, ask: c.nextAction || (last?.body ?? '').split('\n')[0]!.slice(0, 160),
      // The wait on the owner starts at their latest message (waitingSince is the opposite direction).
      since: last?.at ?? c.updatedAt, priority: c.state === 'decision-needed' || c.state === 'prepared-for-approval' ? 'P1' : '',
      state: c.state, session: node?.session ?? null, collabKey: c.key, nodeKey: node?.key ?? null, taskId: null,
    })
  }
  for (const n of nodes) {
    for (const b of n.blocked) {
      if (b.blockedOn !== `user:${owner}`) continue
      out.push({
        key: `task:${n.key}:${b.id}`, kind: 'task', who: n.label, whose: n.kind === 'peer' ? 'friend' : 'mine',
        platform: n.platform, machine: n.machine, title: b.title, ask: b.nextAction, since: b.since || null,
        priority: b.priority, state: 'blocked', session: n.session, collabKey: null, nodeKey: n.key, taskId: b.id,
      })
    }
  }
  return out.sort(rankWaiting)
}
