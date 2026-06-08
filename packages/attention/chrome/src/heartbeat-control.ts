// chrome/src/heartbeat-control.ts
//
// User-side controls for the optional heartbeat content script. Called
// by the popup toggle and the wizard onboarding step.
//
// The toggle is two-state from the user's POV: ON or OFF. From Chrome's
// POV there are three states:
//   (a) Settings.heartbeatEnabled=false, no permission granted
//   (b) Settings.heartbeatEnabled=true,  permission granted, script registered
//   (c) inconsistent — settings says ON but permission was revoked externally
//       (Chrome lets users revoke optional permissions from the extensions
//        page); we treat (c) the same as (a) and resync on next toggle.

import { loadSettings, saveSettings } from "./storage";

const HEARTBEAT_SCRIPT_ID = "fulcra-heartbeat";
const HEARTBEAT_PERMISSION: chrome.permissions.Permissions = {
  origins: ["http://*/*", "https://*/*"],
};

/** Whether Chrome currently grants the host permission we need. */
export async function hasHeartbeatPermission(): Promise<boolean> {
  try {
    return await chrome.permissions.contains(HEARTBEAT_PERMISSION);
  } catch {
    return false;
  }
}

/**
 * Ask Chrome for the optional host permission. MUST be called from a
 * user gesture (button click) or Chrome rejects the request silently.
 * Returns true if the user granted it (or it was already granted).
 */
export async function requestHeartbeatPermission(): Promise<boolean> {
  try {
    return await chrome.permissions.request(HEARTBEAT_PERMISSION);
  } catch {
    return false;
  }
}

/**
 * Drop the host permission. Lets users fully revoke from the popup
 * without going to chrome://extensions.
 */
export async function revokeHeartbeatPermission(): Promise<void> {
  try {
    await chrome.permissions.remove(HEARTBEAT_PERMISSION);
  } catch {
    // some Chrome versions return false for required perms — ignore
  }
}

/**
 * Whether the content script is currently registered with Chrome.
 */
async function isScriptRegistered(): Promise<boolean> {
  try {
    const scripts = await chrome.scripting.getRegisteredContentScripts({
      ids: [HEARTBEAT_SCRIPT_ID],
    });
    return scripts.length > 0;
  } catch {
    return false;
  }
}

/**
 * Register the heartbeat content script. Idempotent.
 */
async function registerHeartbeatScript(): Promise<void> {
  if (await isScriptRegistered()) return;
  try {
    await chrome.scripting.registerContentScripts([{
      id: HEARTBEAT_SCRIPT_ID,
      matches: ["http://*/*", "https://*/*"],
      // public/heartbeat.js → dist/heartbeat.js at build time.
      js: ["heartbeat.js"],
      runAt: "document_start",
      allFrames: false,
      persistAcrossSessions: true,
    }]);
  } catch {
    // Already-registered race or transient SW lifecycle. Best effort.
  }
}

async function unregisterHeartbeatScript(): Promise<void> {
  try {
    await chrome.scripting.unregisterContentScripts({ ids: [HEARTBEAT_SCRIPT_ID] });
  } catch {
    // Already unregistered or no such script. Best effort.
  }
}

/**
 * Single public entry point for the popup and wizard. Pass `true` to
 * enable, `false` to disable. Returns the resulting actual state
 * (which may differ from the request if the user declined the
 * permission prompt).
 */
export async function setHeartbeatEnabled(want: boolean): Promise<boolean> {
  if (want) {
    const granted = await requestHeartbeatPermission();
    if (!granted) {
      // User declined the prompt. Leave settings in sync.
      const s = await loadSettings();
      await saveSettings({ ...s, heartbeatEnabled: false });
      return false;
    }
    await registerHeartbeatScript();
    const s = await loadSettings();
    await saveSettings({ ...s, heartbeatEnabled: true });
    return true;
  }
  // Disabling: unregister + drop the permission so the user can see in
  // chrome://extensions that we no longer have <all_urls>.
  await unregisterHeartbeatScript();
  await revokeHeartbeatPermission();
  const s = await loadSettings();
  await saveSettings({ ...s, heartbeatEnabled: false });
  return false;
}

/**
 * Called from the SW boot path. If the user enabled the heartbeat in a
 * previous session, re-register the script — chrome.scripting
 * registrations survive SW restarts via persistAcrossSessions, but we
 * defensively re-register so a manifest/dist update doesn't strand the
 * setting in an inconsistent state.
 */
export async function reconcileHeartbeatOnBoot(): Promise<void> {
  const s = await loadSettings();
  if (!s.heartbeatEnabled) {
    await unregisterHeartbeatScript();
    return;
  }
  if (!(await hasHeartbeatPermission())) {
    // User revoked the host permission from chrome://extensions while
    // settings.heartbeatEnabled stayed true. Resync.
    await saveSettings({ ...s, heartbeatEnabled: false });
    await unregisterHeartbeatScript();
    return;
  }
  await registerHeartbeatScript();
}
