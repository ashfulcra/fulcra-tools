// chrome/tests/title-scrub.test.ts
import { describe, test, expect } from "vitest";
import { scrubTitle } from "../src/title-scrub";

describe("scrubTitle", () => {
  test("passes through titles for unknown hosts", () => {
    expect(scrubTitle("example.com", "Some Article — Example")).toBe("Some Article — Example");
  });

  test("returns null/null inputs unchanged", () => {
    expect(scrubTitle(null, "anything")).toBe("anything");
    expect(scrubTitle("example.com", null)).toBeNull();
    expect(scrubTitle("example.com", "")).toBe("");
  });

  test("Gmail inbox titles collapse to a generic", () => {
    expect(scrubTitle("mail.google.com", "Inbox (12) - ash@fulcra.com - Gmail"))
      .toBe("Inbox — Gmail");
  });

  test("Gmail thread titles drop the subject", () => {
    expect(scrubTitle("mail.google.com", "Re: confidential proposal - ash@fulcra.com - Gmail"))
      .toBe("Email thread — Gmail");
    // The subject must NOT appear in the redacted output.
    expect(
      scrubTitle("mail.google.com", "Re: confidential proposal - ash@fulcra.com - Gmail"),
    ).not.toContain("confidential");
  });

  test("Slack: workspace + channel name dropped entirely", () => {
    expect(scrubTitle("acme.slack.com", "#engineering | Acme")).toBe("Slack");
    expect(scrubTitle("acme.slack.com", "alice (DM) | Acme")).toBe("Slack");
  });

  test("Notion page titles redacted", () => {
    expect(scrubTitle("www.notion.so", "Confidential Roadmap — Fulcra"))
      .toBe("Notion page");
  });

  test("Calendar event titles redacted, generic shell pass-through", () => {
    expect(scrubTitle("calendar.google.com", "Interview with Jane — Google Calendar"))
      .toBe("Google Calendar");
    // Bare "Google Calendar" with no separator stays.
    expect(scrubTitle("calendar.google.com", "Google Calendar"))
      .toBe("Google Calendar");
  });

  test("LinkedIn DMs redacted, public feed stays", () => {
    expect(scrubTitle("www.linkedin.com", "Messaging with Jane Doe"))
      .toBe("LinkedIn");
    expect(scrubTitle("www.linkedin.com", "Feed | LinkedIn"))
      .toBe("Feed | LinkedIn");
  });

  test("Wildcard pattern matches both base and subdomains", () => {
    // *.slack.com matches BOTH acme.slack.com AND slack.com
    expect(scrubTitle("slack.com", "anything | something")).toBe("Slack");
    expect(scrubTitle("acme.slack.com", "anything | something")).toBe("Slack");
    // Non-matching domain falls through.
    expect(scrubTitle("not-slack.com", "anything | something"))
      .toBe("anything | something");
  });
});
