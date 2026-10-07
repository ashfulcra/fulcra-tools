import { describe, expect, mock, test } from 'claude-code/testing'

import {
  EMPTY_CURSOR, advance, isWakeWorthy, meshNote, parseJsonl, parseMeshRow, parsePeers,
  outboxFor, parseWorkspaceChannel, parseWorkspaceRow, readbackHas, windowStart, workspaceNote,
} from './wire'
import type { MeshPeer } from './wire'

const ME = '00000000-0000-4000-8000-0000000000a1'
const PEER: MeshPeer = { userId: '00000000-0000-4000-8000-0000000000b2', name: 'Peer Agent', channel: 'MomentAnnotation/00000000-0000-4000-8000-0000000000c3' }
const WS_CH = 'MomentAnnotation/00000000-0000-4000-8000-0000000000d4'

const meshRow = (env: Record<string, unknown>, at = '2026-10-07T12:00:00Z') => ({ id: 'r1', recorded_at: at, note: JSON.stringify(env) })
const wsRow = (coord: Record<string, unknown>) => ({ id: 'r2', recorded_at: '2026-10-07T12:00:00Z', note: JSON.stringify({ coord }) })

describe('wire', () => {
  test('parses a mesh envelope addressed to me', () => {
    const p = parseMeshRow(meshRow({ v: 1, mid: 'm1', to: 'ash', to_user: ME, kind: 'directive', slug: 'review', body: 'hi' }), PEER, ME, '')
    expect('message' in p && p.message.id).toBe('m1')
    expect('message' in p && p.message.contact).toBe('Peer Agent')
  })

  test('drops mesh messages for another user, and malformed notes', () => {
    expect(parseMeshRow(meshRow({ v: 1, mid: 'm2', to_user: 'someone-else', body: 'x' }), PEER, ME, '')).toEqual({ skip: 'not-for-me' })
    expect(parseMeshRow({ id: 'r', note: '{not json' }, PEER, ME, '')).toEqual({ skip: 'malformed' })
    expect(parseMeshRow({ id: 'r', note: null }, PEER, ME, '')).toEqual({ skip: 'malformed' })
  })

  test('workspace: keeps messages for my agent name, drops my own and others’', () => {
    const base = { protocol: 'fulcra.workspaces/1', message_id: 'w1', workspace: 'w', sender: 'tycho', recipients: ['aicq'], topic: 't', body: 'b', sent_at: '2026-10-07T12:00:00Z' }
    expect('message' in parseWorkspaceRow(wsRow(base), 'w', WS_CH, 'aicq')).toBe(true)
    expect(parseWorkspaceRow(wsRow({ ...base, recipients: ['codex-coder'] }), 'w', WS_CH, 'aicq')).toEqual({ skip: 'not-for-me' })
    expect(parseWorkspaceRow(wsRow({ ...base, sender: 'aicq' }), 'w', WS_CH, 'aicq')).toEqual({ skip: 'mine' })
    expect('message' in parseWorkspaceRow(wsRow({ ...base, recipients: ['all'] }), 'w', WS_CH, 'aicq')).toBe(true)
  })

  test('cursor dedupes and advances the watermark', () => {
    const p = parseMeshRow(meshRow({ mid: 'm1', to_user: ME, body: 'a' }, '2026-10-07T12:00:00Z'), PEER, ME, '')
    const q = parseMeshRow(meshRow({ mid: 'm3', to_user: ME, body: 'b' }, '2026-10-07T13:00:00Z'), PEER, ME, '')
    if (!('message' in p) || !('message' in q)) throw new Error('parse')
    const one = advance(EMPTY_CURSOR, [p.message])
    expect(one.fresh.length).toBe(1)
    const two = advance(one.cursor, [p.message, q.message])
    expect(two.fresh.map(m => m.id)).toEqual(['m3'])
    expect(two.cursor.at).toBe('2026-10-07T13:00:00Z')
  })

  test('heartbeats and acks never wake', () => {
    const mk = (kind: string, topic: string) => ({ id: 'x', source: 'mesh' as const, channel: '', contact: '', contactUserId: null, workspace: null, to: '', kind, topic, body: '', at: '' })
    expect(isWakeWorthy(mk('heartbeat', 't'))).toBe(false)
    expect(isWakeWorthy(mk('response', 'review-ack'))).toBe(false)
    expect(isWakeWorthy(mk('directive', 'review'))).toBe(true)
  })

  test('peers come from MomentAnnotation shares only, never my own', () => {
    const out = [
      { sharing_fulcra_userid: PEER.userId, sharing_fulcra_user_name: 'Peer Agent', fulcra_data_types: [PEER.channel] },
      { sharing_fulcra_userid: 'b', sharing_fulcra_user_name: 'b', fulcra_data_types: ['HeartRate'] },
      { sharing_fulcra_userid: ME, fulcra_data_types: ['MomentAnnotation/aaaa'] },
    ].map(r => JSON.stringify(r)).join('\n')
    expect(parsePeers(out, ME)).toEqual([PEER])
  })

  test('outbox resolves per peer, newest share wins', () => {
    const rows = [
      { created_at: '2026-01-01', fulcra_data_types: ['MomentAnnotation/old'], permissions: [{ allowed_fulcra_userid: 'p' }] },
      { created_at: '2026-09-01', fulcra_data_types: ['MomentAnnotation/new'], permissions: [{ allowed_fulcra_userid: 'p' }] },
      { created_at: '2026-10-01', fulcra_data_types: ['MomentAnnotation/other'], permissions: [{ allowed_fulcra_userid: 'q' }] },
    ].map(r => JSON.stringify(r)).join('\n')
    expect(outboxFor(rows, 'p')).toBe('MomentAnnotation/new')
    expect(outboxFor(rows, 'z')).toBe(null)
  })

  test('workspace index, envelopes, readback, window', () => {
    expect(parseWorkspaceChannel(`Message channel: ${WS_CH}\n`)).toBe(WS_CH)
    const mesh = JSON.parse(meshNote({ mid: 'id1', to: 'x', toUser: 'u', slug: 's', body: 'b' })) as { note: string }
    expect(JSON.parse(mesh.note)).toEqual(expect.objectContaining({ v: 1, mid: 'id1', to_user: 'u' }))
    const ws = JSON.parse(workspaceNote({ id: 'id2', workspace: 'w', sender: 'a', recipients: ['b'], topic: 't', body: 'b', sentAt: 'now', inReplyTo: 'p' })) as { note: string }
    expect((JSON.parse(ws.note) as { coord: Record<string, unknown> }).coord).toEqual(expect.objectContaining({ protocol: 'fulcra.workspaces/1', kind: 'reply', in_reply_to: 'p' }))
    expect(readbackHas(JSON.stringify({ note: mesh.note }), 'id1')).toBe(true)
    expect(parseJsonl('{"a":1}\nnope\n').bad).toBe(1)
    expect(windowStart({ at: '2026-10-07T12:00:00.000Z', seen: [] }, 0)).toBe('2026-10-07T11:50:00.000Z')
  })
})

type Run = { argv: readonly string[]; init?: { stdin?: string } }

function fakeFulcra(inbox: string[], opts: { failRecords?: boolean } = {}) {
  const calls: Run[] = []
  const recorded: string[] = []
  const answer = (r: Run) => {
    calls.push(r)
    const [, cmd, sub] = r.argv
    const ok = (stdout: string) => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
    if (cmd === 'user-info') return ok(JSON.stringify({ userid: ME }))
    if (cmd === 'share' && sub === 'list-outgoing') return ok(JSON.stringify({ created_at: '2026-10-01', fulcra_data_types: ['MomentAnnotation/22222222-2222-2222-2222-222222222222'], permissions: [{ allowed_fulcra_userid: PEER.userId }] }))
    if (cmd === 'share') return ok(JSON.stringify({ sharing_fulcra_userid: PEER.userId, sharing_fulcra_user_name: PEER.name, fulcra_data_types: [PEER.channel] }))
    if (cmd === 'record') {
      recorded.push((JSON.parse(r.init?.stdin ?? '{}') as { note: string }).note)
      return ok('Upload ID: x')
    }
    if (cmd === 'get-records') {
      if (opts.failRecords) return { value: { exitCode: 1, stdout: '', stderr: 'boom', isStdoutTruncated: false, isStderrTruncated: false } }
      const rows = r.argv.includes('--user-id') ? inbox : recorded.map(note => JSON.stringify({ id: 'r', note }))
      return ok(rows.join('\n'))
    }
    return { value: { exitCode: 2, stdout: '', stderr: `unknown ${sub ?? ''}`, isStdoutTruncated: false, isStderrTruncated: false } }
  }
  return { calls, recorded, answer }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function engine(on: any) {
  on('session.start', () => ({ cwd: '/tmp' }))
  on('command.register', () => ({ value: undefined }))
  on('tool.register', (_$: unknown, e: { name: string }) => ({ value: { tool: `mcp__aicq__${e.name}` } }))
  on('ui.open', () => ({ value: undefined }))
}

describe('mod', () => {
  test('first poll backfills quietly; a later arrival notifies, and inbox reports it', async ($, on) => {
    const clock = mock.clock(on, { now: Date.parse('2026-10-07T12:00:00Z') })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    engine(on)
    const inbox = [JSON.stringify(meshRow({ v: 1, mid: 'old', to_user: ME, kind: 'directive', slug: 'a', body: 'old one' }, '2026-10-07T11:00:00Z'))]
    const f = fakeFulcra(inbox)
    on('process.run', (_$, e) => f.answer(e as Run))
    const toasts: string[] = []
    on('ui.toast', (_$, e) => { toasts.push(String((e as { text: string }).text)); return { value: undefined } })
    const prompts: string[] = []
    on('prompt.submit', (_$, e) => { prompts.push(e.text); return { text: e.text } })

    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    expect(f.calls[0]?.argv[0]).toBe('/home/t/.local/bin/fulcra')
    expect(toasts.length).toBe(0)

    inbox.push(JSON.stringify(meshRow({ v: 1, mid: 'new', to_user: ME, kind: 'directive', slug: 'b', body: 'new one' }, '2026-10-07T12:01:00Z')))
    await clock.advance(120_000)
    expect(toasts.length).toBe(1)
    expect(prompts.length).toBe(0)

    const res = await $.tool.call({ tool: 'mcp__aicq__aicq_inbox' } as never)
    const text = JSON.stringify(res)
    expect(text.includes('new one') && text.includes('old one')).toBe(true)
  })

  test('wake mode submits a prompt for a real message', { options: { onArrival: 'wake' } }, async ($, on) => {
    const clock = mock.clock(on, { now: Date.parse('2026-10-07T12:00:00Z') })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    engine(on)
    const inbox: string[] = []
    const f = fakeFulcra(inbox)
    on('process.run', (_$, e) => f.answer(e as Run))
    on('ui.toast', () => ({ value: undefined }))
    const prompts: string[] = []
    on('prompt.submit', (_$, e) => { prompts.push(e.text); return { text: e.text } })

    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    inbox.push(JSON.stringify(meshRow({ v: 1, mid: 'hb', to_user: ME, kind: 'heartbeat', slug: 'hb', body: 'alive' }, '2026-10-07T12:01:00Z')))
    await clock.advance(120_000)
    expect(prompts.length).toBe(0)
    inbox.push(JSON.stringify(meshRow({ v: 1, mid: 'q', to_user: ME, kind: 'directive', slug: 'review', body: 'please review' }, '2026-10-07T12:03:00Z')))
    await clock.advance(120_000)
    expect(prompts.length).toBe(1)
    expect(prompts[0]?.includes('please review')).toBe(true)
  })

  test('a failed read is degraded, never an empty all-clear', async ($, on) => {
    const clock = mock.clock(on, { now: Date.parse('2026-10-07T12:00:00Z') })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    engine(on)
    const f = fakeFulcra([], { failRecords: true })
    on('process.run', (_$, e) => f.answer(e as Run))
    const statuses: string[] = []
    on('ui.status', (_$, e) => { statuses.push(String((e as { text?: string }).text)); return { value: undefined } })
    await $.session.start({ cwd: '/tmp' } as never)
    await clock.advance(2000)
    expect(statuses.some(t => t.includes('check failed (Peer Agent)'))).toBe(true)
  })

  test('mesh send uses the outbox shared with that peer and verifies by readback', async ($, on) => {
    mock.clock(on, { now: Date.parse('2026-10-07T12:00:00Z') })
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    engine(on)
    const f = fakeFulcra([])
    on('process.run', (_$, e) => f.answer(e as Run))
    await $.session.start({ cwd: '/tmp' } as never)
    const res = await $.tool.call({ tool: 'mcp__aicq__aicq_send', source: 'mesh', to: 'michael', to_user: PEER.userId, topic: 'hello', body: 'hi' } as never)
    expect(JSON.stringify(res).includes('Sent and read back')).toBe(true)
    expect(f.recorded.length).toBe(1)
    expect(f.calls.some(c => c.argv[1] === 'record' && c.argv[2] === 'MomentAnnotation/22222222-2222-2222-2222-222222222222')).toBe(true)
  })

  test('pane draws on terminal and desktop', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    mock.env(on, { HOME: '/home/t' })
    engine(on)
    on('process.run', () => ({ value: { exitCode: 1, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'aicq', surface, component: 'Pane', requestId: 'aicq', props: {} } as never)
      expect(JSON.stringify(await (ui as { findAll: (q: unknown) => Promise<unknown[]> }).findAll({ text: /Friends Agents/ })).length > 2).toBe(true)
    }
  })
})
