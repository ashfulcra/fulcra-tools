// chrome/tests/authOriginFix.test.ts
import { describe, test, expect, vi } from "vitest";
import {
  AUTH0_ORIGIN_STRIP_RULE_ID,
  registerAuth0OriginStrip,
} from "../src/relayless/authOriginFix";

function makeDnr() {
  return {
    updateDynamicRules: vi.fn(async () => undefined),
  } as unknown as typeof chrome.declarativeNetRequest;
}

describe("registerAuth0OriginStrip", () => {
  test("registers a single dynamic rule that strips the Origin header on POSTs to Auth0", async () => {
    const dnr = makeDnr();
    await registerAuth0OriginStrip(dnr, "testextid");

    expect(dnr.updateDynamicRules).toHaveBeenCalledTimes(1);
    const arg = (dnr.updateDynamicRules as ReturnType<typeof vi.fn>).mock
      .calls[0][0];

    // Idempotency: it removes its own rule id before re-adding.
    expect(arg.removeRuleIds).toContain(AUTH0_ORIGIN_STRIP_RULE_ID);

    expect(arg.addRules).toHaveLength(1);
    const rule = arg.addRules[0];
    expect(rule.id).toBe(AUTH0_ORIGIN_STRIP_RULE_ID);
    expect(rule.priority).toBe(1);

    // Action: modifyHeaders removing the origin request header.
    expect(rule.action.type).toBe("modifyHeaders");
    expect(rule.action.requestHeaders).toEqual([
      { header: "origin", operation: "remove" },
    ]);

    // Condition: only the extension's own POSTs to the Auth0 host.
    expect(rule.condition.requestDomains).toEqual(["fulcra.us.auth0.com"]);
    expect(rule.condition.initiatorDomains).toEqual(["testextid"]);
    expect(rule.condition.requestMethods).toEqual(["post"]);
    expect(rule.condition.resourceTypes).toEqual(["xmlhttprequest", "other"]);
  });

  test("swallows (logs, does not throw) when updateDynamicRules rejects", async () => {
    const dnr = {
      updateDynamicRules: vi.fn(async () => {
        throw new Error("boom");
      }),
    } as unknown as typeof chrome.declarativeNetRequest;

    await expect(
      registerAuth0OriginStrip(dnr, "testextid"),
    ).resolves.toBeUndefined();
    expect(dnr.updateDynamicRules).toHaveBeenCalledTimes(1);
  });
});
