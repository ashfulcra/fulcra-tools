// chrome/src/title-scrub.ts
//
// Tier-1.5 scrubbing for document.title. The page URL goes through
// scrub.ts; titles get this. Some sites bake personal data right into
// the page title — Gmail's title is the email subject; Calendar's title
// is the event name; Slack's title is "<channel> - <workspace>" with
// the user's workspace exposed. These titles flow into the Fulcra
// `title` field, and a user inspecting their attention log would see
// "Re: confidential proposal — Gmail" sitting in their data.
//
// This module collapses known-leaky titles to a generic form,
// per-host. It's intentionally conservative: only the small handful of
// sites where the title-is-content pattern is reliable. Everything
// else passes through.

interface TitleRule {
  /** Host or wildcard pattern (same semantics as ignore/categorize). */
  pattern: string;
  /** Function that takes the raw title and returns a redacted version,
   *  or null to drop the title entirely. */
  redact: (raw: string) => string | null;
}

const RULES: TitleRule[] = [
  {
    pattern: "mail.google.com",
    // Inbox titles: "Inbox (N) - user@example.com - Gmail"
    // Thread titles: "Re: Subject - user@example.com - Gmail"
    redact: (t) => {
      if (/^Inbox\b/.test(t)) return "Inbox — Gmail";
      if (/\bGmail\b/.test(t)) return "Email thread — Gmail";
      return "Gmail";
    },
  },
  {
    pattern: "calendar.google.com",
    // Event-detail titles include the event name. Inbox-level titles
    // are usually "Google Calendar" or "Today, <date>".
    redact: (t) => {
      if (/Google Calendar/.test(t) && !/[-—]/.test(t)) return t;
      return "Google Calendar";
    },
  },
  {
    pattern: "*.slack.com",
    // "<channel> | <workspace>" or "<DM with X> | <workspace>" — the
    // channel/DM is sensitive; the workspace is often not, but conservatively
    // drop both.
    redact: (_t) => "Slack",
  },
  {
    pattern: "*.notion.so",
    // Notion pages: "<page title> — <workspace>". Page title is the
    // PII; replace.
    redact: (_t) => "Notion page",
  },
  {
    pattern: "discord.com",
    // "<channel> | <server>" structure.
    redact: (_t) => "Discord",
  },
  {
    pattern: "*.linkedin.com",
    // LinkedIn DM/profile titles include the other person's name.
    redact: (t) => {
      if (/Feed \| LinkedIn$/.test(t)) return t;
      return "LinkedIn";
    },
  },
];

function matchesHost(host: string, pattern: string): boolean {
  if (pattern.startsWith("*.")) {
    const tail = pattern.slice(2);
    return host === tail || host.endsWith("." + tail);
  }
  return host === pattern;
}

/**
 * Apply the title-scrub ruleset. Returns the cleaned title (which may
 * be the original input unchanged) or null if the rule dropped the
 * title entirely.
 *
 * Pure function. Tested independently of the rest of the pipeline.
 */
export function scrubTitle(host: string | null, rawTitle: string | null): string | null {
  if (!rawTitle || !host) return rawTitle;
  for (const rule of RULES) {
    if (matchesHost(host, rule.pattern)) {
      return rule.redact(rawTitle);
    }
  }
  return rawTitle;
}
