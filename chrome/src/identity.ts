// chrome/src/identity.ts
//
// Capture chrome_identity for the AttentionEvent payload.
// Order of preference (most specific first):
//   1. User-set label in Settings.identityLabel (free text from popup)
//   2. Google account email from chrome.identity.getProfileUserInfo()
//   3. null

import { loadSettings } from "./storage";

function profileUserInfo(): Promise<chrome.identity.UserInfo> {
  return new Promise((resolve) => {
    chrome.identity.getProfileUserInfo({ accountStatus: "ANY" }, (info) => resolve(info));
  });
}

export async function getChromeIdentity(): Promise<string | null> {
  const settings = await loadSettings();
  if (settings.identityLabel && settings.identityLabel.trim() !== "") {
    return settings.identityLabel.trim();
  }
  const info = await profileUserInfo();
  if (info.email && info.email !== "") return info.email;
  return null;
}
