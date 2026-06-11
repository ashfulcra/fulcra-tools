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

## Auto-capture (passive, end-of-session)

You don't need an explicit "remember this" to capture — you SHOULD passively
notice preferences as a session unfolds and record them, subject to the same
conservative rules above. The safe pattern:

1. **Collect, don't interrupt.** As the session runs, keep a short list of
   candidate signals (the same shape as a capture: key, value, strength, and a
   `confidence`). Don't pepper the user with confirmations mid-task.
2. **Set confidence honestly.** Explicitly stated → `confidence` 0.9–1.0.
   Inferred-but-unconfirmed → 0.4–0.6. This is load-bearing: compile weights
   conflict resolution by confidence, so a low-confidence guess will **not**
   override a high-confidence explicit preference. That safety net is exactly
   what lets you capture inferences without poisoning the store.
3. **Record once, at the end, in a batch.** Write the candidates to a JSON
   array and submit a single consented call:
   `fulcra-prefs capture-batch --file <path> --platform <your-platform>`
   (each item may set its own `kind`/`scope`/`confidence`/`half_life_days`/
   `supersedes`). One call, one disclosure to the user, no mid-task spam.
4. **Still never auto-capture** unconfirmed *sensitive* data (credentials,
   health/financial details the user didn't ask to store) or another person's
   preferences — confidence weighting doesn't make those acceptable.

Tier-2 (HTTP) agents do the same but POST each signal to `/ingest/v1/record`
(see the tier2-http reference); compile picks them up regardless.
