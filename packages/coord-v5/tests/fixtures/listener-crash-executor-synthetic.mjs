#!/usr/bin/env node
// Test-only executor: no Claude, network, credentials, or descendants.
import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

let prompt = '';
for await (const chunk of process.stdin) prompt += chunk;
const input = JSON.parse(readFileSync('dispatch-input.json', 'utf8'));
const db = new DatabaseSync('journal.sqlite', { readOnly: true });
let attempt;
try {
  attempt = JSON.parse(db.prepare('SELECT state_json FROM journal').get().state_json).attempts[input.wakeId];
} finally {
  db.close();
}
if (attempt?.attemptId !== input.attemptId || attempt.state !== 'dispatching' ||
    attempt.dispatchStatus !== 'claimed' || !attempt.dispatchClaimId) process.exit(3);
if (!prompt.includes(`Wake ${input.wakeId}; attempt ${input.attemptId}`)) process.exit(4);
const count = existsSync('executor-marker.json')
  ? JSON.parse(readFileSync('executor-marker.json', 'utf8')).count + 1 : 1;
writeFileSync('executor-marker.tmp', JSON.stringify({
  count, fixturePid: process.pid, dispatcherPid: process.ppid,
  wakeId: input.wakeId, attemptId: input.attemptId,
  claimId: attempt.dispatchClaimId, claimCommitted: true,
}), { mode: 0o600 });
// Publication is atomic: observing the marker means all claim checks passed.
renameSync('executor-marker.tmp', 'executor-marker.json');
// Fail-safe only. Never report native acceptance, even if supervisor cleanup fails.
setTimeout(() => process.exit(5), 10000);
