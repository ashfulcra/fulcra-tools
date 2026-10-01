/** Raw native evidence only; injected callbacks do not attest host authority. */
export function validateCodexExecutor(options) {
  const timeoutMs = options?.timeoutMs ?? 60000;
  if (typeof options?.readThread !== 'function' || typeof options?.sendMessage !== 'function' ||
      !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60000 ||
      (options.now !== undefined && typeof options.now !== 'function'))
    throw new TypeError('Invalid Codex executor');
}

/** Reject ambiguous/error wrappers rather than guessing which payload is authority. */
function nativePayload(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.isError || raw.is_error || raw.error)
    return null;
  const candidates = [];
  if (Object.hasOwn(raw, 'structuredContent')) candidates.push(raw.structuredContent);
  if (Object.hasOwn(raw, 'content')) {
    if (!Array.isArray(raw.content) || raw.content.length !== 1 || raw.content[0]?.type !== 'text' ||
        typeof raw.content[0].text !== 'string' || raw.content[0].text.length > 64 * 1024) return null;
    try { candidates.push(JSON.parse(raw.content[0].text)); } catch { return null; }
  }
  if (!candidates.length) return raw;
  if (Object.hasOwn(raw, 'thread') || Object.hasOwn(raw, 'threadId')) return null;
  const payload = candidates[0];
  if (!payload || typeof payload !== 'object' || Array.isArray(payload) || payload.isError || payload.is_error || payload.error) return null;
  try {
    const json = JSON.stringify(payload);
    if (candidates.some(value => JSON.stringify(value) !== json)) return null;
  } catch { return null; }
  return payload;
}

/** Bounds observation, not callback execution; late results have no state-writing continuation. */
async function observe(callback, args, timeoutMs) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(() => callback(args)).then(value => ({ value }), () => ({ code: 'NATIVE_FAILED' })),
      new Promise(resolve => { timer = setTimeout(() => resolve({ code: 'TIMEOUT' }), timeoutMs); })
    ]);
  } finally { clearTimeout(timer); }
}

export async function preflightCodex(target, options) {
  const observed = await observe(options.readThread, { ...target }, options.timeoutMs ?? 60000);
  if (observed.code) return { code: observed.code === 'TIMEOUT' ? 'PREFLIGHT_TIMEOUT' : 'PREFLIGHT_UNAVAILABLE' };
  const result = nativePayload(observed.value);
  if (result?.schemaVersion !== 1 || !result.thread ||
      (Object.hasOwn(result.thread, 'kind') && result.thread.kind !== 'codex') || result.thread.id !== target.threadId ||
      result.thread.hostId !== target.hostId) return { code: 'INVALID_THREAD_RESULT' };
  if (result.thread.status?.type !== 'idle') return { code: 'THREAD_NOT_IDLE' };
  return { code: 'THREAD_IDLE' };
}

export async function executeCodex(target, prompt, options) {
  const observed = await observe(options.sendMessage, { ...target, prompt }, options.timeoutMs ?? 60000);
  if (observed.code) return { accepted: false, code: observed.code };
  const result = nativePayload(observed.value);
  const accepted = result?.threadId === target.threadId;
  return { accepted, code: accepted ? 'NATIVE_ACCEPTED' : 'INVALID_NATIVE_RESULT' };
}
