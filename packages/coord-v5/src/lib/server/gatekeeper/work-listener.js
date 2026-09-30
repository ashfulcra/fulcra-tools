import { createHash } from 'node:crypto';
import { canonicalWorkJson } from '../../gatekeeper/work-contract.js';
import { buildAuthorizedWorkView } from './work-view.js';

const TERMINAL = new Set(['completed', 'cancelled', 'closed']);
/** @param {unknown} value */
function revision(value) {
  return `sha256:${createHash('sha256').update(canonicalWorkJson(value)).digest('hex')}`;
}
/** @param {{store:any,policy:any,now:()=>number}} input */
export function buildWorkListenerObservation({ store, policy, now }) {
  const view = buildAuthorizedWorkView({ store, policy, now });
  if (view.status !== 'ready') return view;
  const projection = view.projection;
  const observedAt = projection.observation.as_of;
  /** @type {Map<string,string>} */
  const mapped = new Map(
    policy.work_jobs.map((/** @type {any} */ entry) => [entry.work_id, entry.job_id])
  );
  /** @type {{itemId:string,jobId:string,revision:string}[]} */
  const events = [];
  /** @type {{itemId:string,jobId:string,revision:string}[]} */
  const obligations = [];
  /** @type {{code:string,work_id?:string,subject_id?:string}[]} */
  const diagnostics = [];
  /** @type {Set<string>} */
  const unmapped = new Set();
  /** @param {string} workId @param {string} itemId @param {unknown} content @param {boolean} pending */
  function include(workId, itemId, content, pending) {
    const jobId = mapped.get(workId);
    if (!jobId) {
      if (!unmapped.has(workId)) diagnostics.push({ code: 'UNMAPPED_WORK', work_id: workId });
      unmapped.add(workId);
      return;
    }
    const item = { itemId, jobId, revision: revision(content) };
    events.push(item);
    if (pending) obligations.push(item);
  }
  for (const work of projection.work) {
    if (!work.item) continue;
    include(
      work.work_id,
      `work:${work.work_id}`,
      {
        item: work.item,
        head_event_id: work.head_event_id,
        assignment: work.assignment,
        applied_event_ids: work.history
          .filter((entry) => entry.disposition === 'applied')
          .map((entry) => entry.event_id)
          .sort(),
        state: work.state,
        execution_authority: work.execution_authority,
        validation: work.validation.map((entry) => entry.code).sort(),
        branch_event_ids: [...work.branch_event_ids].sort()
      },
      !TERMINAL.has(work.item.status)
    );
  }
  for (const question of projection.questions) {
    if (!question.opened_event) continue;
    const ids = [
      ...new Set(/** @type {string[]} */ (question.opened_event.payload.work_ids))
    ].sort();
    for (const workId of ids)
      include(
        workId,
        `question:${question.question_id}`,
        {
          opened_event: question.opened_event,
          current_answer: question.current_answer,
          acknowledgments: question.acknowledgments.map((entry) => entry.event_id).sort(),
          applications: question.applications.map((entry) => entry.event_id).sort(),
          state: question.state,
          branch_event_ids: [...question.branch_event_ids].sort()
        },
        !question.current_answer || question.applications.length === 0
      );
  }
  /** @type {Map<string,{code:string,event_ids:string[]}[]>} */
  const conflicts = new Map();
  for (const conflict of projection.conflicts) {
    const subject = conflict.subject_id;
    if (!subject) continue;
    if (!conflicts.has(subject)) conflicts.set(subject, []);
    /** @type {{code:string,event_ids:string[]}[]} */ (conflicts.get(subject)).push({
      code: conflict.code,
      event_ids: [...(conflict.event_ids ?? [])].sort()
    });
  }
  for (const [subject, rows] of conflicts) {
    const question = projection.questions.find((q) => q.question_id === subject);
    const linked = /** @type {string[]} */ (
      question?.opened_event?.payload.work_ids ??
        question?.history
          .filter((entry) => entry.event.kind === 'question.opened')
          .flatMap((entry) => entry.event.payload.work_ids) ??
        []
    );
    for (const workId of linked.length ? [...new Set(linked)].sort() : [subject]) {
      if (!mapped.has(workId)) continue;
      include(
        workId,
        `conflict:${subject}`,
        rows.sort((a, b) => canonicalWorkJson(a).localeCompare(canonicalWorkJson(b))),
        true
      );
    }
  }
  events.sort((a, b) => a.jobId.localeCompare(b.jobId) || a.itemId.localeCompare(b.itemId));
  obligations.sort((a, b) => a.jobId.localeCompare(b.jobId) || a.itemId.localeCompare(b.itemId));
  if (events.length > 1000 || obligations.length > 1000)
    return { status: 'blocked', code: 'OBSERVATION_LIMIT' };
  for (const entry of projection.rejected) diagnostics.push({ code: entry.code });
  for (const entry of projection.pending) diagnostics.push({ code: entry.code });
  for (const entry of projection.conflicts)
    diagnostics.push({ code: entry.code, subject_id: entry.subject_id });
  const coverage = projection.observation.coverage === 'complete' ? 'complete' : 'partial';
  return {
    observation: {
      version: 1,
      eventObservation: { coverage, items: events },
      obligationObservation: { coverage, items: obligations },
      observedAt
    },
    diagnostics
  };
}
