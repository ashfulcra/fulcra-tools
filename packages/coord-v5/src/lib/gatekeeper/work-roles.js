/** Pure folds over source-authorized causal facts. Claims are not locks or grants. */
import { evaluateSourceFreshness } from './work-presence.js';
import { compareWorkInstants } from './work-contract.js';
const actorKeys = ['principal_id', 'logical_agent_id', 'instance_id', 'session_id'];
const sameActor = (a, b) => a && b && actorKeys.every((k) => a[k] === b[k]);
const own = (facts, id) =>
  facts.filter((f) => f.event.subject.type === 'role' && f.event.subject.id === id);
function claims(facts) {
  return facts.filter(
    (f) =>
      f.event.kind === 'role.claimed' &&
      !facts.some(
        (g) =>
          (g.event.kind === 'role.claimed' &&
            g.event.payload.previous_claim_event_id === f.event.event_id) ||
          (g.event.kind === 'role.released' &&
            g.event.payload.claim_event_id === f.event.event_id) ||
          (g.event.kind === 'role.resolved' &&
            g.event.payload.claim_event_ids.includes(f.event.event_id) &&
            !g.event.payload.retained_claim_event_ids.includes(f.event.event_id))
      )
  );
}
const ids = (rows) => rows.map((f) => f.event.event_id).sort();
/** Ancestors only, supplied by replay after source/grant validation. */
export function roleTransitionError(event, facts, trust) {
  const rows = own(facts, event.subject.id),
    p = event.payload;
  const definitions = rows.filter((f) => f.event.kind === 'role.defined');
  if (event.kind === 'role.defined') return definitions.length ? 'ROLE_IDENTITY_IMMUTABLE' : null;
  if (definitions.length !== 1) return 'ROLE_DEFINITION_REQUIRED';
  const def = definitions[0].event;
  if (compareWorkInstants(event.occurred_at, def.occurred_at) < 0)
    return 'ROLE_TIME_BEFORE_ANCESTRY';
  if (def.workstream_id !== event.workstream_id) return 'IDENTITY_MISMATCH';
  if (p.expected_role_event_id && p.expected_role_event_id !== def.event_id)
    return 'ROLE_DEFINITION_MISMATCH';
  const heads = claims(rows),
    prior = heads.find((f) => f.event.event_id === (p.previous_claim_event_id ?? p.claim_event_id));
  if (event.kind === 'role.claimed') {
    if (p.previous_claim_event_id && (!prior || !sameActor(prior.event.actor, event.actor)))
      return 'ROLE_CLAIMANT_REQUIRED';
    return null;
  }
  if (event.kind === 'role.resolved') {
    if (JSON.stringify([...p.claim_event_ids].sort()) !== JSON.stringify(ids(heads)))
      return 'ROLE_RESOLUTION_HEAD_MISMATCH';
    if (def.payload.policy === 'exclusive' && p.retained_claim_event_ids.length > 1)
      return 'ROLE_RESOLUTION_EXCLUSIVE';
    return null;
  }
  if (!prior) return 'ROLE_CURRENT_CLAIM_REQUIRED';
  if (event.kind === 'role.released')
    return sameActor(prior.event.actor, event.actor) ||
      trust.grants.some((g) => sameActor(g, event.actor) && g.capabilities.includes('role.manage'))
      ? null
      : 'ROLE_CLAIMANT_REQUIRED';
  if (!sameActor(prior.event.actor, event.actor)) return 'ROLE_CLAIMANT_REQUIRED';
  if (compareWorkInstants(event.occurred_at, prior.event.payload.expires_at) >= 0)
    return 'ROLE_CLAIM_EXPIRED';
  if (def.payload.policy === 'exclusive' && heads.length !== 1) return 'ROLE_CONTESTED';
  const publication = facts.find((f) => f.event.event_id === p.checkpoint_event_id)?.event;
  if (
    !publication ||
    publication.kind !== 'checkpoint.published' ||
    publication.workstream_id !== event.workstream_id ||
    !sameActor(publication.actor, event.actor) ||
    compareWorkInstants(publication.occurred_at, event.occurred_at) > 0
  )
    return 'ROLE_CHECKPOINT_PUBLICATION_REQUIRED';
  return publication.payload.body_digest === p.body_digest
    ? null
    : 'ROLE_CHECKPOINT_DIGEST_MISMATCH';
}
/** Retained authorized facts only; definitions/checkpoint references survive claims. */
export function foldWorkRoles(facts) {
  return [
    ...new Set(facts.filter((f) => f.event.subject.type === 'role').map((f) => f.event.subject.id))
  ]
    .sort()
    .map((role_id) => {
      const rows = own(facts, role_id),
        definitions = rows.filter((f) => f.event.kind === 'role.defined');
      const references = rows.filter((f) => f.event.kind === 'role.checkpoint');
      const latest = references.filter(
        (f) => !references.some((g) => g.ancestors.has(f.event.event_id))
      );
      const resolutions = rows.filter((f) => f.event.kind === 'role.resolved');
      const resolutionHeads = resolutions.filter(
        (f) => !resolutions.some((g) => g.ancestors.has(f.event.event_id))
      );
      const mutations = rows.filter(
        (f) =>
          f.event.kind === 'role.released' ||
          (f.event.kind === 'role.claimed' && f.event.payload.previous_claim_event_id)
      );
      const mutationConflict = mutations.some((a) =>
        mutations.some(
          (b) =>
            a !== b &&
            (a.event.payload.claim_event_id ?? a.event.payload.previous_claim_event_id) ===
              (b.event.payload.claim_event_id ?? b.event.payload.previous_claim_event_id) &&
            !a.ancestors.has(b.event.event_id) &&
            !b.ancestors.has(a.event.event_id) &&
            !resolutionHeads.some(
              (r) => r.ancestors.has(a.event.event_id) && r.ancestors.has(b.event.event_id)
            )
        )
      );
      return {
        role_id,
        name: definitions.length === 1 ? definitions[0].event.payload.name : null,
        policy: definitions.length === 1 ? definitions[0].event.payload.policy : null,
        definition_event_id: definitions.length === 1 ? definitions[0].event.event_id : null,
        claim_event_ids: ids(claims(rows)),
        claims: claims(rows)
          .map((f) => f.event)
          .sort((a, b) => a.event_id.localeCompare(b.event_id)),
        released_claim_event_ids: rows
          .filter((f) => f.event.kind === 'role.released')
          .map((f) => f.event.payload.claim_event_id)
          .sort(),
        checkpoint_references: latest
          .map((f) => ({ event_id: f.event.event_id, ...f.event.payload, actor: f.event.actor }))
          .sort((a, b) => a.event_id.localeCompare(b.event_id)),
        conflicted:
          definitions.length !== 1 ||
          resolutionHeads.length > 1 ||
          latest.length > 1 ||
          mutationConflict,
        provisional: false
      };
    });
}

/** Known claims can be retained without establishing current exclusive ownership. */
export function evaluateWorkRoles(input) {
  const source = evaluateSourceFreshness(input);
  if (source.status !== 'ready') return { ...source, rows: [] };
  if (!Array.isArray(input.projection.roles))
    return { ...source, status: 'blocked', code: 'INVALID_PROJECTION', rows: [] };
  return {
    ...source,
    rows: input.projection.roles.map((row) => {
      const live_claims = row.claims.filter(
        (e) => compareWorkInstants(e.payload.expires_at, input.evaluated_at) > 0
      );
      const duplicate =
        new Set(live_claims.map((e) => actorKeys.map((k) => e.actor[k]).join('\0'))).size !==
        live_claims.length;
      const complete = input.projection.observation.coverage === 'complete';
      const contested =
        row.conflicted || duplicate || (row.policy === 'exclusive' && live_claims.length > 1);
      let state =
        source.source_freshness !== 'fresh'
          ? 'unknown'
          : contested
            ? 'contested'
            : live_claims.length
              ? 'held'
              : row.claims.length
                ? 'lapsed'
                : complete
                  ? 'vacant'
                  : 'unknown';
      return {
        ...row,
        state,
        live_claims,
        routing_allowed: source.source_freshness === 'fresh' && complete && state === 'held',
        checkpoint_reference:
          row.checkpoint_references.length === 1 ? row.checkpoint_references[0] : null,
        provisional: !complete
      };
    })
  };
}
