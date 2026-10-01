import { createHash, randomUUID } from 'node:crypto';
import { executeClaude, validateClaudeExecutor } from './listener-claude.js';
import { advanceListenerPolicy, evaluateWake } from '../../gatekeeper/listener.js';
import {
  correlation,
  observation as validObservation,
  routes as validRoutes
} from './listener-validation.js';

/** @typedef {ReturnType<typeof import('./listener-store.js').openListenerStore>} Store */
/** @typedef {Parameters<Store['transact']>[0]} Lease */
/** @typedef {{jobId:string,itemId:string,revision:string,kind?:string}} Item */

/** @param {Item} item */
const itemKey = (item) => JSON.stringify([item.jobId, item.itemId]);
/** @param {Item[]} items */
const sortItems = (items) =>
  items.sort(
    (a, b) =>
      a.jobId.localeCompare(b.jobId) ||
      a.itemId.localeCompare(b.itemId) ||
      a.revision.localeCompare(b.revision)
  );
/** @param {Record<string,any>|undefined} a @param {Record<string,any>|undefined} b @param {string[]} fields */
const sameFields = (a, b, fields) =>
  fields.every((field) => (a?.[field] ?? null) === (b?.[field] ?? null));
const routeFields = [
  'jobId',
  'logicalIdentity',
  'lifecycle',
  'threadId',
  'hostId',
  'coordinatorJobId'
];
const targetFields = ['kind', 'sessionId', 'cwd', 'threadId', 'hostId'];

/** @param {Record<string,string>} previous @param {Item[]} incoming @param {string} coverage */
function mergeItems(previous, incoming, coverage) {
  const result = coverage === 'complete' ? {} : { ...previous };
  for (const item of incoming) result[itemKey(item)] = item.revision;
  return result;
}

/** @param {Record<string,string>} map @param {string} kind */
function listed(map, kind) {
  return Object.entries(map).map(([key, revision]) => {
    const [jobId, itemId] = JSON.parse(key);
    return { kind, jobId, itemId, revision };
  });
}

/** @param {Record<string,any>} route @param {Record<string,any>} routes */
function destination(route, routes) {
  if (route.lifecycle === 'retired') {
    const coordinator = routes[route.coordinatorJobId];
    if (!coordinator || coordinator.lifecycle !== 'active' || (!coordinator.threadId && !coordinator.target))
      return { status: 'needs_coordinator' };
    return {
      target: coordinator.target ?? {
        threadId: coordinator.threadId,
        ...(coordinator.hostId ? { hostId: coordinator.hostId } : {})
      }
    };
  }
  if (route.lifecycle !== 'active' || (!route.threadId && !route.target)) return { status: 'needs_successor' };
  return {
    target: route.target ?? { threadId: route.threadId, ...(route.hostId ? { hostId: route.hostId } : {}) }
  };
}

/** @param {Lease['scope']} scope @param {Record<string,any>} route @param {Item[]} items */
function wakeIdentity(scope, route, items) {
  const payload = [
    scope.principalId,
    scope.workspaceId,
    scope.environmentId,
    scope.harness,
    route.jobId,
    route.logicalIdentity,
    route.bindingVersion,
    sortItems([...items])
  ];
  return createHash('sha256').update(JSON.stringify(payload)).digest('hex');
}

/** @param {Record<string,any>} route @param {Item[]} items @param {string} wakeId @param {string} attemptId */
function promptFor(route, items, wakeId, attemptId) {
  const prefix = `Listener notification for logical job ${route.logicalIdentity} (${route.jobId}). Known item IDs/revisions: `;
  const suffix = ` Wake ${wakeId}; attempt ${attemptId}. Please acknowledge this notification using those IDs and your session target. This is not a completion request.`;
  const limit = 4096 - prefix.length - suffix.length - 50;
  let refs = '';
  let count = 0;
  for (const item of items) {
    const next = `${count ? ', ' : ''}${item.kind} ${item.itemId}@${item.revision}`;
    if (refs.length + next.length > limit) break;
    refs += next;
    count += 1;
  }
  if (count < items.length) refs += `, plus ${items.length - count} more journaled items`;
  return prefix + refs + suffix;
}

/** @param {Store} store @param {Lease} lease @param {unknown} routes @param {number} nowMs */
export function configureRoutes(store, lease, routes, nowMs) {
  const configured = validRoutes(routes);
  store.transact(lease, nowMs, (state) => {
    const previous = /** @type {Record<string,any>} */ (state.routes);
    const policy = /** @type {Record<string,any>} */ (state.policy);
    /** @type {Record<string,number>} */
    const generations = { ...(policy.routeGenerations ?? {}) };
    /** @type {Record<string,any>} */
    const next = {};
    for (const [jobId, route] of Object.entries(configured)) {
      const prior = previous[jobId];
      const historicalAttemptVersion = Math.max(
        0,
        .../** @type {any[]} */ (Object.values(state.attempts))
          .filter((attempt) => attempt.jobId === jobId)
          .map((attempt) => attempt.bindingVersion)
      );
      const baseVersion = Math.max(
        generations[jobId] ?? 0,
        prior?.bindingVersion ?? 0,
        historicalAttemptVersion
      );
      const oldTarget = prior && destination(prior, previous).target;
      const newTarget = destination(route, configured).target;
      const bindingVersion =
        prior &&
        sameFields(prior, route, routeFields) &&
        sameFields(oldTarget, newTarget, targetFields)
          ? prior.bindingVersion
          : baseVersion + 1;
      if (!Number.isSafeInteger(bindingVersion)) throw new RangeError('Listener binding exhausted');
      generations[jobId] = bindingVersion;
      next[jobId] = {
        ...route,
        bindingVersion
      };
    }
    for (const attempt of /** @type {any[]} */ (Object.values(state.attempts))) {
      const old = previous[attempt.jobId];
      if (!next[attempt.jobId] || old?.bindingVersion !== next[attempt.jobId].bindingVersion)
        attempt.superseded = true;
    }
    policy.routeGenerations = generations;
    state.routes = next;
    return state;
  });
  return { routes: configured };
}

/** @param {Store} store @param {Lease} lease @param {unknown} input @param {number} nowMs */
export function prepareWake(store, lease, input, nowMs) {
  /** @type {any} */
  let output;
  store.transact(lease, nowMs, (state) => {
    const policy = /** @type {Record<string,any>} */ (state.policy);
    const read = validObservation(input, nowMs, policy.lastObservedMs ?? 0);
    const oldEvents = policy.events ?? {};
    const oldObligations = policy.obligations ?? {};
    const events = mergeItems(
      oldEvents,
      read.eventObservation.items,
      read.eventObservation.coverage
    );
    const obligations = mergeItems(
      oldObligations,
      read.obligationObservation.items,
      read.obligationObservation.coverage
    );
    const arrival =
      read.eventObservation.items.some((item) => oldEvents[itemKey(item)] !== item.revision) ||
      read.obligationObservation.items.some(
        (item) => oldObligations[itemKey(item)] !== item.revision
      );
    const wake = evaluateWake({
      eventObservation: {
        coverage: read.eventObservation.coverage,
        events: listed(events, 'event')
      },
      obligationObservation: {
        coverage: read.obligationObservation.coverage,
        obligations: listed(obligations, 'obligation')
      }
    });
    const complete =
      read.eventObservation.coverage === 'complete' &&
      read.obligationObservation.coverage === 'complete';
    const cadence = advanceListenerPolicy(
      policy,
      {
        status: complete ? 'success' : 'incomplete',
        wake,
        ...(arrival ? { newlyObservedWorkAt: read.observedAt } : {})
      },
      read.observedAt
    );
    Object.assign(policy, cadence);
    if (arrival && !complete) {
      policy.lastWorkAt = read.observedAt;
      policy.policyIntervalMinutes = Math.min(policy.policyIntervalMinutes ?? 15, 15);
      policy.nextIntervalMinutes = Math.min(policy.nextIntervalMinutes, 15);
    }
    policy.events = events;
    policy.obligations = obligations;
    policy.enrolledAt ??= read.observedAt;
    policy.lastObservedMs = read.observedMs;
    const byJob = new Map();
    for (const item of [...listed(events, 'event'), ...listed(obligations, 'obligation')]) {
      if (!byJob.has(item.jobId)) byJob.set(item.jobId, []);
      byJob.get(item.jobId).push(item);
    }
    const actions = [];
    const attention = [];
    const reconciliation = /** @type {any[]} */ (Object.values(state.attempts))
      .filter((attempt) => ['prepared', 'dispatching', 'uncertain'].includes(attempt.state) || ['claimed', 'uncertain'].includes(attempt.dispatchStatus))
      .map((attempt) => ({
        wakeId: attempt.wakeId,
        attemptId: attempt.attemptId,
        jobId: attempt.jobId,
        state: attempt.state,
        target: attempt.target,
        superseded: Boolean(attempt.superseded)
      }));
    for (const pending of reconciliation)
      attention.push({
        jobId: pending.jobId,
        wakeId: pending.wakeId,
        status: 'needs_reconciliation'
      });
    for (const [jobId, items] of [...byJob.entries()].sort(([a], [b]) => a.localeCompare(b))) {
      const route = /** @type {any} */ (state.routes[jobId]);
      if (!route) {
        attention.push({ jobId, status: 'needs_route' });
        continue;
      }
      const resolved = destination(route, /** @type {Record<string,any>} */ (state.routes));
      if (resolved.status) {
        attention.push({ jobId, status: resolved.status });
        continue;
      }
      const claude = resolved.target?.kind === 'claude-code';
      if (lease.scope.harness !== (claude ? 'claude-code' : 'codex')) {
        attention.push({ jobId, status: 'unsupported_harness' });
        continue;
      }
      const wakeId = wakeIdentity(lease.scope, route, items);
      if (state.attempts[wakeId]) continue;
      const attemptId = randomUUID();
      const target = resolved.target;
      const sorted = sortItems([...items]);
      state.attempts[wakeId] = {
        wakeId,
        attemptId,
        jobId,
        logicalIdentity: route.logicalIdentity,
        bindingVersion: route.bindingVersion,
        target,
        items: sorted,
        state: 'prepared',
        superseded: false
      };
      actions.push({
        wakeId,
        attemptId,
        tool: claude ? 'coord-v5 listener dispatch-claude' : 'mcp__codex_app__send_message_to_thread',
        arguments: claude ? { wakeId, attemptId, target } : { ...target, prompt: promptFor(route, sorted, wakeId, attemptId) }
      });
    }
    let interval = policy.nextIntervalMinutes ?? 15;
    if (!complete || reconciliation.length) interval = Math.min(interval, 5);
    else if (!byJob.size && interval > 15) {
      const jitter =
        ((parseInt(
          createHash('sha256')
            .update(`${JSON.stringify(lease.scope)}:${read.observedAt}`)
            .digest('hex')
            .slice(0, 4),
          16
        ) +
          1) /
          65536) *
        0.1;
      interval = Math.min(180, policy.operatorIntervalMinutes ?? Infinity, interval * (1 + jitter));
    }
    output = {
      coverage: {
        events: read.eventObservation.coverage,
        obligations: read.obligationObservation.coverage
      },
      actions,
      reconciliation,
      attention,
      nextIntervalMinutes: interval
    };
    return state;
  });
  return output;
}

/** @param {any} state @param {Record<string,any>} value */
function matched(state, value) {
  const attempt = state.attempts[value.wakeId];
  if (
    !attempt ||
    attempt.attemptId !== value.attemptId ||
    !sameFields(attempt.target, value.target, targetFields)
  )
    throw new Error('Listener correlation mismatch');
  return attempt;
}

/** Explicit opt-in delivery. A claimed/ambiguous invocation is never retried. */
export async function dispatchClaudeWake(store, lease, input, options) {
  const value = correlation(input);
  if (lease.scope.harness !== 'claude-code' || value.target.kind !== 'claude-code')
    throw new TypeError('Invalid Claude dispatch target');
  validateClaudeExecutor(options);
  const now = options.now ?? Date.now;
  let claim;
  let blocked;
  store.transact(lease, now(), state => {
    const attempt = matched(state, value);
    const route = state.routes[attempt.jobId];
    if (attempt.superseded || !route || route.bindingVersion !== attempt.bindingVersion || !sameFields(destination(route, state.routes).target, attempt.target, targetFields)) {
      blocked = { state: attempt.state, code: 'STALE_BINDING' };
    } else if (attempt.state !== 'prepared' || attempt.dispatchStatus) {
      blocked = { state: attempt.state, code: 'NOT_DISPATCHABLE' };
    } else if (Object.values(state.attempts).some(other => other !== attempt && other.target?.kind === 'claude-code' && other.target.sessionId === attempt.target.sessionId && ['claimed', 'uncertain'].includes(other.dispatchStatus))) {
      blocked = { state: attempt.state, code: 'SESSION_RECONCILIATION_REQUIRED' };
    } else {
      attempt.state = 'dispatching';
      attempt.dispatchStatus = 'claimed';
      attempt.dispatchClaimId = randomUUID();
      claim = { claimId: attempt.dispatchClaimId, target: { ...attempt.target }, prompt: promptFor(attempt, attempt.items, attempt.wakeId, attempt.attemptId) };
    }
    return state;
  });
  if (blocked) return { wakeId: value.wakeId, attemptId: value.attemptId, ...blocked };
  const outcome = await executeClaude(claim.target, claim.prompt, options);
  let result;
  store.transact(lease, now(), state => {
    const attempt = matched(state, value);
    if (attempt.dispatchClaimId !== claim.claimId) throw new Error('Claude dispatch claim mismatch');
    attempt.dispatchStatus = outcome.accepted ? 'accepted' : 'uncertain';
    attempt.receiptStatus = outcome.accepted ? 'accepted' : 'unknown';
    attempt.dispatchCode = outcome.code;
    if (attempt.state !== 'acknowledged') attempt.state = outcome.accepted ? 'accepted' : 'uncertain';
    result = { wakeId: attempt.wakeId, attemptId: attempt.attemptId, state: attempt.state, code: outcome.code };
    return state;
  });
  return result;
}

/** @param {Store} store @param {Lease} lease @param {unknown} receipt @param {number} nowMs */
export function settleWake(store, lease, receipt, nowMs) {
  const value = correlation(receipt, true);
  let result;
  store.transact(lease, nowMs, (state) => {
    const attempt = matched(state, value);
    const next = value.status === 'accepted' ? 'accepted' : 'uncertain';
    if (attempt.receiptStatus === 'accepted' && value.status !== 'accepted')
      throw new Error('Conflicting listener receipt');
    if (attempt.state === 'acknowledged' && next !== 'accepted')
      throw new Error('Conflicting listener receipt');
    attempt.receiptStatus = value.status;
    if (attempt.dispatchStatus && value.status === 'accepted') attempt.dispatchStatus = 'accepted';
    if (attempt.state !== 'acknowledged') attempt.state = next;
    result = { wakeId: attempt.wakeId, attemptId: attempt.attemptId, state: attempt.state };
    return state;
  });
  return result;
}

/** @param {Store} store @param {Lease} lease @param {unknown} ack @param {number} nowMs */
export function acknowledgeWake(store, lease, ack, nowMs) {
  const value = correlation(ack);
  let result;
  store.transact(lease, nowMs, (state) => {
    const attempt = matched(state, value);
    attempt.state = 'acknowledged';
    result = { wakeId: attempt.wakeId, attemptId: attempt.attemptId, state: attempt.state };
    return state;
  });
  return result;
}
