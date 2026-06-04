// chrome/tests/popup.test.tsx
import { describe, test, expect, beforeEach, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { App } from "../src/popup/App";
import { SignIn } from "../src/popup/SignIn";
import { IdentityLabel } from "../src/popup/IdentityLabel";
import { saveSettings, loadSettings } from "../src/storage";
import { DEFAULT_SETTINGS } from "../src/types";

// React's act() expects this flag in a test environment; without it
// every render logs a spurious "not configured to support act" warning.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  await chrome.storage.session.clear();
  vi.restoreAllMocks();
});

/** Mount a component, returning {container, root}. Flushes effects. */
async function mount(node: React.ReactElement): Promise<{
  container: HTMLElement;
  root: Root;
}> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root!: Root;
  await act(async () => {
    root = createRoot(container);
    root.render(node);
  });
  // Let mount effects (async loadSettings / token checks) settle.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  return { container, root };
}

/** Click the first button whose text contains `label`. */
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

describe("popup App", () => {
  test("mounts and renders the header without throwing", async () => {
    const { container } = await mount(<App />);
    expect(container.textContent).toContain("Fulcra Attention");
  });

  test("signed out shows 'Connect to Fulcra', no paste-token form", async () => {
    await saveSettings({
      ...DEFAULT_SETTINGS,
      onboarded: true,
    });
    const { container } = await mount(<App />);
    expect(container.textContent).toContain("Connect to Fulcra");
    // No daemon paste-token field anymore.
    expect(container.querySelector('input[type="password"]')).toBeNull();
  });

  test("unauthorized banner says 'Sign in to Fulcra', not 'Reconnect'", async () => {
    await saveSettings({
      ...DEFAULT_SETTINGS,
      onboarded: true,
    });
    await chrome.storage.local.set({
      lastIngestError: { kind: "unauthorized", at: 1 },
    });
    const { container } = await mount(<App />);
    expect(container.textContent).toContain("Sign in to Fulcra");
    expect(container.textContent).not.toContain("Reconnect");
  });
});

describe("SignIn surface", () => {
  test("already signed in on mount → 'Signed in as <email>'", async () => {
    const tokenStore = {
      getValidAccessToken: vi.fn(async () => "ACCESS-TOKEN"),
      clear: vi.fn(async () => undefined),
    } as never;
    const { container } = await mount(
      <SignIn
        runSignIn={(async () => ({ ok: true })) as never}
        tokenStore={tokenStore}
        resolveLabel={async () => "user@example.com"}
        openUrl={vi.fn()}
        clearResolved={async () => undefined}
      />,
    );
    expect(container.textContent).toContain("Signed in as user@example.com");
  });

  test("clicking sign-in runs the device flow: renders the code, opens the URL, then 'Signed in as <email>'", async () => {
    const openUrl = vi.fn();
    const runSignIn = vi.fn(async (opts: { onPrompt: (u: string, c: string) => void }) => {
      opts.onPrompt("https://verify.example/?code=WXYZ-9999", "WXYZ-9999");
      return { ok: true as const };
    });
    // First call (mount check) → null (signed out); subsequent → token.
    let calls = 0;
    const tokenStore = {
      getValidAccessToken: vi.fn(async () => (calls++ === 0 ? null : "ACCESS")),
      clear: vi.fn(async () => undefined),
    } as never;
    const resolveLabel = vi.fn(async () => "user@example.com");

    const { container } = await mount(
      <SignIn
        runSignIn={runSignIn as never}
        tokenStore={tokenStore}
        resolveLabel={resolveLabel}
        openUrl={openUrl}
        clearResolved={async () => undefined}
      />,
    );

    expect(container.textContent).toContain("Connect to Fulcra");

    await clickButton(container, "Connect to Fulcra");

    // onPrompt rendered the user code and opened the verification URL.
    expect(openUrl).toHaveBeenCalledWith("https://verify.example/?code=WXYZ-9999");
    // After resolution → signed in with the resolved label.
    expect(container.textContent).toContain("Signed in as user@example.com");
  });

  test("prompting phase shows the user code + 'waiting for approval' before approval lands", async () => {
    const openUrl = vi.fn();
    // Hold the device flow open at the prompting phase: fire onPrompt, then
    // return a promise that we control so the UI stays in 'prompting'.
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const runSignIn = vi.fn(async (opts: { onPrompt: (u: string, c: string) => void }) => {
      opts.onPrompt("https://verify.example/?code=PROMPT-CODE", "PROMPT-CODE");
      await gate;
      return { ok: true as const };
    });
    const tokenStore = {
      getValidAccessToken: vi.fn(async () => null),
      clear: vi.fn(async () => undefined),
    } as never;

    const { container } = await mount(
      <SignIn
        runSignIn={runSignIn as never}
        tokenStore={tokenStore}
        resolveLabel={async () => "user@example.com"}
        openUrl={openUrl}
        clearResolved={async () => undefined}
      />,
    );

    await clickButton(container, "Connect to Fulcra");

    // Still mid-flow: the user code and the waiting state are visible.
    expect(container.textContent).toContain("PROMPT-CODE");
    expect(container.textContent).toContain("Waiting for approval");
    expect(openUrl).toHaveBeenCalledWith("https://verify.example/?code=PROMPT-CODE");

    // Release the flow → resolves to idle/signed-in (token still null here).
    await act(async () => {
      release();
      await Promise.resolve();
      await Promise.resolve();
    });
  });

  test("sign-out clears tokens + resolved-attention cache and returns to idle", async () => {
    const clear = vi.fn(async () => undefined);
    const clearResolved = vi.fn(async () => undefined);
    const tokenStore = {
      getValidAccessToken: vi.fn(async () => "ACCESS"),
      clear,
    } as never;

    const { container } = await mount(
      <SignIn
        runSignIn={(async () => ({ ok: true })) as never}
        tokenStore={tokenStore}
        resolveLabel={async () => "user@example.com"}
        openUrl={vi.fn()}
        clearResolved={clearResolved}
      />,
    );

    expect(container.textContent).toContain("Signed in");
    await clickButton(container, "Sign out");

    expect(clear).toHaveBeenCalledTimes(1);
    expect(clearResolved).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("Connect to Fulcra");
  });

  test("device-flow failure shows an error + Try again", async () => {
    const runSignIn = vi.fn(async () => {
      throw new Error("device authorization failed: access_denied");
    });
    const tokenStore = {
      getValidAccessToken: vi.fn(async () => null),
      clear: vi.fn(async () => undefined),
    } as never;

    const { container } = await mount(
      <SignIn
        runSignIn={runSignIn as never}
        tokenStore={tokenStore}
        resolveLabel={async () => null}
        openUrl={vi.fn()}
        clearResolved={async () => undefined}
      />,
    );

    await clickButton(container, "Connect to Fulcra");
    expect(container.textContent).toContain("Sign-in failed");
    expect(container.textContent).toContain("Try again");
  });
});

/** Type a value into the first text input (React-controlled). */
async function setTextInput(container: HTMLElement, value: string): Promise<void> {
  const input = container.querySelector<HTMLInputElement>('input[type="text"]');
  if (!input) throw new Error("no text input found");
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype, "value",
  )!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("IdentityLabel — this browser", () => {
  const tokenStore = {
    getValidAccessToken: vi.fn(async () => "ACCESS"),
    clear: vi.fn(async () => undefined),
  } as never;

  test("renders the current identity label", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: "Work MBP — Chrome" });
    const update = vi.fn(async () => null);
    const { container } = await mount(
      <IdentityLabel tokenStore={tokenStore} update={update} />,
    );
    expect(container.textContent).toContain("This browser");
    const input = container.querySelector<HTMLInputElement>('input[type="text"]');
    expect(input?.value).toBe("Work MBP — Chrome");
  });

  test("saving persists the label (saveSettings) and re-tags (updateIdentity)", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: null });
    const update = vi.fn(async () => ({ definitionId: "d", tagIds: [] }));
    const { container } = await mount(
      <IdentityLabel tokenStore={tokenStore} update={update} />,
    );

    await setTextInput(container, "Home iMac");
    await clickButton(container, "Save");

    const stored = await loadSettings();
    expect(stored.identityLabel).toBe("Home iMac");
    expect(update).toHaveBeenCalledTimes(1);
    expect(update).toHaveBeenCalledWith(expect.anything(), "Home iMac");
  });

  test("not yet onboarded: updateIdentity no-ops (null) but the label still persists", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, identityLabel: null });
    const update = vi.fn(async () => null); // no cached definition
    const { container } = await mount(
      <IdentityLabel tokenStore={tokenStore} update={update} />,
    );

    await setTextInput(container, "Spare Browser");
    await clickButton(container, "Save");

    const stored = await loadSettings();
    expect(stored.identityLabel).toBe("Spare Browser");
    expect(update).toHaveBeenCalledWith(expect.anything(), "Spare Browser");
  });
});
