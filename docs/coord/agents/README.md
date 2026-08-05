# Per-agent harness docs

Self-service under the operator rule (2026-07-28): **an agent may update the
doc describing ITS OWN harness here directly, with coord-boss review as the
only gate.** Shared contracts (BUS-V3, AGENTS.md doctrine, engine code) keep
the normal review flow.

One file per canonical agent id: identity/lane, runtime, wake source(s),
durability constraints, read discipline, and known limits (what to route
around). Written BY the agent — these are self-descriptions dispatchers rely
on, so keep them current when your harness changes. No secrets; adapter-arg
identifiers (thread ids, session refs, trigger ids) are allowed.
