import { describe, expect, mock, test } from 'claude-code/testing'

import type { AicqMessage } from '../types'
import { collaborations, contextBlock, replyStats } from './collab'
import { modeOf, wakePrompt, withinBudget } from './policy'
import { buildUniverse, grouped, labelOf, placeIdentity, workspaceMembers } from './universe'
import {
  EMPTY_CURSOR, advance, encode, isWakeWorthy, outboxFor, parseJsonl, parsePeers, parseRow,
  parseWorkspaceChannel, readbackHas, splitMarkers, windowStart,
} from './wire'
import type { RowContext } from './wire'

// Synthetic ids only.
const ME = '00000000-0000-4000-8000-0000000000a1'
const PEER = '00000000-0000-4000-8000-0000000000b2'
const LEGACY_IN = 'MomentAnnotation/00000000-0000-4000-8000-0000000000c3'
const V1_IN = 'Event/00000000-0000-4000-8000-0000000000c4'
const V1_OUT = 'Event/00000000-0000-4000-8000-0000000000c5'
const LEGACY_OUT = 'MomentAnnotation/00000000-0000-4000-8000-0000000000c6'
const WS_CH = 'MomentAnnotation/00000000-0000-4000-8000-0000000000d4'

const ctxIn: RowContext = { source: 'mesh', channel: V1_IN, direction: 'in', contact: 'Peer Agent', contactUserId: PEER, workspace: null, me: ME, agentName: 'aicq' }
const wsCtx: RowContext = { source: 'workspace', channel: WS_CH, direction: 'in', contact: '', contactUserId: null, workspace: 'team', me: ME, agentName: 'aicq' }

const v1Row = (o: Record<string, unknown>, at = '2026-10-07T12:00:00Z') =>
  ({ id: 'r', start_time: at, protocol: 'connect-our-agents/1', sender: 'peer-agent', recipients: ['aicq'], kind: 'message', topic: 'review', body: 'hi', ...o })
const meshRow = (env: Record<string, unknown>, at = '2026-10-07T12:00:00Z') => ({ id: 'r', recorded_at: at, note: JSON.stringify(env) })
const wsRow = (coord: Record<string, unknown>) => ({ id: 'r', recorded_at: '2026-10-07T12:00:00Z', note: JSON.stringify({ coord: { protocol: 'fulcra.workspaces/1', ...coord } }) })

function msg(o: Partial<AicqMessage>): AicqMessage {
  return {
    id: crypto.randomUUID(), source: 'mesh', direction: 'in', channel: V1_IN, contact: 'Peer Agent', contactUserId: PEER, workspace: null,
    to: '', kind: 'message', topic: 'review', body: 'x', at: '2026-10-07T12:00:00Z', inReplyTo: null, state: null, purpose: null, artifacts: [], ...o,
  }
}

describe('wire: three formats in', () => {
  test('connect-our-agents/1 record', () => {
    const p = parseRow(v1Row({ message_id: 'v1', in_reply_to: 'p', artifacts: [{ path: 'a/b.txt', version: '3' }], body: '[state:working]\nOn it' }), ctxIn)
    if (!('message' in p)) throw new Error(JSON.stringify(p))
    expect(p.message.id).toBe('v1')
    expect(p.message.state).toBe('working')
    expect(p.message.body).toBe('On it')
    expect(p.message.inReplyTo).toBe('p')
    expect(p.message.artifacts[0]?.version).toBe('3')
    expect(p.message.at).toBe('2026-10-07T12:00:00Z')
  })

  test('legacy mesh: kinds normalised, acks recognised, heartbeats are presence, other users dropped', () => {
    const ctx = { ...ctxIn, channel: LEGACY_IN }
    const p = parseRow(meshRow({ v: 1, mid: 'm1', to_user: ME, kind: 'directive', slug: 'plan', body: 'b' }), ctx)
    expect('message' in p && p.message.kind).toBe('message')
    const ack = parseRow(meshRow({ v: 1, mid: 'm2', to_user: ME, kind: 'response', slug: 'plan-ack', body: 'ok' }), ctx)
    expect('message' in ack && ack.message.kind).toBe('ack')
    expect(parseRow(meshRow({ v: 1, mid: 'm3', kind: 'heartbeat' }), ctx)).toEqual({ skip: 'presence' })
    expect(parseRow(meshRow({ v: 1, mid: 'm4', to_user: 'someone-else', body: 'x' }), ctx)).toEqual({ skip: 'not-for-me' })
    expect(parseRow({ id: 'r', note: '{bad' }, ctx)).toEqual({ skip: 'malformed' })
  })

  test('workspace: mine is out, for me is in, others dropped', () => {
    const base = { message_id: 'w1', workspace: 'team', sender: 'tycho', recipients: ['aicq'], topic: 't', body: 'b', sent_at: '2026-10-07T12:00:00Z', kind: 'message' }
    const inn = parseRow(wsRow(base), wsCtx)
    expect('message' in inn && [inn.message.direction, inn.message.contact]).toEqual(['in', 'tycho'])
    const out = parseRow(wsRow({ ...base, message_id: 'w2', sender: 'aicq', recipients: ['tycho'] }), wsCtx)
    expect('message' in out && [out.message.direction, out.message.contact]).toEqual(['out', 'tycho'])
    expect(parseRow(wsRow({ ...base, message_id: 'w3', recipients: ['codex-coder'] }), wsCtx)).toEqual({ skip: 'not-for-me' })
  })

  test('markers split and survive a missing state', () => {
    expect(splitMarkers('[state:decision_needed]\n[purpose:Pick a date]\nText')).toEqual({ body: 'Text', state: 'decision-needed', purpose: 'Pick a date' })
    expect(splitMarkers('[state:bogus]\nText').state).toBe(null)
    expect(splitMarkers('plain').body).toBe('plain')
  })
})

describe('wire: peers, outboxes, out', () => {
  const inc = [
    { sharing_fulcra_userid: PEER, sharing_fulcra_user_name: 'Peer Agent', fulcra_data_types: [V1_IN, LEGACY_IN] },
    { sharing_fulcra_userid: 'p2', sharing_fulcra_user_name: 'p2', fulcra_data_types: ['HeartRate'] },
  ].map(r => JSON.stringify(r)).join('\n')
  const out = [
    { created_at: '2026-01-01', fulcra_data_types: [LEGACY_OUT], permissions: [{ allowed_fulcra_userid: PEER }] },
    { created_at: '2026-10-01', fulcra_data_types: [V1_OUT], permissions: [{ allowed_fulcra_userid: PEER }] },
  ].map(r => JSON.stringify(r)).join('\n')

  test('peers combine both directions; v1 outbox preferred when they speak v1', () => {
    const peers = parsePeers(inc, out, ME)
    expect(peers.length).toBe(1)
    expect(peers[0]!.inbound.length).toBe(2)
    expect(outboxFor(peers[0]!, '')).toEqual({ channel: V1_OUT, wire: 'v1' })
    const legacyOnly = { ...peers[0]!, inbound: [{ channel: LEGACY_IN, wire: 'mesh' as const }] }
    expect(outboxFor(legacyOnly, '')).toEqual({ channel: LEGACY_OUT, wire: 'mesh' })
  })

  test('encode writes each format', () => {
    const base = { id: 'id1', sender: 'aicq', to: 'peer-agent', toUser: PEER, workspace: null, kind: 'reply' as const, topic: 't', body: 'b', inReplyTo: 'p', state: 'completed' as const, purpose: null, artifacts: [{ path: 'x/y', version: '1' }], sentAt: 'now' }
    const v1 = JSON.parse(encode({ ...base, wire: 'v1' })) as Record<string, unknown>
    expect(v1.protocol).toBe('connect-our-agents/1')
    expect(v1.body).toBe('[state:completed]\nb')
    expect(v1.in_reply_to).toBe('p')
    const mesh = JSON.parse((JSON.parse(encode({ ...base, wire: 'mesh' })) as { note: string }).note) as Record<string, unknown>
    expect([mesh.mid, mesh.kind, mesh.to_user]).toEqual(['id1', 'response', PEER])
    expect(String(mesh.body).includes('fulcra:x/y')).toBe(true)
    const ws = JSON.parse((JSON.parse(encode({ ...base, wire: 'workspace', workspace: 'team' })) as { note: string }).note) as { coord: Record<string, unknown> }
    expect(ws.coord.protocol).toBe('fulcra.workspaces/1')
    expect(readbackHas(JSON.stringify(v1), 'id1')).toBe(true)
    // Exact identity: another message that mentions id1 (body or in_reply_to) never verifies it.
    const other = JSON.stringify({ protocol: 'connect-our-agents/1', message_id: 'id2', sender: 'x', recipients: ['y'], kind: 'reply', body: 'about id1', in_reply_to: 'id1', topic: 't' })
    expect(readbackHas(other, 'id1')).toBe(false)
    expect(readbackHas(JSON.stringify(v1), 'id1', 'other-topic')).toBe(false)
    expect(readbackHas(JSON.stringify({ note: JSON.stringify({ v: 1, mid: 'id9', body: 'id1' }) }), 'id1')).toBe(false)
  })

  test('cursor, window, index, jsonl', () => {
    const a = msg({ id: 'a', at: '2026-10-07T12:00:00Z' })
    const b = msg({ id: 'b', at: '2026-10-07T13:00:00Z' })
    const one = advance(EMPTY_CURSOR, [a])
    const two = advance(one.cursor, [a, b])
    expect(two.fresh.map(m => m.id)).toEqual(['b'])
    expect(two.cursor.at).toBe('2026-10-07T13:00:00Z')
    expect(windowStart({ at: '2026-10-07T12:00:00.000Z', seen: [] }, 0)).toBe('2026-10-07T11:50:00.000Z')
    expect(parseWorkspaceChannel(`Message channel: ${WS_CH}\n`)).toBe(WS_CH)
    expect(parseJsonl('{"a":1}\nnope\n').bad).toBe(1)
    expect(isWakeWorthy(msg({ direction: 'out' }))).toBe(false)
    expect(isWakeWorthy(msg({ kind: 'ack' }))).toBe(false)
    expect(isWakeWorthy(msg({}))).toBe(true)
  })
})

describe('collaborations', () => {
  const none = { paused: [], drafts: [] }
  test('groups by contact + topic and derives state', () => {
    const list = collaborations([
      msg({ id: '1', direction: 'out', topic: 'review', at: '2026-10-07T10:00:00Z', body: 'Please review the model' }),
      msg({ id: '2', topic: 'review-ack', kind: 'ack', at: '2026-10-07T10:01:00Z' }),
      msg({ id: '3', topic: 'lunch', at: '2026-10-07T11:00:00Z', kind: 'question', body: 'Friday or Monday?' }),
    ], none)
    const review = list.find(c => c.topic === 'review')!
    expect(review.state).toBe('waiting')
    expect(review.waitingSince).toBe('2026-10-07T10:00:00Z')
    expect(review.purpose).toBe('Please review the model')
    expect(list.find(c => c.topic === 'lunch')!.state).toBe('decision-needed')
    expect(list[0]!.topic).toBe('lunch')
  })

  test('explicit work updates, completion, pause and drafts', () => {
    const msgs = [msg({ id: '1', direction: 'out', at: '2026-10-07T10:00:00Z' }), msg({ id: '2', at: '2026-10-07T10:05:00Z', state: 'working', kind: 'reply' })]
    expect(collaborations(msgs, none)[0]!.state).toBe('working')
    expect(collaborations([...msgs, msg({ id: '3', at: '2026-10-07T11:00:00Z', kind: 'reply', body: 'Done: v7 delivered' })], none)[0]!.state).toBe('result-ready')
    expect(collaborations([...msgs, msg({ id: '4', at: '2026-10-07T11:30:00Z', kind: 'reply', state: 'completed', body: 'Booked.' })], none)[0]!.state).toBe('completed')
    const key = collaborations(msgs, none)[0]!.key
    expect(collaborations(msgs, { paused: [key], drafts: [] })[0]!.state).toBe('paused')
    expect(collaborations(msgs, { paused: [], drafts: [key] })[0]!.state).toBe('prepared-for-approval')
  })

  test('response time needs three substantive replies; receipts never count', () => {
    const seq: AicqMessage[] = []
    for (let i = 0; i < 3; i += 1) {
      seq.push(msg({ direction: 'out', at: `2026-10-0${i + 1}T10:00:00Z` }))
      seq.push(msg({ kind: 'ack', topic: 'review-ack', at: `2026-10-0${i + 1}T10:00:30Z` }))
      seq.push(msg({ kind: 'reply', at: `2026-10-0${i + 1}T10:0${i + 1}:00Z` }))
    }
    const key = `mesh:${PEER}`
    expect(replyStats(seq.slice(0, 6), key)).toEqual({ typicalMs: null, samples: 2 })
    expect(replyStats(seq, key)).toEqual({ typicalMs: 120_000, samples: 3 })
    expect(contextBlock(collaborations(seq, none)[0]!).includes('not the user')).toBe(true)
  })
})

describe('universe', () => {
  const machines = ['DeskBookPro', 'SingularityServer']
  test('placement: platform:machine:name, namespaces, instance ids, hints', () => {
    expect(placeIdentity('claude-code:DeskBookPro:fulcra-tools-coord', machines, {})).toEqual({ machine: 'DeskBookPro', platform: 'Claude Code' })
    expect(placeIdentity('openclaw:arc:main-comms', machines, {})).toEqual({ machine: 'Unplaced', platform: 'OpenClaw' })
    expect(placeIdentity('home-infra-maintainer', machines, { machine: 'codex-desktop-singularity-home-infra' }).machine).toBe('SingularityServer')
    expect(placeIdentity('tycho', machines, { text: 'independent Linux Claude Code cloud tester' })).toEqual({ machine: 'Cloud', platform: 'Claude Code' })
    expect(labelOf('openclaw:arc:main-comms', machines)).toBe('arc/main-comms')
    expect(labelOf('claude-code:DeskBookPro:fulcra-tools-coord', machines)).toBe('fulcra-tools-coord')
    expect(labelOf('coord-reconcile:DeskBookPro', machines)).toBe('reconcile host')
  })

  test('build: blocked-on-owner, meshes, owner excluded, this agent placed', () => {
    const u = buildUniverse({
      builtAt: '2026-10-07T12:00:00Z', agents: COORD.agents as never, board: COORD.board as never, health: COORD.health as never,
      v5: [{ logicalAgentId: 'arc-maintainer', instanceId: 'singularityserver', workspaceId: 'w1' }],
      workspaces: { team: '## Members\n- codex-coder — joined; implementation\n- tycho — Linux Claude Code cloud tester\n\n## Messaging' },
      peers: [{ userId: PEER, label: 'Peer Agent' }], owner: 'ash',
      thisAgent: { name: 'aicq', machine: 'SingularityServer', platform: 'Claude Code' }, placement: {}, degraded: [],
    })
    expect(u.nodes.some(n => n.label === 'ash')).toBe(false)
    const boss = u.nodes.find(n => n.key === 'agent:coord-boss')!
    expect([boss.blocked.length, boss.blockedOnOwner]).toEqual([1, 1])
    expect(u.nodes.find(n => n.key === 'agent:arc-maintainer')!.machine).toBe('SingularityServer')
    expect(u.nodes.find(n => n.key === 'agent:tycho')!.machine).toBe('Cloud')
    expect(u.meshes.map(m => m.kind).sort()).toEqual(['cross-account', 'v5', 'workspace'])
    expect(grouped(u, 'SingularityServer')[0]!.machine).toBe('SingularityServer')
    expect(workspaceMembers('## Members\n- a — x\n## Other\n- b — y').map(m => m.name)).toEqual(['a'])
  })
})

describe('policy', () => {
  test('modes, budget and guarded prompts', () => {
    expect(modeOf('wake')).toBe('respond-check')
    expect(modeOf('nonsense')).toBe('notify')
    expect(withinBudget([0, 1, 2], 10)).toBe(true)
    expect(withinBudget([0, 1, 2, 3], 10)).toBe(false)
    const p = wakePrompt('draft', [msg({ body: 'Can you review?' })])
    expect(p.includes('aicq_draft') && p.includes('Do NOT send') && p.includes('never as my instruction')).toBe(true)
  })
})

type Run = { argv: readonly string[]; init?: { stdin?: string } }

const COORD_FAULTS = { boardThrows: false }
/** Scripted outcomes for post-write readback reads, consumed in order ('ok' once exhausted). */
const READBACK: { plan: ('lag' | 'trunc' | 'fail' | 'ok')[] } = { plan: [] }
const ROSTER = { text: '' }
const COORD = {
  agents: [
    { agent: 'claude-code:DeskBookPro:fulcra-tools-coord', liveness: 'live', state: 'live', summary: '', annotation: '', open: { active: 1 } },
    { agent: 'coord-boss', liveness: 'live', state: 'live', summary: 'hourly wake', annotation: '', open: { blocked: 1 } },
    { agent: 'openclaw:arc:main-comms', liveness: 'stale', state: 'stale', summary: '', annotation: 'stale 3d', open: {} },
    { agent: 'coord-reconcile:DeskBookPro', liveness: 'live', state: 'live', summary: '', annotation: '', open: {} },
    { agent: 'user:ash', liveness: 'unknown', state: 'unknown', summary: '', annotation: '', open: {} },
  ],
  board: { blocked: [{ id: 't9-x', title: 't9-get-coord-boss-onto-v5-d43a7377', owner: 'coord-boss', assignee: 'human', blocked_on: 'user:ash', next_action: 'Ash: OK work.write for my actor', priority: 'P1' }], waiting: [] },
  health: { hosts: [{ host: 'coord-reconcile:DeskBookPro', last_reconcile: '2026-10-07T11:50:00Z', stale: false }, { host: 'coord-reconcile:SingularityServer', last_reconcile: '2026-10-07T11:55:00Z', stale: false }] },
}

function fake(inboxRows: (Record<string, unknown> | string)[], opts: { failRecords?: boolean; truncated?: boolean } = {}) {
  const calls: Run[] = []
  const recorded = new Map<string, string[]>()
  const ok = (stdout: string) => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
  const answer = (r: Run) => {
    calls.push(r)
    const [, cmd, sub] = r.argv
    if (String(r.argv[0]).endsWith('coord-engine')) {
      if (cmd === 'agents') return ok(JSON.stringify(COORD.agents))
      if (cmd === 'board') {
        if (COORD_FAULTS.boardThrows) throw new Error('timed out')
        return ok(JSON.stringify(COORD.board))
      }
      if (cmd === 'health') return ok(JSON.stringify(COORD.health))
      if (cmd === 'tell') return ok(`directive ${String(r.argv[4]).slice(0, 20)}-abcd1234 -> ${String(r.argv[3])}`)
    }
    if (cmd === 'user-info') return ok(JSON.stringify({ userid: ME }))
    if (cmd === 'share' && sub === 'list-incoming') return ok(JSON.stringify({ sharing_fulcra_userid: PEER, sharing_fulcra_user_name: 'Peer Agent', fulcra_data_types: [V1_IN] }))
    if (cmd === 'share' && sub === 'list-outgoing') return ok(JSON.stringify({ created_at: '2026-10-01', fulcra_data_types: [V1_OUT], permissions: [{ allowed_fulcra_userid: PEER }] }))
    if (cmd === 'file' && (sub === 'upload' || sub === 'share')) return ok('ok')
    if (cmd === 'file' && sub === 'download' && String(r.argv[3]) === 'workspace/team/index.md') return ok(ROSTER.text)
    if (cmd === 'record') {
      const ch = String(r.argv[2])
      recorded.set(ch, [...(recorded.get(ch) ?? []), r.init?.stdin ?? ''])
      return ok('Upload ID: x')
    }
    if (cmd === 'get-records') {
      if (opts.failRecords) return { value: { exitCode: 1, stdout: '', stderr: 'boom', isStdoutTruncated: false, isStderrTruncated: false } }
      const ch = String(r.argv[2])
      if (ch === V1_OUT && READBACK.plan.length) {
        const step = READBACK.plan.shift()!
        if (step === 'lag') return ok('')
        if (step === 'fail') return { value: { exitCode: 1, stdout: '', stderr: 'boom', isStdoutTruncated: false, isStderrTruncated: false } }
        if (step === 'trunc') return { value: { exitCode: 0, stdout: (recorded.get(ch) ?? []).map(x => JSON.stringify({ start_time: '2026-10-07T12:00:00Z', ...JSON.parse(x) as object })).join('\n'), stderr: '', isStdoutTruncated: true, isStderrTruncated: false } }
      }
      if (ch === V1_IN) return { value: { exitCode: 0, stdout: inboxRows.map(x => (typeof x === 'string' ? x : JSON.stringify(x))).join('\n'), stderr: '', isStdoutTruncated: !!opts.truncated, isStderrTruncated: false } }
      return ok((recorded.get(ch) ?? []).map(x => JSON.stringify({ start_time: '2026-10-07T12:00:00Z', ...JSON.parse(x) as object })).join('\n'))
    }
    return { value: { exitCode: 2, stdout: '', stderr: `unknown ${String(sub)}`, isStdoutTruncated: false, isStderrTruncated: false } }
  }
  return { calls, recorded, answer }
}

type Sink = { toasts: string[]; prompts: string[]; statuses: string[] }
const sinkOf = (): Sink => ({ toasts: [], prompts: [], statuses: [] })

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function engine(on: any, f: ReturnType<typeof fake>, sink: Sink) {
  on('session.start', () => ({ cwd: '/tmp' }))
  on('command.register', () => ({ value: undefined }))
  on('tool.register', (_$: unknown, e: { name: string }) => ({ value: { tool: `mcp__aicq__${e.name}` } }))
  on('ui.open', () => ({ value: undefined }))
  on('process.run', (_$: unknown, e: Run) => f.answer(e))
  on('ui.toast', (_$: unknown, e: { text: string }) => { sink.toasts.push(e.text); return { value: undefined } })
  on('ui.status', (_$: unknown, e: { text?: string }) => { sink.statuses.push(String(e.text)); return { value: undefined } })
  on('prompt.submit', (_$: unknown, e: { text: string }) => { sink.prompts.push(e.text); return { text: e.text } })
}

const T0 = Date.parse('2026-10-07T12:00:00Z')

describe('mod', () => {
  test('backfill is quiet; arrivals notify; respond mode starts one guarded turn', { options: { agentName: 'aicq' } }, async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const rows = [v1Row({ message_id: 'old', body: 'old one' }, '2026-10-07T11:00:00Z')]
    const f = fake(rows)
    const sink = sinkOf()
    engine(on, f, sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    expect(sink.toasts.length).toBe(0)

    rows.push(v1Row({ message_id: 'new', body: 'please review', topic: 'review' }, '2026-10-07T12:01:00Z'))
    await clock.advance(120_000)
    expect(sink.toasts.length).toBe(1)
    expect(sink.prompts.length).toBe(0)

    await $.command.run({ command: 'aicq', args: 'mode respond-check' } as never)
    rows.push(v1Row({ message_id: 'new2', body: 'one more', topic: 'review' }, '2026-10-07T12:03:00Z'))
    rows.push(v1Row({ message_id: 'ackx', kind: 'ack', topic: 'review' }, '2026-10-07T12:03:30Z'))
    await clock.advance(120_000)
    expect(sink.prompts.length).toBe(1)
    expect(sink.prompts[0]!.includes('one more') && !sink.prompts[0]!.includes('ackx')).toBe(true)

    const res = JSON.stringify(await $.tool.call({ tool: 'mcp__aicq__aicq_inbox' } as never))
    expect(res.includes('needs-reply') && res.includes('please review')).toBe(true)
  })

  // The durable cursors are shared by every session on the machine and outlive each one; the inbox is
  // per session. A new session (or a sibling that polled first) must still show what is already seen.
  test('a new session shows messages an earlier or sibling session already saw, without re-notifying', { options: { agentName: 'aicq' } }, async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on, { schema: 2, [`cursor:in:${PEER}:${V1_IN}`]: { at: '2026-10-07T11:00:00Z', seen: ['seen-before'] } })
    mock.env(on, { HOME: '/home/t' })
    const sink = sinkOf()
    engine(on, fake([v1Row({ message_id: 'seen-before', body: 'already handled elsewhere', topic: 'review' }, '2026-10-07T11:00:00Z')]), sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    const res = JSON.stringify(await $.tool.call({ tool: 'mcp__aicq__aicq_inbox' } as never))
    expect(res.includes('already handled elsewhere')).toBe(true)
    expect(sink.toasts.length).toBe(0)
    expect(sink.prompts.length).toBe(0)
  })

  test('a message that arrived while no session ran is still an arrival in the next one', { options: { agentName: 'aicq' } }, async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on, { schema: 2, [`cursor:in:${PEER}:${V1_IN}`]: { at: '2026-10-07T10:00:00Z', seen: ['older'] } })
    mock.env(on, { HOME: '/home/t' })
    const sink = sinkOf()
    engine(on, fake([
      v1Row({ message_id: 'older', body: 'older one', topic: 'review' }, '2026-10-07T10:00:00Z'),
      v1Row({ message_id: 'while-away', body: 'sent while you were away', topic: 'review' }, '2026-10-07T11:30:00Z'),
    ]), sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    expect(sink.toasts.length).toBe(1)
    const res = JSON.stringify(await $.tool.call({ tool: 'mcp__aicq__aicq_inbox' } as never))
    expect(res.includes('older one') && res.includes('sent while you were away')).toBe(true)
  })

  // The ledger's seen set is bounded (800 ids), so a handled id can age out. The session's 7-day display
  // backfill must not hand such an old message back to the ledger as an arrival (codex-reviewer, PR #803).
  test('display backfill never replays an evicted, already-handled message as an arrival', { options: { agentName: 'aicq' } }, async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    const recent = Array.from({ length: 800 }, (_, i) => `recent-${i}`)
    mock.store(on, { schema: 2, mode: 'respond-check', [`cursor:in:${PEER}:${V1_IN}`]: { at: '2026-10-07T11:00:00Z', seen: recent } })
    mock.env(on, { HOME: '/home/t' })
    const sink = sinkOf()
    engine(on, fake([
      v1Row({ message_id: 'evicted-old', body: 'old request, handled long ago', topic: 'review' }, '2026-10-06T12:00:00Z'),
      v1Row({ message_id: 'recent-799', body: 'latest handled', topic: 'review' }, '2026-10-07T11:00:00Z'),
    ]), sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    const res = JSON.stringify(await $.tool.call({ tool: 'mcp__aicq__aicq_inbox' } as never))
    expect(res.includes('old request, handled long ago')).toBe(true)
    expect({ toasts: sink.toasts.length, prompts: sink.prompts.length }).toEqual({ toasts: 0, prompts: 0 })
  })

  test('a delayed message inside the ledger overlap is still an arrival', { options: { agentName: 'aicq' } }, async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on, { schema: 2, [`cursor:in:${PEER}:${V1_IN}`]: { at: '2026-10-07T11:00:00Z', seen: ['handled'] } })
    mock.env(on, { HOME: '/home/t' })
    const sink = sinkOf()
    engine(on, fake([
      v1Row({ message_id: 'handled', body: 'handled', topic: 'review' }, '2026-10-07T11:00:00Z'),
      // Stamped 5 minutes before the watermark but landed after it: inside the 10-minute overlap.
      v1Row({ message_id: 'delayed', body: 'arrived late', topic: 'review' }, '2026-10-07T10:55:00Z'),
    ]), sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    expect(sink.toasts.length).toBe(1)
  })

  test('a failed read is degraded, never an empty all-clear', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const sink = sinkOf()
    engine(on, fake([], { failRecords: true }), sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    expect(sink.statuses.some(t => t.includes('check failed (Peer Agent'))).toBe(true)
  })

  test('send speaks v1 to a v1 peer and reads back', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const f = fake([])
    engine(on, f, sinkOf())
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    const res = JSON.stringify(await $.tool.call({ tool: 'mcp__aicq__aicq_send', to: 'Peer Agent', topic: 'hello', body: 'hi', state: 'working' } as never))
    expect(res.includes('Sent and read back')).toBe(true)
    const sent = JSON.parse(f.recorded.get(V1_OUT)![0]!) as Record<string, unknown>
    expect([sent.protocol, sent.body]).toEqual(['connect-our-agents/1', '[state:working]\nhi'])
  })

  test('draft: nothing sent until approved; the pane leads with it on both surfaces', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const f = fake([v1Row({ message_id: 'q1', kind: 'message', topic: 'review', body: 'review this?' }, '2026-10-07T11:00:00Z')])
    engine(on, f, sinkOf())
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    const r = JSON.stringify(await $.tool.call({ tool: 'mcp__aicq__aicq_draft', to: 'Peer Agent', topic: 'review', body: 'Looks good', in_reply_to: 'q1' } as never))
    expect(r.includes('Nothing was sent')).toBe(true)
    expect(f.recorded.size).toBe(0)
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'aicq', surface, component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as { findAll: (q: unknown) => Promise<unknown[]>; press: (t: unknown) => Promise<unknown> }
      if (surface === 'terminal') {
        expect((await ui.findAll({ text: /Your agents at work/ })).length > 0).toBe(true)
        expect((await ui.findAll({ text: /Prepared for approval/ })).length > 0).toBe(true)
        expect((await ui.findAll({ text: /FRIENDS AGENTS/ })).length > 0).toBe(true)
      } else {
        expect((await ui.findAll({ type: 'Svg' })).length > 2).toBe(true)
        expect((await ui.findAll({ type: 'Button', text: /Review draft/ })).length).toBe(1)
        // The sidebar is interactive: pressing a contact opens its collaboration with the draft to approve.
        await ui.press({ key: `ds-open-mesh:${PEER}` })
        expect((await ui.findAll({ type: 'Button', text: /Approve and proceed/ })).length).toBe(1)
        expect((await ui.findAll({ type: 'Button', text: /Agent conversation · 1 update/ })).length).toBe(1)
        await ui.press({ key: 'dback' })
        await ui.press({ key: `ds-new-mesh:${PEER}` })
        expect((await ui.findAll({ type: 'Input', key: 'dstart' })).length).toBe(1)
        await ui.press({ key: 'dback' })
      }
    }
  })

  test('decision options: each choice sends its own reply, nothing before the press', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const f = fake([v1Row({ message_id: 'q2', kind: 'message', topic: 'review-time', body: 'Friday is out. Monday 10?' }, '2026-10-07T11:00:00Z')])
    engine(on, f, sinkOf())
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    await $.tool.call({ tool: 'mcp__aicq__aicq_draft', to: 'Peer Agent', topic: 'review-time', body: 'fallback', in_reply_to: 'q2', question: 'Move the review to Monday at 10?',
      options: [{ label: 'Book Monday at 10', body: 'Monday 10 works. Please book it.' }, { label: 'Keep Friday', body: 'Please keep Friday.' }] } as never)
    expect(f.recorded.size).toBe(0)
    const ui = await $.ui.mount({ plugin: 'aicq', surface: 'desktop', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as { findAll: (q: unknown) => Promise<unknown[]>; press: (t: unknown) => Promise<unknown> }
    await ui.press({ key: `ds-open-mesh:${PEER}` })
    expect((await ui.findAll({ type: 'Button', text: /Book Monday at 10/ })).length).toBe(1)
    await ui.press({ key: 'dopt-1' })
    const sent = JSON.parse(f.recorded.get(V1_OUT)![0]!) as Record<string, unknown>
    expect([sent.body, sent.in_reply_to]).toEqual(['Please keep Friday.', 'q2'])
  })

  test('per-collaboration autonomy: an override to "Just notify me" stops the wake', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const rows = [v1Row({ message_id: 'o1', topic: 'quiet-thread', body: 'first' }, '2026-10-07T11:00:00Z')]
    const sink = sinkOf()
    engine(on, fake(rows), sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    await $.command.run({ command: 'aicq', args: 'mode respond-results' } as never)
    const ui = await $.ui.mount({ plugin: 'aicq', surface: 'desktop', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as { press: (t: unknown) => Promise<unknown>; select: (t: unknown) => Promise<unknown> }
    await ui.press({ key: `ds-open-mesh:${PEER}` })
    await (ui as unknown as { select: (t: unknown) => Promise<unknown> }).select({ key: 'dauto', value: 'notify' })
    rows.push(v1Row({ message_id: 'o2', topic: 'quiet-thread', body: 'second' }, '2026-10-07T12:01:00Z'))
    await clock.advance(120_000)
    expect(sink.prompts.length).toBe(0)
    expect(sink.toasts.filter(t => t.includes('new from')).length).toBe(1)
  })

  test('inline chat cards: send receipt and decision render on both surfaces', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    engine(on, fake([v1Row({ message_id: 'q3', topic: 'plan', body: 'Which date?' }, '2026-10-07T11:00:00Z')]), sinkOf())
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    await $.tool.call({ tool: 'mcp__aicq__aicq_draft', to: 'Peer Agent', topic: 'plan', body: 'x', question: 'Pick a date?', options: [{ label: 'Oct 19', body: 'Oct 19 please' }] } as never)
    for (const surface of ['terminal', 'desktop'] as const) {
      const send = await $.ui.mount({ plugin: 'aicq', surface, component: 'ToolUse', props: { tool_use_id: 't1', tool: 'mcp__aicq__aicq_send', input: { to: 'Peer Agent', topic: 'plan', body: 'Here is v3' }, isRunning: false, isErrored: false, isInterrupted: false, output: 'Sent and read back: id 1 to Peer Agent' } } as never) as unknown as { findAll: (q: unknown) => Promise<unknown[]> }
      expect((await send.findAll({ text: /Sent to Peer Agent/ })).length > 0).toBe(true)
      expect((await send.findAll({ type: 'Button', text: /View collaboration/ })).length).toBe(1)
      const draft = await $.ui.mount({ plugin: 'aicq', surface, component: 'ToolUse', props: { tool_use_id: 't2', tool: 'mcp__aicq__aicq_draft', input: { to: 'Peer Agent', topic: 'plan', body: 'x', question: 'Pick a date?' }, isRunning: false, isErrored: false, isInterrupted: false, output: 'saved' } } as never) as unknown as { findAll: (q: unknown) => Promise<unknown[]> }
      expect((await draft.findAll({ type: 'Button', text: /Oct 19/ })).length).toBe(1)
    }
  })

  test('rename a contact; hide a collaboration until something new arrives', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const rows = [v1Row({ message_id: 'h1', topic: 'noise', body: 'daily report' }, '2026-10-07T11:00:00Z')]
    engine(on, fake(rows), sinkOf())
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    type UI = { findAll: (q: unknown) => Promise<unknown[]>; press: (t: unknown) => Promise<unknown>; input: (t: unknown) => Promise<unknown> }
    const ui = await $.ui.mount({ plugin: 'aicq', surface: 'desktop', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as UI
    await ui.press({ key: `ds-new-mesh:${PEER}` })
    await ui.input({ key: 'drename', text: 'Renamed Contact' })
    expect((await ui.findAll({ type: 'Button', text: /^Renamed Contact$/ })).length).toBeGreaterThan(0)
    await ui.press({ key: `ds-open-mesh:${PEER}` })
    await ui.press({ key: 'dhide' })
    expect((await ui.findAll({ text: /1 hidden/ })).length).toBeGreaterThan(0)
    rows.push(v1Row({ message_id: 'h2', topic: 'noise', body: 'new report' }, '2026-10-07T12:01:00Z'))
    await clock.advance(120_000)
    expect((await ui.findAll({ text: /hidden/ })).length).toBe(0)
  })

  test('quiet channels back off; Refresh still reads everything', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const f = fake([])
    engine(on, f, sinkOf())
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    const reads = () => f.calls.filter(c => c.argv[1] === 'get-records' && c.argv[2] === V1_IN).length
    for (let i = 0; i < 6; i += 1) await clock.advance(120_000)
    const before = reads()
    await clock.advance(120_000)
    await clock.advance(120_000)
    expect(reads() - before).toBeLessThan(2)
    const ui = await $.ui.mount({ plugin: 'aicq', surface: 'desktop', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as { press: (t: unknown) => Promise<unknown> }
    const r0 = reads()
    await ui.press({ key: 'drefresh' })
    expect(reads()).toBe(r0 + 1)
  })

  test('mixed-policy arrivals: a draft-only collaboration is never answered autonomously, in either order', { options: { agentName: 'aicq' } }, async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const rows: Record<string, unknown>[] = [
      v1Row({ message_id: 'a0', topic: 'auto-thread', body: 'auto start' }, '2026-10-07T11:00:00Z'),
      v1Row({ message_id: 'c0', topic: 'careful-thread', body: 'careful start' }, '2026-10-07T11:00:00Z'),
    ]
    const sink = sinkOf()
    const f = fake(rows)
    engine(on, f, sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    await $.command.run({ command: 'aicq', args: 'mode respond-results' } as never)
    type UI = { press: (t: unknown) => Promise<unknown>; select: (t: unknown) => Promise<unknown>; findAll: (q: unknown) => Promise<unknown[]> }
    const ui = await $.ui.mount({ plugin: 'aicq', surface: 'desktop', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as UI
    await ui.press({ key: `ds-new-mesh:${PEER}` })
    await ui.press({ key: `dch-mesh:${PEER}#careful-thread` })
    await ui.select({ key: 'dauto', value: 'draft' })
    for (const [first, second, at] of [['auto-thread', 'careful-thread', '2026-10-07T12:01:00Z'], ['careful-thread', 'auto-thread', '2026-10-07T12:03:00Z']] as const) {
      sink.prompts.length = 0
      rows.push(v1Row({ message_id: `${first}-${at}`, topic: first, body: `${first} body` }, at))
      rows.push(v1Row({ message_id: `${second}-${at}`, topic: second, body: `${second} body` }, at))
      await clock.advance(120_000)
      const careful = sink.prompts.filter(p => p.includes('careful-thread body'))
      const auto = sink.prompts.filter(p => p.includes('auto-thread body'))
      expect(careful.length).toBe(1)
      expect(careful[0]!.includes('Do NOT send')).toBe(true)
      expect(careful[0]!.includes('auto-thread body')).toBe(false)
      expect(auto.length).toBe(1)
    }
    // And the send hook enforces it, whatever a prompt says.
    const r = JSON.stringify(await $.tool.call({ tool: 'mcp__aicq__aicq_send', to: 'Peer Agent', topic: 'careful-thread', body: 'sneaky' } as never))
    expect(r.includes('Prepare for my approval')).toBe(true)
    expect(f.recorded.size).toBe(0)
  })

  test('malformed-only and mixed reads are reported, not silently empty; truncated reads keep the watermark', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const rows: (Record<string, unknown> | string)[] = [{ id: 'bad-1', start_time: '2026-10-07T11:00:00Z', note: '{"not":"a message"}' }]
    const sink = sinkOf()
    engine(on, fake(rows), sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    expect(sink.statuses.some(t => t.includes('1 unreadable'))).toBe(true)
    // Mixed: the valid message lands; the old unreadable record is not reported again.
    sink.statuses.length = 0
    rows.push(v1Row({ message_id: 'ok-1', body: 'fine' }, '2026-10-07T12:01:00Z'))
    await clock.advance(120_000)
    expect(sink.statuses[sink.statuses.length - 1]!.includes('unreadable')).toBe(false)
    const res = JSON.stringify(await $.tool.call({ tool: 'mcp__aicq__aicq_inbox', topic: 'review' } as never))
    expect(res.includes('fine')).toBe(true)
  })

  test('a truncated read is degraded and does not advance the watermark', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const opts = { truncated: false }
    const rows: (Record<string, unknown> | string)[] = [v1Row({ message_id: 't0', body: 'complete' }, '2026-10-07T11:00:00Z')]
    const f = fake(rows, opts)
    const sink = sinkOf()
    engine(on, f, sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000) // complete read: watermark 11:00
    opts.truncated = true
    rows.push(v1Row({ message_id: 't1', body: 'partial' }, '2026-10-07T12:00:30Z'), 'not json at all')
    await clock.advance(120_000) // truncated read sees t1 but must not move the watermark to 12:00
    expect(sink.statuses[sink.statuses.length - 1]!.includes('incomplete read')).toBe(true)
    opts.truncated = false
    await clock.advance(120_000)
    const starts = f.calls.filter(c => c.argv[1] === 'get-records' && c.argv[2] === V1_IN).map(c => String(c.argv[3]))
    expect(starts[starts.length - 1]).toBe('2026-10-07T10:50:00.000Z')
  })


  test('share honors policy: paused and draft-only have zero effects before approval', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const f = fake([v1Row({ message_id: 's0', topic: 'plan', body: 'send me the plan' }, '2026-10-07T11:00:00Z')])
    engine(on, f, sinkOf())
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    const effects = () => f.calls.filter(c => (c.argv[1] === 'file' && (c.argv[2] === 'upload' || c.argv[2] === 'share')) || c.argv[1] === 'record').length
    const share = () => $.tool.call({ tool: 'mcp__aicq__aicq_share', path: '/tmp/plan.pdf', to: 'Peer Agent', topic: 'plan', body: 'Here is the plan' } as never)
    type UI = { press: (t: unknown) => Promise<unknown>; findAll: (q: unknown) => Promise<unknown[]> }
    const ui = await $.ui.mount({ plugin: 'aicq', surface: 'desktop', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as UI
    // Paused: refused, nothing uploaded, granted or sent.
    await ui.press({ key: `ds-open-mesh:${PEER}` })
    await ui.press({ key: 'dpause' })
    expect(JSON.stringify(await share()).includes('paused')).toBe(true)
    expect(effects()).toBe(0)
    await ui.press({ key: 'dpause' })
    // Draft-only: held as a draft, still nothing; approval runs upload, grant and send.
    await $.command.run({ command: 'aicq', args: 'mode draft' } as never)
    expect(JSON.stringify(await share()).includes('nothing was uploaded')).toBe(true)
    expect(effects()).toBe(0)
    await ui.press({ key: 'dapprove' })
    expect(f.calls.some(c => c.argv[1] === 'file' && c.argv[2] === 'upload' && c.argv[3] === '/tmp/plan.pdf')).toBe(true)
    expect(f.calls.some(c => c.argv[1] === 'file' && c.argv[2] === 'share' && c.argv.includes(PEER))).toBe(true)
    const sent = JSON.parse(f.recorded.get(V1_OUT)![0]!) as { artifacts?: { path: string }[] }
    expect(sent.artifacts?.[0]?.path.endsWith('plan.pdf')).toBe(true)
  })

  test('ownership: threads another agent started never wake this session until taken over', { options: { agentName: 'aicq' } }, async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const rows: Record<string, unknown>[] = [v1Row({ message_id: 'x0', topic: 'other-thread', recipients: ['gatekeeper'], body: 'earlier' }, '2026-10-07T11:00:00Z')]
    const sink = sinkOf()
    const f = fake(rows)
    engine(on, f, sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    await $.command.run({ command: 'aicq', args: 'mode respond-results' } as never)
    // A reply to a message this agent never sent: another agent's thread.
    rows.push(v1Row({ message_id: 'x1', topic: 'other-thread', recipients: ['gatekeeper'], body: 'In reply to 11111111-2222-3333-4444-555555555555: Wednesday works.' }, '2026-10-07T12:01:00Z'))
    await clock.advance(120_000)
    expect(sink.prompts.length).toBe(0)
    expect(sink.toasts.some(t => t.includes('handled by another of your agents'))).toBe(true)
    const inbox = JSON.stringify(await $.tool.call({ tool: 'mcp__aicq__aicq_inbox', topic: 'other-thread' } as never))
    expect(inbox.includes('another of the owner')).toBe(true)
    // The owner hands it over; the next message in it wakes this agent.
    type UI = { press: (t: unknown) => Promise<unknown> }
    const ui = await $.ui.mount({ plugin: 'aicq', surface: 'desktop', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as UI
    await ui.press({ key: `ds-new-mesh:${PEER}` })
    await ui.press({ key: `dch-mesh:${PEER}#other-thread` })
    await ui.press({ key: 'dtake' })
    rows.push(v1Row({ message_id: 'x2', topic: 'other-thread', recipients: ['gatekeeper'], body: 'One more detail' }, '2026-10-07T12:03:00Z'))
    await clock.advance(120_000)
    expect(sink.prompts.length).toBe(1)
  })

  test('ownership: a thread this agent started keeps waking it', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const rows: Record<string, unknown>[] = []
    const sink = sinkOf()
    const f = fake(rows)
    engine(on, f, sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    await $.command.run({ command: 'aicq', args: 'mode respond-results' } as never)
    await $.tool.call({ tool: 'mcp__aicq__aicq_send', to: 'Peer Agent', topic: 'mine-thread', body: 'Can you review?' } as never)
    const sentId = (JSON.parse(f.recorded.get(V1_OUT)![0]!) as { message_id: string }).message_id
    rows.push(v1Row({ message_id: 'm1', topic: 'mine-thread', recipients: ['someone'], kind: 'reply', in_reply_to: sentId, body: 'Reviewed.' }, '2026-10-07T12:01:00Z'))
    await clock.advance(120_000)
    expect(sink.prompts.length).toBe(1)
  })

  for (const n of [399, 401]) {
    test(`ownership survives inbox eviction (${n} later messages)`, async ($, on) => {
      const clock = mock.clock(on, { now: T0 })
      mock.store(on)
      mock.env(on, { HOME: '/home/t' })
      const rows: Record<string, unknown>[] = []
      const sink = sinkOf()
      const f = fake(rows)
      engine(on, f, sink)
      await $.session.start({ cwd: '/tmp' } as never)
      await clock.advance(2000)
      await $.command.run({ command: 'aicq', args: 'mode respond-results' } as never)
      await $.tool.call({ tool: 'mcp__aicq__aicq_send', to: 'Peer Agent', topic: 'long-owned-thread', body: 'Start' } as never)
      for (let i = 0; i < n; i += 1) {
        rows.push(v1Row({ message_id: `l${i}`, topic: 'long-owned-thread', recipients: ['someone'], body: `update ${i}` }, new Date(T0 + 60_000 + i * 1000).toISOString()))
      }
      await clock.advance(120_000)
      expect(sink.prompts.length).toBe(1)
    })
  }

  test('ownership has no count cap: an old owned thread still wakes after 2000 newer owned threads', { options: { agentName: 'aicq' } }, async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const rows: Record<string, unknown>[] = [v1Row({ message_id: 'old-0', topic: 'old-owned', recipients: ['aicq'], body: 'addressed to aicq' }, '2026-10-07T10:00:00Z')]
    const sink = sinkOf()
    const f = fake(rows)
    engine(on, f, sink)
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000) // backfill: old-owned is this agent's (addressed by name)
    for (let i = 0; i < 1999; i += 1) rows.push(v1Row({ message_id: `t${i}`, topic: `topic-${i}`, recipients: ['aicq'], body: `hi ${i}` }, new Date(T0 + 30_000 + i * 10).toISOString()))
    await clock.advance(120_000) // notify mode: 1999 newer owned threads, no turns
    expect(sink.prompts.length).toBe(0)
    await $.command.run({ command: 'aicq', args: 'mode respond-results' } as never)
    await $.tool.call({ tool: 'mcp__aicq__aicq_send', to: 'Peer Agent', topic: 'new-owned', body: 'one more' } as never)
    // The next read holds only a new old-owned update, not addressed by name and with no reply reference.
    rows.length = 0
    rows.push(v1Row({ message_id: 'old-1', topic: 'old-owned', recipients: ['someone'], body: 'update' }, new Date(T0 + 200_000).toISOString()))
    await clock.advance(120_000)
    expect(sink.prompts.length).toBe(1)
    expect(sink.prompts[0]!.includes('update')).toBe(true)
  })

  test('universe map: opens, shows blocked-on-you, a node can be messaged on the bus', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    const f = fake([])
    engine(on, f, sinkOf())
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    await $.command.run({ command: 'aicq', args: 'map' } as never)
    await clock.advance(10)
    type UI = { findAll: (q: unknown) => Promise<unknown[]>; press: (t: unknown) => Promise<unknown>; input: (t: unknown) => Promise<unknown> }
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'aicq', surface, component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as UI
      expect((await ui.findAll({ text: /1 blocked on you/ })).length).toBeGreaterThan(0)
      if (surface === 'desktop') expect((await ui.findAll({ type: 'Svg' })).length).toBeGreaterThan(0)
      await ui.press({ key: 'un-agent:coord-boss' })
      expect((await ui.findAll({ type: 'Button', text: /Help me unblock this/ })).length).toBe(1)
      await ui.input({ key: 'utell', text: 'Ash says: proceed with the V5 grant request' })
      await ui.press({ key: 'uback' })
      await $.command.run({ command: 'aicq', args: 'map' } as never)
    }
    const tells = f.calls.filter(c => String(c.argv[0]).endsWith('coord-engine') && c.argv[1] === 'tell')
    expect(tells.length).toBe(2)
    expect(tells[0]!.argv.slice(2, 4)).toEqual(['fulcra', 'coord-boss'])
  })

  test('universe: a thrown source read is named; readable sources still render', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    COORD_FAULTS.boardThrows = true
    try {
      engine(on, fake([]), sinkOf())
      await $.session.start({ cwd: '/tmp' } as never)
      await clock.advance(2000)
      await $.command.run({ command: 'aicq', args: 'map' } as never)
      await clock.advance(10)
      const ui = await $.ui.mount({ plugin: 'aicq', surface: 'terminal', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as { findAll: (q: unknown) => Promise<unknown[]> }
      expect((await ui.findAll({ text: /coord board \(failed or timed out\)/ })).length).toBeGreaterThan(0)
      expect((await ui.findAll({ type: 'Button', text: /coord-boss/ })).length).toBeGreaterThan(0)
    } finally {
      COORD_FAULTS.boardThrows = false
    }
  })

  test('universe: an unreadable V5 root is named, never an empty inventory', async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    on('fs.exists', () => ({ value: true }))
    on('fs.list', () => ({ deny: 'EACCES: permission denied' }))
    engine(on, fake([]), sinkOf())
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    await $.command.run({ command: 'aicq', args: 'map' } as never)
    await clock.advance(10)
    const ui = await $.ui.mount({ plugin: 'aicq', surface: 'terminal', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as { findAll: (q: unknown) => Promise<unknown[]> }
    expect((await ui.findAll({ text: /V5 actors \(~\/\.local\/share\/coord-v5 unreadable\)/ })).length).toBeGreaterThan(0)
  })

  test('universe: Refresh re-reads the workspace roster', { options: { workspaces: 'team' } }, async ($, on) => {
    const clock = mock.clock(on, { now: T0 })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    ROSTER.text = `Message channel: ${WS_CH}\n## Members\n- first-agent — joined\n`
    try {
      engine(on, fake([]), sinkOf())
      await $.session.start({ cwd: '/tmp' } as never)
      await clock.advance(2000)
      await $.command.run({ command: 'aicq', args: 'map' } as never)
      await clock.advance(10)
      type UI = { findAll: (q: unknown) => Promise<unknown[]>; press: (t: unknown) => Promise<unknown> }
      const ui = await $.ui.mount({ plugin: 'aicq', surface: 'terminal', component: 'Pane', requestId: 'aicq', props: {} } as never) as unknown as UI
      expect((await ui.findAll({ type: 'Button', text: /first-agent/ })).length).toBe(1)
      ROSTER.text = `Message channel: ${WS_CH}\n## Members\n- second-agent — joined\n`
      await ui.press({ key: 'urefresh' })
      expect((await ui.findAll({ type: 'Button', text: /second-agent/ })).length).toBe(1)
      expect((await ui.findAll({ type: 'Button', text: /first-agent/ })).length).toBe(0)
    } finally {
      ROSTER.text = ''
    }
  })

  for (const [first, second, verified] of [['lag', 'ok', true], ['trunc', 'ok', true], ['fail', 'ok', true], ['lag', 'lag', false]] as const) {
    test(`readback: first read ${first}, second ${second} -> ${verified ? 'verified' : 'UNVERIFIED'}; one write, two reads`, async ($, on) => {
      const clock = mock.clock(on, { now: T0 })
      mock.store(on)
      mock.env(on, { HOME: '/home/t' })
      const f = fake([])
      engine(on, f, sinkOf())
      await $.session.start({ cwd: '/tmp' } as never)
      await clock.advance(2000)
      READBACK.plan = [first, second]
      try {
        const before = f.calls.length
        const pending = $.tool.call({ tool: 'mcp__aicq__aicq_send', to: 'Peer Agent', topic: 'lagging', body: 'hello' } as never)
        await clock.advance(5000)
        const res = JSON.stringify(await pending)
        const after = f.calls.slice(before)
        expect(after.filter(c => c.argv[1] === 'record').length).toBe(1)
        expect(after.filter(c => c.argv[1] === 'get-records' && c.argv[2] === V1_OUT).length).toBe(2)
        expect(res.includes('Sent and read back')).toBe(verified)
        if (!verified) expect(res.includes('UNVERIFIED') && res.includes('a resend duplicates')).toBe(true)
      } finally {
        READBACK.plan = []
      }
    })
  }
})

