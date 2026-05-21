// chrome/src/default-categories.ts
//
// Default Tier 2 host → category mappings, seeded on install and offered
// in the wizard. The user can edit or delete any of them via the
// popup's CategoryEditor.
//
// Tier 2 (category) is the right tool for "I want to know I spent time
// on Gmail, not what individual emails I read." When a host matches a
// category mapping, the visit emits as `category: <slug>` with
// `url`/`title` null — no per-thread/per-email data leaves the
// extension.
//
// Slugs MUST be in the CATEGORY_VOCAB the Python side bootstraps tags
// for (see fulcra_attention/fulcra.py CATEGORY_VOCAB). Unknown slugs
// would get sanitised by the relay but wouldn't bind to a tag UUID.

import type { CategoryMapping } from "./types";

export const DEFAULT_CATEGORY_MAP: ReadonlyArray<CategoryMapping> = [
  // --- Webmail: collapse to "I was on webmail" rather than per-thread.
  { pattern: "mail.google.com",         category: "webmail" },
  { pattern: "*.mail.google.com",       category: "webmail" },
  { pattern: "outlook.live.com",        category: "webmail" },
  { pattern: "outlook.office.com",      category: "webmail" },
  { pattern: "*.outlook.com",           category: "webmail" },

  // --- Calendar: one event per calendar session, not per appointment view.
  { pattern: "calendar.google.com",     category: "calendar" },

  // --- Document editors: collapse to "I was writing" without leaking doc titles.
  { pattern: "docs.google.com",         category: "doc-editor" },
  { pattern: "sheets.google.com",       category: "doc-editor" },
  { pattern: "slides.google.com",       category: "doc-editor" },
  { pattern: "*.notion.so",             category: "doc-editor" },
  { pattern: "notion.so",               category: "doc-editor" },

  // --- AI chats: lots of users want time-on-chat-tools tracked without
  // leaking prompts. Conversation URLs differ per chat, so without the
  // mapping every conversation is its own event with the chat title in
  // the URL.
  { pattern: "chatgpt.com",             category: "ai-chat" },
  { pattern: "chat.openai.com",         category: "ai-chat" },
  { pattern: "claude.ai",               category: "ai-chat" },
  { pattern: "gemini.google.com",       category: "ai-chat" },

  // --- DMs: same rationale — collapse to "time spent in messaging
  // apps" without per-conversation data.
  { pattern: "web.whatsapp.com",        category: "dm" },
  { pattern: "messages.google.com",     category: "dm" },
  { pattern: "web.telegram.org",        category: "dm" },
  { pattern: "messages.discord.com",    category: "dm" },
];

/**
 * Merge the defaults into a user's existing category map. User entries
 * for the same pattern win — we don't overwrite a category mapping the
 * user has changed. Returns the merged list, sorted by pattern for
 * deterministic display.
 */
export function mergeDefaults(
  userMap: CategoryMapping[],
): CategoryMapping[] {
  const byPattern = new Map<string, CategoryMapping>();
  // Defaults go in first so user entries override.
  for (const m of DEFAULT_CATEGORY_MAP) byPattern.set(m.pattern, { ...m });
  for (const m of userMap) byPattern.set(m.pattern, m);
  return Array.from(byPattern.values())
    .sort((a, b) => a.pattern.localeCompare(b.pattern));
}
