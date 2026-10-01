import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import {
  chmodSync, copyFileSync, existsSync, mkdtempSync, readFileSync,
  realpathSync, rmSync, statSync, writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

async function until(predicate, milliseconds, description) {
  const deadline = performance.now() + milliseconds;
  while (!predicate()) {
    assert.ok(performance.now() < deadline, `Timed out: ${description}`);
    await delay(10);
  }
}

// Node 22 reports this built-in module warning; no application stderr is hidden.
const applicationStderr = (text) => text.replace(
  /^\(node:\d+\) ExperimentalWarning: SQLite is an experimental feature and might change at any time\r?\n\(Use `node --trace-warnings \.\.\.` to show where the warning was created\)\r?\n/gm, '',
);

test('installed dispatcher crash retains exact claim and refuses same-holder resend', { timeout: 60000 }, async () => {
  const directory = realpathSync(mkdtempSync(join(tmpdir(), 'coord-v5-crash-')));
  chmodSync(directory, 0o700);
  const markerPath = join(directory, 'executor-marker.json');
  const fixture = join(directory, 'synthetic-executor.mjs');
  const db = join(directory, 'journal.sqlite');
  const scope = { principalId: 'synthetic', workspaceId: 'synthetic', environmentId: 'crash-test', harness: 'claude-code' };
  const holder = 'synthetic-holder';
  const target = { kind: 'claude-code', sessionId: '11111111-1111-4111-8111-111111111111', cwd: directory };
  let dispatcher, marker, exit, closed = false, spawnError, testError;
  let stdout = '', stderr = '', outputOverflow = false;
  const run = (program, args, options = {}) => {
    const result = spawnSync(program, args, {
      cwd: directory, encoding: 'utf8', timeout: 15000, maxBuffer: 128 * 1024, ...options,
    });
    assert.ifError(result.error);
    return result;
  };
  // Do not signal a PID that has exited and been reused. This unique temporary
  // executable path and the marker's direct parent bind cleanup to our fixture.
  const fixtureRunning = () => {
    if (!marker) return false;
    assert.ok(Number.isSafeInteger(marker.fixturePid) && marker.fixturePid > 1);
    assert.equal(marker.dispatcherPid, dispatcher.pid);
    assert.notEqual(marker.fixturePid, dispatcher.pid);
    const processInfo = run('ps', ['-ww', '-p', String(marker.fixturePid), '-o', 'stat=', '-o', 'command='], { timeout: 1000 });
    if (processInfo.status === 1 && !processInfo.stdout.trim()) return false;
    assert.equal(processInfo.status, 0, processInfo.stderr);
    const info = processInfo.stdout.trim();
    if (/^Z/.test(info)) return false; // Terminated orphan awaiting OS reaping.
    assert.ok(info.includes(fixture), 'Fixture PID is not our owned executor');
    return true;
  };
  const stopFixture = async () => {
    if (fixtureRunning()) {
      try { process.kill(marker.fixturePid, 'SIGKILL'); }
      catch (error) { if (error.code !== 'ESRCH') throw error; }
      await until(() => !fixtureRunning(), 2000, 'owned fixture termination');
    }
  };
  try {
    assert.equal(statSync(directory).mode & 0o777, 0o700);
    const packed = run('npm', ['pack', '--json', '--ignore-scripts', '--pack-destination', directory], { cwd: resolve(import.meta.dirname, '..') });
    assert.equal(packed.status, 0, packed.stderr);
    const packageInfo = JSON.parse(packed.stdout)[0];
    assert.ok(packageInfo.files.every(({ path }) => !path.startsWith('test/') && !path.startsWith('tests/')),
      'Crash regression and executor must not ship in the runtime package');
    const installed = run('npm', ['install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund', join(directory, packageInfo.filename)]);
    assert.equal(installed.status, 0, installed.stderr);
    const root = join(directory, 'node_modules', '@fulcra', 'coord-v5');
    const bin = join(root, 'bin', 'coord-v5.mjs');
    copyFileSync(resolve(import.meta.dirname, '../tests/fixtures/listener-crash-executor-synthetic.mjs'), fixture);
    chmodSync(fixture, 0o700);
    const flags = ['--db', db, '--scope', JSON.stringify(scope), '--holder', holder];
    const cli = (command, input, expected = 0, extra = []) => {
      const result = run(process.execPath, [bin, 'listener', command, ...flags, ...extra], {
        input: input === undefined ? undefined : JSON.stringify(input), timeout: 5000,
      });
      assert.equal(result.status, expected, `${command}: ${result.stdout} ${result.stderr}`);
      assert.equal(applicationStderr(result.stderr), '', `${command} stderr`);
      return JSON.parse(result.stdout);
    };
    const inspect = () => cli('inspect').snapshot.state;
    const observation = (coverage = 'complete', empty = false) => ({
      version: 1, observedAt: new Date().toISOString(),
      eventObservation: { coverage, items: empty ? [] : [{ jobId: 'job', itemId: 'event', revision: 'r1' }] },
      obligationObservation: { coverage, items: empty ? [] : [{ jobId: 'job', itemId: 'obligation', revision: 'r1' }] },
    });
    cli('configure', [{ jobId: 'job', logicalIdentity: 'synthetic-agent', lifecycle: 'active', target }]);
    const prepared = cli('prepare', observation());
    assert.equal(prepared.actions.length, 1);
    const input = prepared.actions[0].arguments;
    writeFileSync(join(directory, 'dispatch-input.json'), JSON.stringify(input), { mode: 0o600 });
    const before = inspect();
    assert.equal(before.attempts[input.wakeId].state, 'prepared');
    assert.deepEqual(before.policy.obligations, { '["job","obligation"]': 'r1' });
    const executorFlags = ['--executable', fixture, '--timeout-ms', '5000'];
    // Spawn the actual installed script, not the outer CLI wrapper: this PID owns
    // dispatch and is killed while its external executor remains in flight.
    dispatcher = spawn(process.execPath, [join(root, 'scripts', 'gatekeeper-listener.mjs'), 'dispatch-claude', ...flags, ...executorFlags], {
      cwd: directory, stdio: ['pipe', 'pipe', 'pipe'],
    });
    dispatcher.on('error', (error) => { spawnError = error; });
    dispatcher.on('exit', (code, signal) => { exit = { code, signal }; });
    dispatcher.on('close', () => { closed = true; });
    for (const [stream, append] of [
      [dispatcher.stdout, (text) => { stdout += text; }],
      [dispatcher.stderr, (text) => { stderr += text; }],
    ]) stream.on('data', (chunk) => {
      if (stdout.length + stderr.length + chunk.length > 65536) outputOverflow = true;
      else append(chunk.toString());
    });
    dispatcher.stdin.on('error', (error) => { spawnError ??= error; });
    dispatcher.stdin.end(JSON.stringify(input));
    await until(() => {
      assert.ifError(spawnError);
      assert.equal(exit, undefined, 'Dispatcher exited before committed-claim marker');
      return existsSync(markerPath);
    }, 3000, 'committed claim observed by executor');
    marker = JSON.parse(readFileSync(markerPath, 'utf8'));
    assert.equal(marker.claimCommitted, true);
    assert.equal(marker.count, 1);
    assert.equal(marker.wakeId, input.wakeId);
    assert.equal(marker.attemptId, input.attemptId);
    assert.equal(marker.dispatcherPid, dispatcher.pid);
    assert.ok(fixtureRunning());
    assert.equal(dispatcher.kill('SIGKILL'), true);
    await until(() => exit !== undefined, 2000, 'actual dispatcher SIGKILL exit');
    assert.deepEqual(exit, { code: null, signal: 'SIGKILL' });
    // Separate supervisor cleanup, not a claim about product descendant cleanup.
    await stopFixture();
    await until(() => closed, 2000, 'dispatcher stdio closed');
    assert.ifError(spawnError);
    assert.equal(outputOverflow, false);
    assert.equal(stdout, '');
    assert.equal(applicationStderr(stderr), '');
    const crashed = inspect();
    const claim = crashed.attempts[input.wakeId];
    assert.equal(claim.state, 'dispatching');
    assert.equal(claim.dispatchStatus, 'claimed');
    assert.equal(claim.dispatchClaimId, marker.claimId);
    assert.equal(claim.attemptId, input.attemptId);
    assert.deepEqual(claim.target, input.target);
    assert.equal(claim.receiptStatus, undefined);
    assert.equal(claim.dispatchCode, undefined);
    assert.deepEqual(crashed.policy.obligations, before.policy.obligations);
    // Each CLI call starts a fresh process with the same holder and durable DB.
    assert.deepEqual(cli('dispatch-claude', input, 2, executorFlags), {
      wakeId: input.wakeId, attemptId: input.attemptId, state: 'dispatching', code: 'NOT_DISPATCHABLE',
    });
    for (const observed of [observation(), observation('partial', true), observation('unavailable', true)]) {
      const replay = cli('prepare', observed);
      assert.deepEqual(replay.actions, []);
      assert.deepEqual(replay.reconciliation, [{
        wakeId: input.wakeId, attemptId: input.attemptId, jobId: 'job',
        state: 'dispatching', target: input.target, superseded: false,
      }]);
      assert.ok(replay.attention.some((item) => item.status === 'needs_reconciliation' && item.wakeId === input.wakeId));
      const retained = inspect();
      assert.deepEqual(retained.attempts, { [input.wakeId]: claim });
      assert.deepEqual(retained.policy.obligations, before.policy.obligations);
    }
    assert.equal(JSON.parse(readFileSync(markerPath, 'utf8')).count, 1);
    assert.equal(fixtureRunning(), false);
  } catch (error) {
    testError = error;
  } finally {
    const errors = testError ? [testError] : [];
    try {
      // On pre-marker failure, allow the bounded product executor timeout to clean
      // up before interrupting dispatch. Never signal an unidentified executor.
      if (dispatcher && !marker && !exit) {
        await until(() => existsSync(markerPath) || exit !== undefined || closed, 7000, 'failure cleanup marker or dispatcher exit');
      }
      if (!marker && existsSync(markerPath)) marker = JSON.parse(readFileSync(markerPath, 'utf8'));
      await stopFixture();
    } catch (error) {
      errors.push(error);
    } finally {
      try {
        if (dispatcher && !exit) dispatcher.kill('SIGKILL');
        if (dispatcher) await until(() => closed, 3000, 'owned dispatcher cleanup');
      } catch (error) {
        errors.push(error);
      } finally {
        try {
          dispatcher?.stdin.destroy();
          dispatcher?.stdout.destroy();
          dispatcher?.stderr.destroy();
        } catch (error) {
          errors.push(error);
        } finally {
          try { rmSync(directory, { recursive: true, force: true }); }
          catch (error) { errors.push(error); }
        }
      }
    }
    // Preserve the original test/identity failure, including when cleanup also
    // fails. A failed ownership check never authorizes signalling that PID.
    if (errors.length === 1) throw errors[0];
    if (errors.length > 1) throw new AggregateError(errors, 'Test and/or owned-process cleanup failed');
  }
});
