import { describe, expect, mock, test } from 'claude-code/testing'

import type { AicqMessage } from '../types'
import { collaborations, contextBlock, replyStats } from './collab'
import { modeOf, wakePrompt, withinBudget } from './policy'
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
    expect(collaborations([...msgs, msg({ id: '3', at: '2026-10-07T11:00:00Z', kind: 'reply', body: 'Done: v7 delivered' })], none)[0]!.state).toBe('completed')
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

function fake(inboxRows: Record<string, unknown>[], opts: { failRecords?: boolean } = {}) {
  const calls: Run[] = []
  const recorded = new Map<string, string[]>()
  const ok = (stdout: string) => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
  const answer = (r: Run) => {
    calls.push(r)
    const [, cmd, sub] = r.argv
    if (cmd === 'user-info') return ok(JSON.stringify({ userid: ME }))
    if (cmd === 'share' && sub === 'list-incoming') return ok(JSON.stringify({ sharing_fulcra_userid: PEER, sharing_fulcra_user_name: 'Peer Agent', fulcra_data_types: [V1_IN] }))
    if (cmd === 'share' && sub === 'list-outgoing') return ok(JSON.stringify({ created_at: '2026-10-01', fulcra_data_types: [V1_OUT], permissions: [{ allowed_fulcra_userid: PEER }] }))
    if (cmd === 'record') {
      const ch = String(r.argv[2])
      recorded.set(ch, [...(recorded.get(ch) ?? []), r.init?.stdin ?? ''])
      return ok('Upload ID: x')
    }
    if (cmd === 'get-records') {
      if (opts.failRecords) return { value: { exitCode: 1, stdout: '', stderr: 'boom', isStdoutTruncated: false, isStderrTruncated: false } }
      const ch = String(r.argv[2])
      if (ch === V1_IN) return ok(inboxRows.map(x => JSON.stringify(x)).join('\n'))
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
  test('backfill is quiet; arrivals notify; respond mode starts one guarded turn', async ($, on) => {
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
        expect((await ui.findAll({ type: 'Button', text: /Approve & send/ })).length).toBe(1)
        await ui.press({ key: 'dback' })
        await ui.press({ key: `ds-new-mesh:${PEER}` })
        expect((await ui.findAll({ type: 'Input', key: 'dstart' })).length).toBe(1)
        await ui.press({ key: 'dback' })
      }
    }
  })
})
