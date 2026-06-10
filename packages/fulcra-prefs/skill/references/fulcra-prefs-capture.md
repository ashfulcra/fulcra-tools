# When and what to capture

Capture creates durable, user-visible records on the user's Fulcra timeline.
Be conservative: capture what the user SAID, not what you inferred silently.

CAPTURE when:
- Explicit ask: "remember that I…", "from now on…", "I always/never want…"
  → strength ±0.8–1.0, half_life 365 (or null for hard facts).
- Correction of your behavior: "no, I prefer X" → strength ±0.7, half_life 180,
  and set `supersedes` to the prior signal's id if you know it.
- Pattern you observed AND the user confirmed when asked → strength ±0.4–0.6,
  half_life 90.

DO NOT capture:
- Unconfirmed inferences, one-off task context, anything secret-like
  (credentials, health details the user didn't ask to store), or another
  person's preferences.

Conventions: keys are dot-namespaced (`dining.cuisine.thai`,
`schedule.no-meetings-before`, `comms.tone.concise`); `scope` is `global`
unless the user scoped it ("only in Claude Code" → `platform:claude-code`);
aversions are negative strength on the same key, not a `.not` key.
After capturing in a CLI session, run `fulcra-prefs compile`.
