// chrome/tests/wizard.test.tsx
//
// The onboarding wizard's auth step (step 2) is transport-aware:
//   relayless (default) → device-flow sign-in surface (reuses popup SignIn)
//   relay               → daemon bearer-token paste form (unchanged)
//
// startDeviceSignIn is mocked at the module boundary so the SignIn surface
// embedded in the wizard drives without network/chrome identity.

import { describe, test, expect, beforeEach, vi } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

// Mock the device-flow runner the embedded SignIn calls. Each test sets the
// behaviour via this handle before mounting.
const signInImpl = { run: vi.fn() };
vi.mock("../src/relayless/signIn", () => ({
  startDeviceSignIn: (opts: unknown) => signInImpl.run(opts),
}));

// Mock the TokenStore so "already signed in" vs "signed out" is controllable.
const tokenImpl = { getValidAccessToken: vi.fn(async () => null as string | null) };
vi.mock("../src/relayless/tokenStore", () => ({
  TokenStore: class {
    getValidAccessToken = (...a: unknown[]) => tokenImpl.getValidAccessToken(...(a as []));
    clear = vi.fn(async () => undefined);
  },
}));

// whoami / ensureDefinition pull in chrome + network at import; stub them out.
vi.mock("../src/relayless/whoami", () => ({
  whoami: vi.fn(async () => ({ label: null })),
}));
const ensureImpl = {
  listAttentionDestinations: vi.fn(async () => [] as unknown[]),
  chooseAttentionDestination: vi.fn(async () => ({ definitionId: "d", tagIds: [] })),
  createAttentionDestination: vi.fn(async () => ({ definitionId: "d", tagIds: [] })),
};
vi.mock("../src/relayless/ensureDefinition", () => ({
  clearResolvedAttention: vi.fn(async () => undefined),
  ATTENTION_DEFINITION_NAME: "Attention",
  listAttentionDestinations: (...a: unknown[]) => ensureImpl.listAttentionDestinations(...(a as [])),
  chooseAttentionDestination: (...a: unknown[]) => ensureImpl.chooseAttentionDestination(...(a as [])),
  createAttentionDestination: (...a: unknown[]) => ensureImpl.createAttentionDestination(...(a as [])),
}));

import { Wizard } from "../src/wizard/Wizard";
import { saveSettings } from "../src/storage";
import { DEFAULT_SETTINGS } from "../src/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(async () => {
  await chrome.storage.local.clear();
  await chrome.storage.sync.clear();
  await chrome.storage.session.clear();
  vi.clearAllMocks();
  tokenImpl.getValidAccessToken.mockResolvedValue(null);
  signInImpl.run.mockReset();
  ensureImpl.listAttentionDestinations.mockResolvedValue([]);
  ensureImpl.chooseAttentionDestination.mockResolvedValue({ definitionId: "d", tagIds: [] });
  ensureImpl.createAttentionDestination.mockResolvedValue({ definitionId: "d", tagIds: [] });
});

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

/** Advance the wizard from welcome to the auth step. */
async function gotoAuthStep(container: HTMLElement): Promise<void> {
  await clickButton(container, "Get started");
}

describe("Wizard auth step — relayless (default)", () => {
  test("shows the sign-in surface, NOT the token-paste input", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, transportMode: "relayless" });
    const { container } = await mount(<Wizard />);
    await gotoAuthStep(container);

    expect(container.textContent).toContain("Sign in to Fulcra");
    expect(container.textContent).toContain("Sign in with Fulcra");
    // No daemon bearer-token paste field.
    expect(container.querySelector('input[type="password"]')).toBeNull();
  });

  test("completing sign-in advances the wizard to the destination step", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, transportMode: "relayless" });
    // First token check (mount) → null (signed out); after the flow → a token.
    let calls = 0;
    tokenImpl.getValidAccessToken.mockImplementation(async () =>
      calls++ === 0 ? null : "ACCESS",
    );
    signInImpl.run.mockImplementation(
      async (opts: { onPrompt: (u: string, c: string) => void }) => {
        opts.onPrompt("https://verify.example/?code=ABCD", "ABCD");
        return { ok: true as const };
      },
    );

    const { container } = await mount(<Wizard />);
    await gotoAuthStep(container);
    await clickButton(container, "Sign in with Fulcra");

    // Landed on the destination step (NOT scan directly).
    expect(container.textContent).toContain("Choose where your browsing attention is saved");
    expect(container.textContent).not.toContain("Scan your history");
    expect(container.textContent).not.toContain("Sign in with Fulcra");
  });

  test("already signed in → 'Continue' advances to destination, no forced re-auth", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, transportMode: "relayless" });
    tokenImpl.getValidAccessToken.mockResolvedValue("EXISTING-TOKEN");

    const { container } = await mount(<Wizard />);
    await gotoAuthStep(container);

    // Signed-in affordance present; no sign-in button / device flow.
    expect(container.textContent).toContain("Signed in");
    expect(container.textContent).not.toContain("Sign in with Fulcra");

    await clickButton(container, "Continue");

    expect(signInImpl.run).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Choose where your browsing attention is saved");
  });
});

describe("Wizard destination step — relayless", () => {
  /** Sign in (already-signed-in path) and land on the destination step. */
  async function gotoDestinationStep(): Promise<{ container: HTMLElement }> {
    await saveSettings({ ...DEFAULT_SETTINGS, transportMode: "relayless" });
    tokenImpl.getValidAccessToken.mockResolvedValue("EXISTING-TOKEN");
    const { container } = await mount(<Wizard />);
    await gotoAuthStep(container);
    await clickButton(container, "Continue");
    // Let the on-mount listAttentionDestinations settle.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    return { container };
  }

  test("lists existing destinations and marks the auto-pick (current)", async () => {
    ensureImpl.listAttentionDestinations.mockResolvedValue([
      { id: "old", name: "Attention", createdAt: "2026-01-01T00:00:00Z", isAutoPick: true },
      { id: "new", name: "Attention", createdAt: "2026-03-01T00:00:00Z", isAutoPick: false },
    ]);
    const { container } = await gotoDestinationStep();

    expect(ensureImpl.listAttentionDestinations).toHaveBeenCalled();
    expect(container.textContent).toContain('"Attention"');
    expect(container.textContent).toContain("(current)");
    // The auto-pick radio is selected by default.
    const radios = [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    const checked = radios.find((r) => r.checked);
    expect(checked?.value).toBe("old");
  });

  test("Continue with an existing destination selected calls chooseAttentionDestination then advances to scan", async () => {
    ensureImpl.listAttentionDestinations.mockResolvedValue([
      { id: "old", name: "Attention", createdAt: "2026-01-01T00:00:00Z", isAutoPick: true },
    ]);
    const { container } = await gotoDestinationStep();

    await clickButton(container, "Continue");
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(ensureImpl.chooseAttentionDestination).toHaveBeenCalledWith(
      expect.anything(),
      "old",
    );
    expect(ensureImpl.createAttentionDestination).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Scan your history");
  });

  test("empty list defaults to create; Continue calls createAttentionDestination then advances to scan", async () => {
    ensureImpl.listAttentionDestinations.mockResolvedValue([]);
    const { container } = await gotoDestinationStep();

    // Create option is the default selection when nothing exists.
    expect(container.textContent).toContain("Create a new Attention annotation");

    await clickButton(container, "Continue");
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(ensureImpl.createAttentionDestination).toHaveBeenCalledWith(
      expect.anything(),
      "Attention",
    );
    expect(ensureImpl.chooseAttentionDestination).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Scan your history");
  });

  test("surfaces a load error instead of swallowing it", async () => {
    ensureImpl.listAttentionDestinations.mockRejectedValue(new Error("boom-load"));
    const { container } = await gotoDestinationStep();
    expect(container.textContent).toContain("boom-load");
  });

  test("surfaces a save error and stays on the destination step", async () => {
    ensureImpl.listAttentionDestinations.mockResolvedValue([
      { id: "old", name: "Attention", createdAt: "2026-01-01T00:00:00Z", isAutoPick: true },
    ]);
    ensureImpl.chooseAttentionDestination.mockRejectedValue(new Error("boom-save"));
    const { container } = await gotoDestinationStep();

    await clickButton(container, "Continue");
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(container.textContent).toContain("boom-save");
    expect(container.textContent).not.toContain("Scan your history");
  });
});

describe("Wizard auth step — relay (unchanged)", () => {
  test("shows the token-paste form and advances via saveTokenAndAdvance", async () => {
    await saveSettings({ ...DEFAULT_SETTINGS, transportMode: "relay" });
    const { container } = await mount(<Wizard />);
    await gotoAuthStep(container);

    // Relay keeps the daemon paste form, not the sign-in surface.
    expect(container.textContent).toContain("Connect to Fulcra Collect");
    expect(container.textContent).not.toContain("Sign in with Fulcra");
    const input = container.querySelector<HTMLInputElement>('input[type="password"]');
    expect(input).not.toBeNull();

    // Type a token and continue.
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value",
      )!.set!;
      setter.call(input, "tok-123");
      input!.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });

    await clickButton(container, "Continue");

    // Advanced to scan, and the token persisted to settings.
    expect(container.textContent).toContain("Scan your history");
    const { loadSettings } = await import("../src/storage");
    expect((await loadSettings()).bearerToken).toBe("tok-123");
    expect(signInImpl.run).not.toHaveBeenCalled();
  });
});
