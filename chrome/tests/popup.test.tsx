// chrome/tests/popup.test.tsx
import { describe, test, expect, beforeEach } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { App } from "../src/popup/App";

// React's act() expects this flag in a test environment; without it
// every render logs a spurious "not configured to support act" warning.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  await chrome.storage.session.clear();
});

describe("popup App", () => {
  test("mounts and renders the header without throwing", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    await act(async () => {
      createRoot(container).render(<App />);
    });
    // A blank popup = #root empty. The header text must be present.
    expect(container.textContent).toContain("Fulcra Attention");
  });
});
