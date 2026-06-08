# Fulcra Continuity Reverse-Engineered Product Spec - Codex

## 1. One-line product definition

`fulcra-continuity` is a small stdlib-only checkpoint and resume-brief CLI/API for packaging enough structured state that another agent or later session can resume long-running work without the original transcript.

## 2. Target user + core jobs-to-be-done

The primary user is an agent or human operator preparing for compaction, teardown, idle stop, overnight pause, or cross-agent handoff. Their job is to write a portable, self-describing checkpoint containing objective, decisions, artifacts, open questions, next actions, memory writes, and optional coordination identity.

The receiving user is another agent session, possibly from a different runtime. Their job is to render or read a checkpoint, inspect the listed artifacts, and continue without guessing at invisible transcript state.

The runtime/instruction author is a secondary user. Their job is to teach Claude Code, Codex, OpenClaw/Arc, Hermes, or another environment when to write checkpoints and how to hand them off without assuming GitHub, a shared filesystem, or a shared transcript.

## 3. Capability surface - grouped list of what it does, from the CLI/API

Checkpoint data model:

- `SCHEMA_VERSION = "fulcra.continuity.checkpoint.v1"` locks the top-level checkpoint shape.
- Dataclasses model `Artifact`, `MemoryWrite`, `WorkstreamIdentity`, and `ContinuityCheckpoint`.
- Checkpoints carry `checkpoint_id`, `task_id`, `title`, `objective`, `created_at`, `owner_agent`, optional `identity`, `source`, `transcript_path`, optional `context_used_percent`, `decisions`, `artifacts`, `open_questions`, `next_actions`, `memory_writes`, and `tags`.
- Empty identity is omitted from serialized JSON to keep continuity-only checkpoints lighter.

Checkpoint creation and parsing:

- `make_checkpoint` builds collision-resistant checkpoint IDs from timestamp, task/title slug, and random suffix.
- `checkpoint_from_dict` accepts JSON dictionaries and tolerates malformed optional fields by coercing lists, artifacts, memory writes, identity, and context percent.
- `parse_artifact` parses `PATH` or `PATH=NOTE`.
- `parse_memory_write` parses `CLAIM` or `CLAIM|SCOPE|TTL|SUPERSEDES`.

Resume rendering:

- `render_resume_brief` converts checkpoint JSON into a human-readable brief with objective, checkpoint metadata, identity, decisions, artifacts, open questions, next actions, and memory writes.

CLI:

- `fulcra-continuity checkpoint` writes checkpoint JSON and optionally a resume brief, accepting task/title/objective, owner identity, coord identity fields, source, transcript path, context percent, repeated decisions/artifacts/open questions/next actions/memory/tags, and output path.
- `fulcra-continuity resume <checkpoint>` prints or writes a rendered resume brief.
- `fulcra-continuity demo --out-dir` writes a sample "Context Cliff Rescue" checkpoint JSON plus resume markdown.
- `--version` reports the package version.

Documentation and instructions:

- `README.md` explains install, checkpoint creation, coord pairing, durable pause points, agent handoff, resume, and demo.
- `AGENTS.md` gives package rules for when checkpoints are appropriate and what minimum fields make them useful.
- `docs/agent-handoff.md` specifies cross-runtime handoff behavior, portable artifact requirements, coord-backed versus continuity-only work, and bootstrap instructions for agents that do not know Continuity.

Interop with `fulcra-coord`:

- The standalone package does not depend on coord.
- The README/docs define shared identity fields for coord pairing.
- `fulcra-coord` contains a separate stdlib bridge that writes the same schema by convention, and a coord-side test asserts schema version and top-level key parity when the standalone package is importable.

## 4. Scope classification - tag every capability CORE / SUPPORTING / PERIPHERAL-or-creep with a one-line rationale

CORE:

- Checkpoint schema and dataclasses - the product is the portable resume packet, so the structured shape is the product.
- `make_checkpoint` and JSON serialization - creating checkpoint files is the central user action.
- `checkpoint_from_dict` - receiving agents need robust checkpoint loading, including imperfect optional fields.
- `render_resume_brief` and `resume` - the checkpoint must be usable by humans/agents without manually decoding JSON.
- Identity fields (`workstream_id`, `agent_id`, `coord_task_id`, `coord_owner_agent`) - optional, but essential for cross-session and coord-backed resume.
- Decisions, artifacts, open questions, next actions, and memory writes - these are the core semantic payload that makes a checkpoint more than a log entry.

SUPPORTING:

- CLI argument parsing for repeated fields - makes the core schema usable from scripts and agent hooks.
- `parse_artifact` and `parse_memory_write` compact syntaxes - ergonomic helpers for CLI use.
- `demo` command and `default_demo_checkpoint` - useful proof/demo fixture; not needed for production checkpointing.
- `transcript_path`, `context_used_percent`, `source`, and `tags` - supporting metadata that improves diagnostics and provenance.
- README/AGENTS/handoff docs - currently important because the package is small and protocol-by-instruction; docs carry much of the behavioral contract.
- Coord pairing model - supporting because continuity intentionally remains independent, but real usage likely depends on coord-backed task identity.

PERIPHERAL-or-creep:

- Runtime-specific sections for Claude Code, Codex, OpenClaw/Arc, Hermes in `docs/agent-handoff.md` - useful operational guidance, but it risks turning a small schema/CLI package into an adapter-policy manual.
- Cross-agent handoff policy details - necessary today because no richer discovery layer exists, but much of it is instruction/policy rather than code.
- Demo narrative ("Context Cliff Rescue") - good for onboarding, but not product functionality.
- Memory write semantics - currently just structured strings; if it grows into a real memory store contract, it may belong in a separate memory product or coord integration.

## 5. Maturity / proven-ness read - code-level evidence of real use vs speculative surface

`fulcra-continuity` is v0.1.0 and looks intentionally thin. The implementation is one checkpoint module and one CLI module, plus package metadata and docs. Its test suite is small but relevant: 14 standalone continuity tests cover checkpoint serialization, identity omission, resume brief rendering, ASCII/collision-resistant IDs, context percent coercion, malformed optional fields, memory parsing, CLI checkpoint/resume/demo paths, and clean error reporting for missing, bad, and non-object JSON.

The most meaningful evidence of external pressure is not broad standalone use; it is interop with `fulcra-coord`. The coord package has a bridge that writes the same schema without importing `fulcra-continuity`, and a test locks schema version plus top-level key parity. That suggests Continuity is already shaping coord behavior, but also that the standalone package is not yet the only source of truth.

The package is real enough as a checkpoint format and renderer. It is not yet proven as an autonomous product with storage, discovery, synchronization, or cross-agent lookup. The docs repeatedly state that cross-agent handoff requires portable artifacts or producer-provided checkpoint path/JSON; automatic latest lookup is same-agent in coord. That is honest, but it means Continuity is currently a structured artifact format plus instructions, not a full continuity service.

The strongest code-level maturity choice is simplicity: stdlib-only, explicit dataclasses, no repo/forge dependency, no direct coord dependency, and robust parsing of imperfect JSON. The weakest maturity signal is that a lot of behavior lives in docs rather than executable workflow. For example, "do not checkpoint every message" and "include portable artifacts" are crucial, but they are not enforced by code.

The package should be considered a thin but useful artifact, not yet a broad product. It becomes operationally valuable when paired with coord hooks or disciplined agents; by itself it only creates files and renders briefs.

## 6. Open questions about product identity

- Is `fulcra-continuity` meant to remain a tiny schema/CLI package, or grow into a discovery/storage service for checkpoints?
- Should the standalone package own the schema authoritatively, with coord importing or depending on it, or is by-convention duplication intentional forever?
- What is the minimal checkpoint that should be accepted as production-useful, and should the CLI warn when decisions/artifacts/next actions are empty?
- Should cross-agent lookup be a first-class CLI capability, or should producers always pass checkpoint paths/JSON explicitly?
- Are memory writes just notes, or should Continuity integrate with a durable memory system?
- Should runtime-specific handoff instructions live in this package, in coord adapters, or in each runtime's own skill/instruction set?
- What storage target should be canonical outside coord: local files only, Fulcra Files paths, repository artifacts, or all of the above?
- How will schema evolution work after v0.1.0 if bridge code in coord deliberately avoids importing the standalone package?
