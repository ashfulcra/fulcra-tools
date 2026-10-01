import { spawn } from 'node:child_process';
import { accessSync, constants, statSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';

const outputLimit = 64 * 1024;

export function validateClaudeExecutor(options) {
  const executable = options?.executable;
  const timeoutMs = options?.timeoutMs ?? 60000;
  if (typeof executable !== 'string' || !isAbsolute(executable) || resolve(executable) !== executable || /[\x00-\x1f\x7f]/.test(executable) || !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60000)
    throw new TypeError('Invalid Claude executor');
  try { if (!statSync(executable).isFile()) throw new Error(); accessSync(executable, constants.X_OK); }
  catch { throw new TypeError('Invalid Claude executor'); }
}

/** No shell, caller argv, tool access, implicit MCP servers, or raw diagnostics. */
export async function executeClaude(target, prompt, options) {
  try { if (!statSync(target.cwd).isDirectory()) throw new Error(); }
  catch { return { accepted: false, code: 'CWD_UNAVAILABLE' }; }
  return new Promise(resolveOutcome => {
    let child;
    try {
      child = spawn(options.executable, ['--print', '--resume', target.sessionId, '--model', 'haiku', '--max-budget-usd', '0.25', '--output-format', 'json', '--safe-mode', '--tools', '', '--disable-slash-commands', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--permission-mode', 'plan', '--no-chrome'], {
        cwd: target.cwd, shell: false, stdio: ['pipe', 'pipe', 'pipe']
      });
    } catch { resolveOutcome({ accepted: false, code: 'SPAWN_FAILED' }); return; }
    let reason;
    let bytes = 0;
    const chunks = [];
    const stop = code => {
      if (!reason) reason = code;
      child.kill('SIGKILL');
      // A descendant may inherit these pipes. Bound our wait without killing it.
      child.stdin.destroy();
      child.stdout.destroy();
      child.stderr.destroy();
    };
    const timer = setTimeout(() => stop('TIMEOUT'), options.timeoutMs ?? 60000);
    child.on('error', () => { reason = 'SPAWN_FAILED'; });
    child.stdin.on('error', () => { if (!reason) reason = 'INPUT_FAILED'; });
    const collect = (chunk, keep) => {
      bytes += chunk.length;
      if (bytes > outputLimit) stop('OUTPUT_LIMIT');
      else if (keep && !reason) chunks.push(chunk);
    };
    child.stdout.on('data', chunk => collect(chunk, true));
    child.stderr.on('data', chunk => collect(chunk, false));
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      if (reason) { resolveOutcome({ accepted: false, code: reason }); return; }
      if (code !== 0 || signal) { resolveOutcome({ accepted: false, code: 'NATIVE_FAILED' }); return; }
      try {
        const result = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
        if (result?.type !== 'result' || result.subtype !== 'success' || result.is_error !== false || result.session_id !== target.sessionId)
          throw new Error();
        resolveOutcome({ accepted: true, code: 'NATIVE_ACCEPTED' });
      } catch { resolveOutcome({ accepted: false, code: 'INVALID_NATIVE_RESULT' }); }
    });
    child.stdin.end(prompt);
  });
}
