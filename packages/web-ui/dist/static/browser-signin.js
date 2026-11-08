"use strict";

// One browser-link flow for onboarding and reconnect. A CLI process exiting
// successfully is not approval; only the completion endpoint's explicit result
// can let a caller refresh its signed-in view. Device codes stay in this closure.
async function browserSignin(apiCall, onPrompt, registerCancel = () => {}) {
  let cancelled = false, attemptId = null;
  const cancel = async () => {
    cancelled = true;
    if (attemptId) await apiCall("/api/fulcra/auth/cancel", {
      method: "POST", body: JSON.stringify({attempt_id: attemptId}),
    });
  };
  registerCancel(cancel);
  const start = await apiCall("/api/fulcra/auth/cli_login_start", {
    method: "POST", body: JSON.stringify({}),
  });
  if (!start || start.protocol !== 2 || typeof start.attempt_id !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(start.attempt_id) ||
      !Number.isFinite(start.expires_in) || start.expires_in <= 0 || start.expires_in > 1800) {
    throw new Error("Refresh Collect and start a new browser sign-in.");
  }
  attemptId = start.attempt_id;
  if (cancelled) {
    await cancel();
    throw new Error("Sign-in cancelled.");
  }
  const deadline = Date.now() + start.expires_in * 1000;
  const authUrl = start && start.auth_url;
  const webCode = start && start.web_auth_code;
  const deviceCode = start && start.device_code;
  if (typeof authUrl !== "string" || authUrl.length > 4096 ||
      typeof webCode !== "string" || !webCode || webCode.length > 64 ||
      typeof deviceCode !== "string" || !deviceCode || deviceCode.length > 4096) {
    throw new Error("Could not verify the sign-in link. Update Collect and try again.");
  }
  let parsed;
  try { parsed = new URL(authUrl); } catch {
    throw new Error("Could not verify the sign-in link. Try again.");
  }
  const codes = parsed.searchParams.getAll("user_code");
  if (parsed.origin !== "https://fulcra.us.auth0.com" || parsed.username || parsed.password ||
      codes.length !== 1 || codes[0] !== webCode) {
    throw new Error("Could not verify the sign-in link. Try again.");
  }
  onPrompt({authUrl, webCode});
  for (let polls = 0; polls < 361 && Date.now() < deadline; polls++) {
    if (cancelled) throw new Error("Sign-in cancelled.");
    const result = await apiCall("/api/fulcra/auth/cli_login_poll", {
      method: "POST", body: JSON.stringify({protocol: 2, attempt_id: attemptId, device_code: deviceCode}),
    });
    if (cancelled) throw new Error("Sign-in cancelled.");
    if (result && result.protocol === 2 && result.ok === true) return;
    if (!result || result.protocol !== 2 || result.ok !== false || result.pending !== true ||
        !Number.isFinite(result.retry_after) || result.retry_after < 1 || result.retry_after > 60) {
      throw new Error("Sign-in didn't complete. Please try again.");
    }
    if (Date.now() + result.retry_after * 1000 >= deadline) break;
    await new Promise(resolve => setTimeout(resolve, result.retry_after * 1000));
  }
  throw new Error("The sign-in link expired. Start again for a new link.");
}
