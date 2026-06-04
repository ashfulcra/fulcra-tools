// chrome/tests/wizard.test.tsx
//
// The onboarding wizard's auth step (step 2) is the Fulcra device-flow
// sign-in surface (reuses the popup SignIn). There is no longer a relay /
// daemon paste-token path.
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
const whoamiImpl = { whoami: vi.fn(async () => ({ label: null as string | null })) };
vi.mock("../src/relayless/whoami", () => ({
  whoami: (...a: unknown[]) => whoamiImpl.whoami(...(a as [])),
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
import { saveSettings, loadSettings } from "../src/storage";
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
  whoamiImpl.whoami.mockResolvedValue({ label: null });
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

describe("Wizard auth step — relayless (default)", () => {
  test("shows the sign-in surface, NOT the token-paste input", async () => {
    await saveSettings(DEFAULT_SETTINGS);
    const { container } = await mount(<Wizard />);
    await gotoAuthStep(container);

    expect(container.textContent).toContain("Sign in to Fulcra");
    expect(container.textContent).toContain("Sign in with Fulcra");
    // No daemon bearer-token paste field.
    expect(container.querySelector('input[type="password"]')).toBeNull();
  });

  test("completing sign-in advances the wizard to the destination step", async () => {
    await saveSettings(DEFAULT_SETTINGS);
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
    await saveSettings(DEFAULT_SETTINGS);
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
    await saveSettings(DEFAULT_SETTINGS);
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

  test("shows the 'Name this browser' field defaulting from whoami", async () => {
    whoamiImpl.whoami.mockResolvedValue({ label: "user@example.com" });
    ensureImpl.listAttentionDestinations.mockResolvedValue([]);
    const { container } = await gotoDestinationStep();

    expect(container.textContent).toContain("Name this browser");
    const input = container.querySelector<HTMLInputElement>('input[type="text"]');
    expect(input?.value).toBe("user@example.com browser");
  });

  test("Continue is blocked while the label is blank, enabled once filled", async () => {
    whoamiImpl.whoami.mockResolvedValue({ label: null }); // no prefill → blank
    ensureImpl.listAttentionDestinations.mockResolvedValue([
      { id: "old", name: "Attention", createdAt: "2026-01-01T00:00:00Z", isAutoPick: true },
    ]);
    const { container } = await gotoDestinationStep();

    const cont = [...container.querySelectorAll("button")].find((b) =>
      (b.textContent ?? "").includes("Continue"),
    ) as HTMLButtonElement;
    expect(cont.disabled).toBe(true);

    await setTextInput(container, "My Laptop");
    expect(cont.disabled).toBe(false);
  });

  test("Continue with an existing destination persists identityLabel + calls choose with the label then advances to scan", async () => {
    ensureImpl.listAttentionDestinations.mockResolvedValue([
      { id: "old", name: "Attention", createdAt: "2026-01-01T00:00:00Z", isAutoPick: true },
    ]);
    const { container } = await gotoDestinationStep();

    await setTextInput(container, "Work Laptop");
    await clickButton(container, "Continue");
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(ensureImpl.chooseAttentionDestination).toHaveBeenCalledWith(
      expect.anything(),
      "old",
      "Work Laptop",
    );
    expect(ensureImpl.createAttentionDestination).not.toHaveBeenCalled();
    // identityLabel persisted to settings.
    const stored = await loadSettings();
    expect(stored.identityLabel).toBe("Work Laptop");
    expect(container.textContent).toContain("Scan your history");
  });

  test("empty list defaults to create; Continue persists label + calls create with the label then advances to scan", async () => {
    ensureImpl.listAttentionDestinations.mockResolvedValue([]);
    const { container } = await gotoDestinationStep();

    // Create option is the default selection when nothing exists.
    expect(container.textContent).toContain("Create a new Attention annotation");

    await setTextInput(container, "Home iMac");
    await clickButton(container, "Continue");
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(ensureImpl.createAttentionDestination).toHaveBeenCalledWith(
      expect.anything(),
      "Attention",
      "Home iMac",
    );
    expect(ensureImpl.chooseAttentionDestination).not.toHaveBeenCalled();
    const stored = await loadSettings();
    expect(stored.identityLabel).toBe("Home iMac");
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

    await setTextInput(container, "Some Browser");
    await clickButton(container, "Continue");
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(container.textContent).toContain("boom-save");
    expect(container.textContent).not.toContain("Scan your history");
  });
});
