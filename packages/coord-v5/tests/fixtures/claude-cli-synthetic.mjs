#!/usr/bin/env node
// Deliberately synthetic local executable. No provider access or credentials.
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
let prompt = '';
for await (const chunk of process.stdin) prompt += chunk;
const behavior = JSON.parse(readFileSync('behavior.json', 'utf8'));
const count = existsSync('calls.json') ? JSON.parse(readFileSync('calls.json', 'utf8')).count + 1 : 1;
let claimState;
if (behavior.inspectClaim) {
  const { DatabaseSync } = await import('node:sqlite');
  const db = new DatabaseSync('journal.sqlite');
  try { claimState = Object.values(JSON.parse(db.prepare('SELECT state_json FROM journal').get().state_json).attempts)[0].dispatchStatus; }
  finally { db.close(); }
}
writeFileSync('calls.json', JSON.stringify({ argv: process.argv.slice(2), cwd: process.cwd(), prompt, count, claimState }));
if (behavior.descendantPipes) spawn(process.execPath, ['-e', 'setTimeout(() => {}, 1500)'], { stdio: 'inherit' });
if (behavior.hang) await new Promise(() => { setInterval(() => {}, 1000); });
if (behavior.delayMs) await new Promise(resolve => setTimeout(resolve, behavior.delayMs));
if (behavior.stdoutBytes) process.stdout.write('x'.repeat(behavior.stdoutBytes));
if (behavior.stderrBytes) process.stderr.write('x'.repeat(behavior.stderrBytes));
if (behavior.stderr) process.stderr.write(behavior.stderr);
if (behavior.raw) process.stdout.write(behavior.raw);
if (behavior.response) process.stdout.write(JSON.stringify(behavior.response));
process.exitCode = behavior.exitCode ?? 0;
