// chrome/tests/page-context-flush.test.tsx
//
// Bug A3: page contexts (popup SignIn, popup Banner) must NOT call
// flushOutbox() directly — that runs a SECOND, concurrent flush in a
// separate JS context where the module-scope single-flight guard can't see
// the SW's in-flight flush, re-POSTing the same snapshot (duplicate-storm).
// They must instead call requestFlush(), which asks the SW to flush.
import { describe, test, expect, beforeEach, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

// requestFlush is the corrected contract. Mock it so we can assert the page
// asks the SW to flush rather than flushing in-context.
vi.mock("../src/flushRequest", () => ({ requestFlush: vi.fn() }));
// flushOutbox must NOT be called from these page contexts; spy to prove it.
vi.mock("../src/outbox", async () => {
  const actual = await vi.importActual<typeof import("../src/outbox")>("../src/outbox");
  return { ...actual, flushOutbox: vi.fn(async () => undefined) };
});

import { requestFlush } from "../src/flushRequest";
import { flushOutbox } from "../src/outbox";
import { SignIn } from "../src/popup/SignIn";
import { Banner } from "../src/popup/Banner";
import { addToOutbox } from "../src/outbox";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  await chrome.storage.session.clear();
  vi.mocked(requestFlush).mockClear();
  vi.mocked(flushOutbox).mockClear();
});

async function mount(node: React.ReactElement): Promise<{ container: HTMLElement; root: Root }> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root!: Root;
  await act(async () => {
    root = createRoot(container);
    root.render(node);
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  return { container, root };
}

async function clickButton(container: HTMLElement, label: string): Promise<void> {
  const btn = [...container.querySelectorAll("button")].find((b) =>
    (b.textContent ?? "").includes(label),
  );
  if (!btn) throw new Error(`no button containing "${label}"`);
  await act(async () => {
    btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("SignIn post-sign-in flush", () => {
  test("a successful sign-in calls requestFlush, not flushOutbox", async () => {
    const runSignIn = vi.fn(async (opts: { onPrompt: (u: string, c: string) => void }) => {
      opts.onPrompt("https://verify.example/?code=WXYZ", "WXYZ");
      return { ok: true as const };
    });
    let calls = 0;
    const tokenStore = {
      getValidAccessToken: vi.fn(async () => (calls++ === 0 ? null : "ACCESS")),
      clear: vi.fn(async () => undefined),
    } as never;

    const { container } = await mount(
      <SignIn
        runSignIn={runSignIn as never}
        tokenStore={tokenStore}
        resolveLabel={async () => "user@example.com"}
        openUrl={vi.fn()}
        clearResolved={async () => undefined}
      />,
    );

    await clickButton(container, "Connect to Fulcra");
    expect(container.textContent).toContain("Signed in as user@example.com");

    expect(requestFlush).toHaveBeenCalledTimes(1);
    expect(flushOutbox).not.toHaveBeenCalled();
  });
});

describe("Banner manual flush", () => {
  test("'Flush now' calls requestFlush, not flushOutbox", async () => {
    // Seed a queued event so the "Flush now" affordance renders.
    await addToOutbox({
      source_id: "s1", client: "c", url: "https://x", title: null,
      og_description: null, og_type: null, favicon_url: null, lang: null,
      category: null, start_time: "2026-01-01T00:00:00Z",
      end_time: "2026-01-01T00:01:00Z",
    } as never);

    const { container } = await mount(<Banner />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    await clickButton(container, "Flush now");

    expect(requestFlush).toHaveBeenCalledTimes(1);
    expect(flushOutbox).not.toHaveBeenCalled();
  });
});
