import { afterEach, expect, it } from 'vitest';
import { chmodSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import backlog from '../../../../tests/fixtures/work-backlog-synthetic.json';
import checkpoint from '../../../../tests/fixtures/work-handoff-synthetic.json';
import { buildHandoffPackage } from '../../gatekeeper/checkpoint.js';
import { serializeWorkEvent, workContentDigest } from '../../gatekeeper/work-contract.js';
import { assessHandoffReadiness } from '../../gatekeeper/handoff.js';
import { readWorkWindow } from './work-transport-read.js';
import { openWorkTransportStore } from './work-transport-store.js';
import { buildAuthorizedWorkView } from './work-view.js';

const id = (n) => `30000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const principal = '00000000-0000-4000-8000-000000000900';
const owned = (value) => JSON.parse(JSON.stringify(value).replaceAll('principal-a', principal));
const history = owned([...backlog.events, ...backlog.replay_events]);
history[1].payload.item.revisit = { at: '2026-09-26T08:00:00.000Z', condition: null };
const base = history[0];
const sender = base.actor;
const receiver = { ...sender, logical_agent_id: 'receiver', instance_id: 'receiver-instance' };
const time = '2026-09-27T12:00:00.000Z';
const until = '2026-09-27T13:00:00.000Z';
const config = {
  baseUrl: 'https://api.fulcradynamics.com/',
  principalId: sender.principal_id,
  channel: `MomentAnnotation/${base.stream_id}`,
  workspaceId: base.workspace_id,
  workstreamId: base.workstream_id,
  actorBinding: sender
};
const policy = {
  principal_id: sender.principal_id,
  workspace_id: base.workspace_id,
  stream_id: base.stream_id,
  grants: [
    { ...sender, capabilities: ['work.write', 'assignment.manage', 'assignment.accept',
      'question.ask', 'question.answer', 'question.apply', 'checkpoint.publish', 'handoff.offer'] },
    { ...receiver, capabilities: ['handoff.ready', 'assignment.accept'] }
  ],
  work_jobs: []
};
const dirs = [];
const handles = [];
function open(dbPath) {
  const handle = openWorkTransportStore({ dbPath, config });
  handles.push(handle);
  return handle;
}
function path() {
  const dir = mkdtempSync(join(tmpdir(), 'handoff-receipts-'));
  chmodSync(dir, 0o700);
  dirs.push(dir);
  return join(dir, 'work.sqlite');
}
function event(n, kind, payload, parents, actor = sender) {
  return {
    ...structuredClone(base),
    event_id: id(n), operation_id: id(n + 1000), kind,
    subject: { type: 'handoff', id: id(10) }, actor,
    occurred_at: time, parents: parents.map((item) => item.event_id), payload
  };
}
function fixture() {
  const body = owned(checkpoint);
  const publication = event(1, 'checkpoint.published', {
    work_id: body.work_id, checkpoint_id: body.checkpoint_id,
    artifact: {
      id: id(2), uri: 'https://example.invalid/checkpoint', sha256: workContentDigest(body),
      version: null, media_type: 'application/json', owner_principal_id: 'artifact-owner',
      audience: 'workspace', portable: true
    },
    body_digest: workContentDigest(body), assignment_version: body.assignment_version,
    assignment_event_id: body.assignment_event_id
  }, [history[5]]);
  publication.subject = { type: 'checkpoint', id: body.checkpoint_id };
  const exploratory = {
    ...structuredClone(base), event_id: id(40), operation_id: id(1040),
    subject: { type: 'work', id: id(41) },
    payload: { item: { ...structuredClone(history[1].payload.item), type: 'open_thread',
      status: 'open', title: 'Explore recovery edge cases', next_action: 'Inspect edge cases', revisit: null } }
  };
  const events = [...structuredClone(history), exploratory, publication];
  const observation = {
    coverage: 'complete', as_of: time, last_successful_observation_at: time,
    sources: [{ stream_id: base.stream_id, status: 'complete', pending_pages: 0 }],
    gaps: [], errors: [], completeness_evidence_id: 'synthetic-complete'
  };
  const trust = {
    workspace_id: base.workspace_id, allowed_stream_ids: [base.stream_id],
    grants: policy.grants,
    event_evidence: events.map((e, n) => ({ event_id: e.event_id,
      event_digest: workContentDigest(e), record_id: `synthetic-${n}`,
      source_principal_id: e.actor.principal_id, stream_id: e.stream_id, received_at: time }))
  };
  const built = buildHandoffPackage({ checkpoint: body, publicationEvent: publication,
    events, trust, observation, recipient: receiver,
    accessRequirements: [{ resource_id: id(2), scope_id: id(3), action: 'read' }], asOf: time });
  expect(built.ok, JSON.stringify(built)).toBe(true);
  const pkg = built.package;
  const packageDigest = workContentDigest(pkg);
  const offer = event(4, 'handoff.offered', {
    work_id: body.work_id, expected_assignment_event_id: body.assignment_event_id,
    expected_version: body.assignment_version, checkpoint_event_id: publication.event_id,
    package_digest: packageDigest, target: receiver
  }, [publication, history[5]]);
  const ready = event(5, 'handoff.ready', {
    offer_event_id: offer.event_id, package_digest: packageDigest,
    verification_receipt_id: 'receipt-pub'
  }, [offer], receiver);
  const receipt = {
    schema: 'handoff-verification/1', principal_id: sender.principal_id,
    workspace_id: base.workspace_id, workstream_id: base.workstream_id,
    stream_id: base.stream_id, package: pkg,
    verification: {
      ready_event_id: ready.event_id, ready_event_digest: workContentDigest(ready),
      offer_event_id: offer.event_id, package_digest: packageDigest,
      checks: {
        checked_at: time, valid_until: until, receiver,
        package_digest: packageDigest,
        publication: { event_id: publication.event_id, artifact_id: id(2),
          body_digest: publication.payload.body_digest, status: 'verified', receipt_id: 'receipt-pub' },
        resources: [{ resource_id: id(2), scope_id: id(3), action: 'read',
          status: 'verified', receipt_id: 'receipt-access' }]
      }
    }
  };
  return { events: [...events, offer, ready], receipt, offer, ready, publication };
}
async function append(store, events) {
  const stream = base.stream_id;
  const source = `com.fulcradynamics.annotation.${stream}`;
  const metadata = { id: stream, fulcra_userid: sender.principal_id,
    annotation_type: 'moment', fulcra_source_id: source, deleted_at: null };
  const rows = events.map((e, n) => ({ id: id(n + 30000), source_id: source,
    metadata, note: serializeWorkEvent(e) }));
  const fetch = async (url) => new Response(JSON.stringify(
    String(url).includes('/info') ? { userid: sender.principal_id } :
      String(url).includes('/catalog') ? [{ id: config.channel, api_version: 'v1alpha1',
        recordable: true, queryable: true, record_spec: { type: 'event' }, fulcra_userid: sender.principal_id }] :
        String(url).includes('/annotation?') ? [metadata] : rows
  ), { headers: { 'content-type': 'application/json' } });
  const read = await readWorkWindow({ fetch, token: 'synthetic', config,
    start: '2026-09-26T00:00:00Z', end: '2026-09-27T00:00:00Z',
    now: () => Date.parse(time) });
  expect(store.appendWindow(read).status).toBe('stored');
}
function expectPredecessorOwnership(store, workId, now) {
  const view = buildAuthorizedWorkView({ store, policy, now: () => now });
  expect(view.status).toBe('ready');
  const row = view.projection.work.find((candidate) => candidate.work_id === workId);
  expect(row.assignment).toMatchObject({
    version: 1, owner_id: sender.logical_agent_id, accepted_actor: sender, state: 'accepted'
  });
  expect(row.assignment.accepted_actor).not.toEqual(receiver);
  expect(view.projection.handoffs.every((handoff) => handoff.accepted_event === null)).toBe(true);
  return view;
}
afterEach(() => {
  for (const handle of handles.splice(0)) handle.close();
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

it('retains a valid scoped receipt across reopen without upgrading partial coverage', async () => {
  const dbPath = path(), store = open(dbPath), { events, receipt, ready } = fixture();
  await append(store, events);
  expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 })).toEqual({ status: 'stored' });
  store.close(); handles.splice(handles.indexOf(store), 1);
  const reopened = open(dbPath);
  expect(reopened.handoffVerifications({ now: Date.parse(time) + 2000 })).toMatchObject({
    status: 'ready', verifications: [receipt.verification]
  });
  const view = buildAuthorizedWorkView({ store: reopened, policy, now: () => Date.parse(time) + 2000 });
  expect(view.status).toBe('ready');
  expect(view.projection.observation.coverage).toBe('partial');
  expect(view.projection.handoffs[0].ready_event.event_id).toBe(ready.event_id);
  expect(view.projection.handoffs[0].state).toBe('ready');
  const row = view.projection.work.find((candidate) => candidate.work_id === receipt.package.work_id);
  expect(row.provisional).toBe(true);
  expect(view.projection.handoffs[0].provisional).toBe(true);
  expectPredecessorOwnership(reopened, receipt.package.work_id, Date.parse(time) + 2000);
  const assessment = assessHandoffReadiness({
    package: receipt.package, projection: view.projection, receiver,
    checks: receipt.verification.checks, asOf: view.projection.as_of
  });
  expect(assessment.status).toBe('blocked');
  expect(assessment.errors).toContain('PROJECTION_INCOMPLETE');
});

it('rejects duplicate conflicts and every scope, binding, and resource mismatch without changing saved proof', async () => {
  const store = open(path()), { events, receipt } = fixture();
  await append(store, events);
  const now = Date.parse(time) + 1000;
  expect(store.importHandoffVerification(receipt, { now })).toEqual({ status: 'stored' });
  expect(store.importHandoffVerification(structuredClone(receipt), { now })).toEqual({ status: 'same' });
  const conflicting = structuredClone(receipt);
  conflicting.verification.checks.checked_at = '2026-09-27T11:59:00.000Z';
  expect(store.importHandoffVerification(conflicting, { now })).toEqual({
    status: 'blocked', code: 'VERIFICATION_CONFLICT'
  });
  const bad = [
    ['scope', 'INVALID_VERIFICATION', (r) => { r.workspace_id = id(700); }],
    ['ready digest', 'INVALID_VERIFICATION', (r) => { r.verification.ready_event_digest = 'a'.repeat(64); }],
    ['offer', 'INVALID_VERIFICATION', (r) => { r.verification.offer_event_id = id(701); }],
    ['package', 'INVALID_VERIFICATION', (r) => { r.verification.package_digest = 'b'.repeat(64); }],
    ['receiver', 'INVALID_VERIFICATION', (r) => { r.verification.checks.receiver.logical_agent_id = 'wrong'; }],
    ['publication', 'INVALID_VERIFICATION', (r) => { r.verification.checks.publication.event_id = id(702); }],
    ['resources', 'INVALID_VERIFICATION', (r) => { r.verification.checks.resources = []; }]
  ];
  const predecessor = expectPredecessorOwnership(store, receipt.package.work_id, now).projection.work
    .find((candidate) => candidate.work_id === receipt.package.work_id).assignment;
  for (const [name, code, change] of bad) {
    const changed = structuredClone(receipt); change(changed);
    expect(store.importHandoffVerification(changed, { now }), name).toEqual({ status: 'blocked', code });
    const after = expectPredecessorOwnership(store, receipt.package.work_id, now).projection.work
      .find((candidate) => candidate.work_id === receipt.package.work_id).assignment;
    expect(after, name).toEqual(predecessor);
  }
  expect(store.handoffVerifications({ now }).verifications).toEqual([receipt.verification]);
});

it('requires the exact authenticated ready, offer, and publication events', async () => {
  const { events, receipt, ready, offer, publication } = fixture();
  for (const missing of [ready.event_id, offer.event_id, publication.event_id]) {
    const store = open(path());
    await append(store, events.filter((event) => event.event_id !== missing));
    expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 }), missing)
      .toEqual({ status: 'blocked', code: 'INVALID_VERIFICATION' });
    expect(store.handoffVerifications({ now: Date.parse(time) + 1000 }).verifications).toEqual([]);
    expectPredecessorOwnership(store, receipt.package.work_id, Date.parse(time) + 1000);
  }
});

it('rejects an authenticated offer that names a different work item than the package', async () => {
  const store = open(path()), { events, receipt, offer } = fixture();
  const changed = events.map((entry) => entry.event_id === offer.event_id
    ? { ...entry, payload: { ...entry.payload, work_id: id(998) } } : entry);
  await append(store, changed);
  expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 }))
    .toEqual({ status: 'blocked', code: 'INVALID_VERIFICATION' });
  expectPredecessorOwnership(store, receipt.package.work_id, Date.parse(time) + 1000);
});

it('migrates the legacy private database in place without erasing observations', async () => {
  const dbPath = path(), store = open(dbPath);
  await append(store, [history[0]]);
  store.close(); handles.splice(handles.indexOf(store), 1);
  const db = new DatabaseSync(dbPath);
  db.exec('DROP TABLE handoff_verifications');
  db.exec('PRAGMA user_version = 1');
  db.close();
  const reopened = open(dbPath);
  expect(reopened.accumulated().events).toHaveLength(1);
  expect(reopened.accumulated().observation.coverage).toBe('partial');
  expect(reopened.handoffVerifications({ now: Date.parse(time) }).verifications).toEqual([]);
});

it('expires pending readiness at the current clock without erasing retained proof', async () => {
  const store = open(path()), { events, receipt } = fixture();
  await append(store, events);
  expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 })).toEqual({ status: 'stored' });
  const future = Date.parse(until) + 1000;
  expect(store.handoffVerifications({ now: future })).toMatchObject({
    status: 'ready', inactive_ready_event_ids: [receipt.verification.ready_event_id],
    verifications: [receipt.verification]
  });
  const view = buildAuthorizedWorkView({ store, policy, now: () => future });
  expect(view.projection.handoffs[0].state).not.toBe('ready');
  expect(view.projection.handoffs[0].accepted_event).toBeNull();
});

it('does not treat a replay-rejected acceptance as historical authority after expiry', async () => {
  const store = open(path()), { events, receipt, offer, ready } = fixture();
  const bogus = event(6, 'assignment.accepted', {
    work_id: id(999), offer_event_id: offer.event_id,
    expected_assignment_event_id: offer.payload.expected_assignment_event_id,
    expected_version: offer.payload.expected_version, ready_event_id: ready.event_id
  }, [offer, ready, events.find((item) => item.event_id === offer.payload.expected_assignment_event_id)], receiver);
  bogus.subject = { type: 'work', id: id(999) };
  await append(store, [...events, bogus]);
  expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 })).toEqual({ status: 'stored' });
  const view = buildAuthorizedWorkView({ store, policy, now: () => Date.parse(until) + 1000 });
  expect(view.projection.handoffs[0].accepted_event).toBeNull();
  expect(view.projection.handoffs[0].state).not.toBe('ready');
});

it('keeps replay-accepted ownership after the receipt expires', async () => {
  const store = open(path()), { events, receipt, offer, ready } = fixture();
  const accepted = event(6, 'assignment.accepted', {
    work_id: offer.payload.work_id, offer_event_id: offer.event_id,
    expected_assignment_event_id: offer.payload.expected_assignment_event_id,
    expected_version: offer.payload.expected_version, ready_event_id: ready.event_id
  }, [offer, ready, events.find((item) => item.event_id === offer.payload.expected_assignment_event_id)], receiver);
  accepted.subject = { type: 'work', id: offer.payload.work_id };
  await append(store, [...events, accepted]);
  expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 })).toEqual({ status: 'stored' });
  const view = buildAuthorizedWorkView({ store, policy, now: () => Date.parse(until) + 1000 });
  expect(view.projection.handoffs[0].state).toBe('accepted');
  expect(view.projection.handoffs[0].accepted_event.event_id).toBe(accepted.event_id);
  expect(view.projection.work.find((row) => row.work_id === offer.payload.work_id).assignment.accepted_actor)
    .toEqual(receiver);
});

it('keeps the later accepted ready proof after expiry while the unrelated earlier ready expires', async () => {
  const store = open(path()), { events, receipt, offer, ready } = fixture();
  const laterReady = event(7, 'handoff.ready', structuredClone(ready.payload), [offer], receiver);
  const laterReceipt = structuredClone(receipt);
  laterReceipt.verification.ready_event_id = laterReady.event_id;
  laterReceipt.verification.ready_event_digest = workContentDigest(laterReady);
  const accepted = event(8, 'assignment.accepted', {
    work_id: offer.payload.work_id, offer_event_id: offer.event_id,
    expected_assignment_event_id: offer.payload.expected_assignment_event_id,
    expected_version: offer.payload.expected_version, ready_event_id: laterReady.event_id
  }, [offer, laterReady, events.find((item) => item.event_id === offer.payload.expected_assignment_event_id)], receiver);
  accepted.subject = { type: 'work', id: offer.payload.work_id };
  await append(store, [...events, laterReady, accepted]);
  const importAt = Date.parse(time) + 1000;
  expect(store.importHandoffVerification(receipt, { now: importAt })).toEqual({ status: 'stored' });
  expect(store.importHandoffVerification(laterReceipt, { now: importAt })).toEqual({ status: 'stored' });
  const before = buildAuthorizedWorkView({ store, policy, now: () => importAt });
  expect(before.projection.handoffs[0].ready_event.event_id).toBe(ready.event_id);
  expect(before.projection.work.find((row) => row.work_id === offer.payload.work_id).assignment.version).toBe(2);
  const after = buildAuthorizedWorkView({ store, policy, now: () => Date.parse(until) + 1000 });
  expect(after.status).toBe('ready');
  expect(after.projection.handoffs[0].accepted_event.event_id).toBe(accepted.event_id);
  expect(after.projection.handoffs[0].ready_event.event_id).toBe(laterReady.event_id);
  expect(after.projection.work.find((row) => row.work_id === offer.payload.work_id).assignment)
    .toMatchObject({ version: 2, owner_id: receiver.logical_agent_id, accepted_actor: receiver });
});

it('keeps an expired ready proof that is a causal parent of later accepted readiness', async () => {
  const store = open(path()), { events, receipt, offer, ready } = fixture();
  const laterReady = event(7, 'handoff.ready', structuredClone(ready.payload), [offer, ready], receiver);
  const laterReceipt = structuredClone(receipt);
  laterReceipt.verification.ready_event_id = laterReady.event_id;
  laterReceipt.verification.ready_event_digest = workContentDigest(laterReady);
  const laterUntil = '2026-09-27T14:00:00.000Z';
  laterReceipt.verification.checks.valid_until = laterUntil;
  const accepted = event(8, 'assignment.accepted', {
    work_id: offer.payload.work_id, offer_event_id: offer.event_id,
    expected_assignment_event_id: offer.payload.expected_assignment_event_id,
    expected_version: offer.payload.expected_version, ready_event_id: laterReady.event_id
  }, [offer, laterReady, events.find((item) => item.event_id === offer.payload.expected_assignment_event_id)], receiver);
  accepted.subject = { type: 'work', id: offer.payload.work_id };
  await append(store, [...events, laterReady, accepted]);
  const importAt = Date.parse(time) + 1000;
  expect(store.importHandoffVerification(receipt, { now: importAt })).toEqual({ status: 'stored' });
  expect(store.importHandoffVerification(laterReceipt, { now: importAt })).toEqual({ status: 'stored' });
  for (const [at, expectedState] of [
    [importAt, 'valid'],
    [Date.parse(until) + 1000, 'valid'],
    [Date.parse(laterUntil) + 1000, 'expired']
  ]) {
    const view = buildAuthorizedWorkView({ store, policy, now: () => at });
    expect(view.status).toBe('ready');
    const handoff = view.projection.handoffs[0];
    expect(handoff.state).toBe('accepted');
    expect(handoff.accepted_event.event_id).toBe(accepted.event_id);
    expect(handoff.verification).toEqual({
      state: expectedState, valid_until: laterUntil, ready_event_id: laterReady.event_id
    });
    expect(view.projection.work.find((row) => row.work_id === offer.payload.work_id).assignment)
      .toMatchObject({ version: 2, owner_id: receiver.logical_agent_id, accepted_actor: receiver });
  }
});

it('accepts identical authenticated source copies of each referenced event before import and after reopen', async () => {
  const { events, receipt, ready, offer, publication } = fixture();
  for (const referenced of [ready, offer, publication]) {
    const dbPath = path(), store = open(dbPath);
    await append(store, [...events, structuredClone(referenced)]);
    expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 }), referenced.kind)
      .toEqual({ status: 'stored' });
    store.close(); handles.splice(handles.indexOf(store), 1);
    const reopened = open(dbPath);
    expect(reopened.handoffVerifications({ now: Date.parse(time) + 2000 }).status).toBe('ready');
    const beforeView = buildAuthorizedWorkView({ store: reopened, policy, now: () => Date.parse(time) + 2000 });
    expect(beforeView.status).toBe('ready');
    expect(beforeView.projection.handoffs[0].ready_event.event_id).toBe(ready.event_id);
    const dbPathLater = path(), later = open(dbPathLater);
    await append(later, events);
    expect(later.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 })).toEqual({ status: 'stored' });
    await append(later, [...events, structuredClone(referenced)]);
    later.close(); handles.splice(handles.indexOf(later), 1);
    const reopenedLater = open(dbPathLater);
    expect(reopenedLater.handoffVerifications({ now: Date.parse(time) + 2000 }).status, referenced.kind)
      .toBe('ready');
    const afterView = buildAuthorizedWorkView({ store: reopenedLater, policy, now: () => Date.parse(time) + 2000 });
    expect(afterView.status).toBe('ready');
    expect(afterView.projection.handoffs[0].ready_event.event_id).toBe(ready.event_id);
  }
});

it('fails closed when a referenced ID has a differing authenticated source variant', async () => {
  const { events, receipt, ready, offer, publication } = fixture();
  const variants = [
    [ready, (e) => { e.payload.verification_receipt_id = 'different'; }],
    [offer, (e) => { e.payload.work_id = id(998); }],
    [publication, (e) => { e.payload.artifact.uri = 'https://example.invalid/different'; }]
  ];
  for (const [referenced, change] of variants) {
    const changed = structuredClone(referenced); change(changed);
    const store = open(path());
    await append(store, [...events, changed]);
    expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 }), referenced.kind)
      .toEqual({ status: 'blocked', code: 'INVALID_VERIFICATION' });
    const afterImport = open(path());
    await append(afterImport, events);
    expect(afterImport.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 }))
      .toEqual({ status: 'stored' });
    await append(afterImport, [...events, changed]);
    expect(afterImport.handoffVerifications({ now: Date.parse(time) + 2000 }))
      .toMatchObject({ status: 'unavailable', code: 'STORE_CORRUPT' });
  }
});

it('requires referenced retained events, rejects expired and malformed receipts, and makes tampering unavailable', async () => {
  const dbPath = path(), store = open(dbPath), { events, receipt } = fixture();
  expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 }))
    .toEqual({ status: 'blocked', code: 'INVALID_VERIFICATION' });
  await append(store, events);
  expect(store.importHandoffVerification(receipt, { now: Date.parse(until) }))
    .toEqual({ status: 'blocked', code: 'VERIFICATION_EXPIRED' });
  expectPredecessorOwnership(store, receipt.package.work_id, Date.parse(until));
  expect(store.importHandoffVerification({ ...receipt, extra: 'secret-sentinel' }, { now: Date.parse(time) + 1000 }))
    .toEqual({ status: 'blocked', code: 'INVALID_VERIFICATION' });
  expectPredecessorOwnership(store, receipt.package.work_id, Date.parse(time) + 1000);
  expect(store.importHandoffVerification(receipt, { now: Date.parse(time) + 1000 })).toEqual({ status: 'stored' });
  const db = new DatabaseSync(dbPath);
  db.exec("UPDATE handoff_verifications SET receipt_json='tampered'");
  db.close();
  expect(store.handoffVerifications({ now: Date.parse(time) + 2000 })).toMatchObject({ status: 'unavailable' });
  expect(buildAuthorizedWorkView({ store, policy, now: () => Date.parse(time) + 2000 })).toMatchObject({ status: 'unavailable' });
});
