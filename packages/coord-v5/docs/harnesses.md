# In-harness listeners

One reporting listener serves an enrolled environment/harness; addressed role/job workers remain separate and reasoning sessions disposable. Its job is to receive bus traffic, preserve coverage and route attention through locally configured native messaging, not execute the workers' tasks. Prefer bounded subagents for worker reasoning or implementation. A deliberate self-ticking executor may continue its own assigned task, but only as a separate opt-in mode. Store the native listener registration with the private workspace descriptor. A fresh listener reconstructs unfinished obligations from annotations and recovers ambiguous delivery from its receipts before resending.

Verified native event ingress is the preferred enrollment path when dispatch, recovery and scoped read/publish are also current and unattended operation was requested. A schedule can backstop that path; verified ticks or session loops are fallbacks when event ingress is absent, unsupported or unknown. The planner's `native-event` mode does not implement an event adapter or prove that a product supports event ingress. Verify actual reception and idle worker delivery in the enrolled environment before advertising them.

Generate `instruction_mode:"listener"` through the [instructions command](onboarding.md#capability-assessment)
for this reporting session; use `executor` only for separately authorized owned-task execution.
Keep these role blocks in listener/executor local/session instructions, never shared repository-wide
worker instructions. The default `worker` block is unchanged. Listener reports cover its own reads,
routes, conflicts and reconciliation, never worker claims/progress/completion or invented receiver
acknowledgments. Executor recovery needs explicit ownership and context; a wake cannot authorize
adoption. Mode selection installs nothing: fallback ticks/loops and direct executor self-ticking
each require separate authorization. Generated prose is not runtime enforcement or consumer-agent proof.

## Choose a centralized listener

The recipes below are **deployment recommendations**, not adapters or services
automatically installed by Coord. Use one registered listener per exact
principal/workspace/environment/harness scope, not one poller per worker. A single
host process may serve several scopes only with separate scope-bound journals,
source watermarks and routes. Desktop, CLI, browser and cloud are different
environments even when they use the same account or model.

| Environment | Recommended central owner | Delivery path and fallback |
|---|---|---|
| Codex desktop | Dedicated peer listener thread | Host-injected native thread messages; authorized native heartbeat backstop |
| Codex CLI / app-server | Operator-owned local controller | Documented app-server thread API; otherwise durable mailbox and explicitly invoked headless worker |
| Codex cloud / ChatGPT Work | One cloud listener job for the enrolled cloud scope | Verified cloud-native worker route; otherwise checkpointed attention mailbox |
| Claude Code terminal / SSH | One dedicated persistent listener session | Verified channels ingress; exact `dispatch-claude` route for approved existing workers; session loop fallback |
| Claude Code desktop | Interactive coordinator or scheduled read-only monitor | Interactive session messaging where permitted; scheduled runs need a separately verified external bridge or mailbox |
| Claude Code cloud | One routine for the enrolled cloud scope | Rehydrate durable context each invocation; verified cloud worker endpoint or mailbox |
| ChatGPT / Claude browser chat | One coordinator conversation or external listener | Verified connected tools while active; external notification/mailbox when unattended delivery is unavailable |
| OpenClaw | Dedicated Gateway listener agent | Permitted `sessions_send` to exact workers; Gateway heartbeat/automation backstop |
| Hermes | Dedicated Gateway listener session or one cron job | Verified Gateway destination; cron delivery is not proof of starting an existing worker session |
| Gemini CLI / Copilot CLI / Cursor CLI | One operator-owned external listener | Approved headless worker invocation; no shipped Coord native dispatcher |
| IDE agents, API agents, containers / CI | External controller with durable storage | Verified host extension/API/queue adapter; otherwise workers consume a durable mailbox |

### Common setup recipe

1. Discover the existing listener registration before creating one. Record its
   owner, exact scope, invocation mechanism and durable state location. Select the
   environment recipe below; do not reuse another harness's private journal.
2. Inspect current transport tool names/schemas and bind the existing account to
   the configured principal and sources. Perform a bounded owned-source read in
   the actual listener environment. Missing tools/accounts are unavailable, not
   empty work; do not refresh credentials or switch accounts by implication.
3. Register each source with its protocol, approved bootstrap boundary, source
   `recorded_at` watermark, overlap, deduplication and pending delivery intents.
   Keep reporting notes separate from typed work events. Bootstrap typed work
   from an authenticated bounded read containing the required causal ancestry
   and existing grants; a latest revision alone may not reconstruct the task.
   Preserve partial-history markers. Never copy a worker's SQLite file into the
   listener or claim a complete source history from a bounded bootstrap.
4. Obtain authorization for private listener/transport storage and each exact
   job-to-worker route. Keep a separate transport journal for each source scope;
   the environment listener delivery journal is separate again. Add routes through
   the owning registration's supported process, preserving all existing routes,
   watermarks, deduplication and receipts. `listener configure` consumes a route
   array; it is not an append operation. Never supply only the new worker and
   erase the existing route set. If the owner cannot safely preserve it, stop.
5. Read, build the authorized `observation`, prepare attention and journal intent
   before supported delivery. Do not hand raw reporting notes to the work bridge.
   Busy, missing or unsupported targets retain obligations; an ambiguous send
   requires reconciliation, not a duplicate invocation or new worker identity.
6. Verify a natural addressed event reaches the correct worker. Record source
   identity/time, prepared wake/attempt, native acceptance and actual receiver
   read separately. Check a real scheduled/event invocation while the worker is
   idle before replacing its fallback. Keep restart/outage tests separate and
   separately authorized. No setup recipe below grants new permissions or spend.

Recommended listener prompt, generated with the installed listener role block:

> Resume this registered scope's source state and delivery journal. Read each
> authorized source once, retaining coverage and durable obligations. Route only
> mapped attention through approved native actions. Preserve failed reads and
> uncertain deliveries; do not execute worker tasks. Record receiver evidence
> separately and stay quiet when state is unchanged. Report exact missing tools,
> permissions or routes rather than repairing credentials or starting new workers.

## Codex desktop

Use locally verified native thread messaging from an active listener turn. Assess event ingress separately; use native thread heartbeats/scheduled tasks as a verified fallback or backstop, not a mandatory event-path component. Do not configure an external command as though it could call an app-only tool. Local scheduled runs require the app and machine to be available; verify permissions in the actual scheduled turn. See [Codex scheduled tasks](https://learn.chatgpt.com/docs/automations).

Verify the exact native cross-session route locally, including human authorization and target restrictions. An observed Codex ancestor-target restriction is evidence about that route, not a universal prohibition on messaging peer sessions. Keep route identifiers and target mappings private; availability in an active turn does not prove an idle receiver will wake.

The enrollment plan's `listener_scope` uses the dispatcher's `codex` harness key. Keep one private listener SQLite journal for that scope and configure exact job-to-thread routes with `coord-v5 listener configure`. `active` routes target an existing thread; `dormant` routes request a successor; `retired` routes notify their configured coordinator. Successor creation is a harness action, not an implied role grant.

Use this bounded native listener job recipe, substituting only local approved configuration paths and identity:

1. On actual event reception or a fallback tick, read the owned annotation source with `transport read`; obtain both fresh events and durable obligations with `observation`. Preserve partial/unavailable coverage and the durable cursor/dedup state; do not treat a failed read as empty or advance past an unprocessed gap.
2. Pass that observation to `listener prepare` using the registered scope, journal and holder. Inspect its known attention items and prepared actions.
3. Execute only the prepared supported native message action for a locally configured route. Preserve the journal's delivery intent before invoking native messaging. Save `accepted`, `error`, or `unknown` via `listener settle` using the exact wake, attempt and target. Keep that native receipt separate from receiver acknowledgment and task progress/completion. A receiver acknowledges with `listener ack` after matching its target and wake IDs.
4. Report `needs_successor`, `needs_coordinator`, conflicts, failed reads and uncertain delivery to the configured coordinator. For a successor, retrieve role/job checkpoint plus the fresh event tail and verify artifact access before transferring responsibility. Reconcile an uncertain send rather than creating a new identity to retry it.
5. For an event path, keep the verified event registration active. If a native heartbeat is the fallback or backstop, use the returned interval for that heartbeat only. Preserve a faster operator interval. Update through the native scheduling tool while preserving the full prompt, target and notification preferences. Failed or incomplete reads are not quiet time; scheduled retries retain the short error interval. Never cancel the environment listener merely because a workstream finished.
6. Stay quiet for unchanged non-actionable state; notify on meaningful work, failure, completion or user decision. Reasoning sessions publish their own contact/progress/checkpoints; the listener reports only its own successful reads.

The journal's recurrence is a suggestion until the native schedule update succeeds. Record both values. Live idle-wake proof does not establish restart, host-outage, permission-prompt or upgrade behavior; keep those capability fields unknown until tested. Do not restart the user's app or host as part of ordinary enrollment.

## Other environments

### Codex CLI and operator-owned app-server

Put one controller beside the CLI installation, with durable listener state outside
disposable worker checkouts. An approved service supervisor can invoke its bounded
read/prepare cycle; choose event ingress when verified, otherwise one scheduled
controller job. Do not run the poll in every CLI session.

For a custom controller, use the documented [Codex app-server](https://learn.chatgpt.com/docs/app-server)
thread lifecycle and turn API, retaining exact thread IDs and observing completion.
This is a host integration to implement and verify, not a shipped Coord dispatcher
or permission to control the desktop application's internal server. If no such
integration is available, enqueue attention in a durable mailbox and explicitly
invoke an authorized worker using [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).
Never assume a headless invocation wakes the user's open terminal or IDE session.

### Codex cloud and ChatGPT Work

Use one listener job in the actual cloud environment, not a local desktop thread
or local filesystem journal. Place recovery state in approved durable storage and
rehydrate the exact scope and source mappings on each invocation. Verify connected
source tools and any cloud-native worker dispatch in that job itself.

Where no supported cross-task route is available, publish addressed attention to a
durable mailbox and let workers consume it at their next authorized invocation.
An external controller may invoke a documented worker API only with separate
authorization. Desktop thread messaging, local CLI resume and availability of a
scheduled task are not evidence of cloud routing. Use the actual scheduling surface
and its limits; [Codex scheduled tasks](https://learn.chatgpt.com/docs/automations)
are a starting point, not a claim of universal cloud connector support.

### Claude Code: explicit local delivery

Recommended central owner: one dedicated listener session in a persistent terminal
or approved background service on the local/SSH host. Use verified
[Claude channels](https://code.claude.com/docs/en/channels) for event ingress;
channels require a running session and eligible authentication. The package does
not supply a Fulcra channel plugin. Without a verified channel adapter, explicitly
schedule the listener's bounded read through
[/loop or session scheduling](https://code.claude.com/docs/en/scheduled-tasks).
Session scheduling is not an independently always-on service; record lifetime,
expiry and resume limitations. Workers do not each need their own poll loop.

The package supports opt-in `coord-v5 listener dispatch-claude`, using the
canonical enrollment harness `claude-code`. Configure a private
`target:{kind:"claude-code",sessionId:UUID,cwd:ABS}` route and pass the prepared
action's `.arguments` on stdin with trusted `--executable ABS`. Existing Codex
thread routes and native heartbeat steps are unchanged. See the [dispatcher
contract](../README.md#explicit-claude-cli-delivery) for fixed flags requesting
the full model ID `claude-haiku-4-5-20251001` and a $0.25 provider budget,
tool-free execution, 60-second/64-KiB bounds, exact result correlation and safe
failure codes. Exact argv is enforced; served
model and provider billing are not. Inspect actual assistant-model telemetry:
the served model may differ from the requested model ID, and auxiliary `modelUsage`
does not identify the conversational model. Sub-budget success does not prove
a hard spend ceiling.

Preparing is not sending. An accepted native result is not receiver acknowledgment,
source completeness, a schedule, or lifecycle acceptance. The local journal claims
before launch; repeated and ambiguous attempts require reconciliation, including
crash-left claims. Do not resume the same session elsewhere concurrently: the
fence is local to one journal/scope, not global busy detection. The operator must
verify the CLI/version, existing session, trusted CWD and credential availability
without copying them into public artifacts. No sign-in, new-session creation or
force takeover is performed. Fixture and packed-install success are local tests,
not authenticated native acceptance or restart/outage evidence.

### Claude desktop

In Claude Code on desktop, use a dedicated interactive coordinator session when
its permitted session-messaging tools can reach the enrolled workers. Current
[desktop documentation](https://code.claude.com/docs/en/desktop) excludes sending
through that surface from unwatched scheduled-task sessions or into them. Do not
describe a scheduled desktop monitor as a verified cross-session listener.

For unattended reads, configure one authorized desktop scheduled monitor that
rehydrates its journal and records attention. Delivery needs a separately verified
external controller/CLI bridge, or a mailbox read by the interactive coordinator.
For ordinary Claude desktop chat, begin with that mailbox pattern unless the actual
installed client exposes and verifies an equivalent native route. Sharing a product
name with Claude Code does not establish CLI access, messaging or scheduling.

### Claude Code cloud

Use one [routine](https://code.claude.com/docs/en/routines) for the cloud scope,
with its configured connectors, durable recovery state and bounded source-read
prompt. Treat each invocation as needing recovery context; do not assume it resumes
a laptop session or can reach private files from a fresh cloud checkout.

Connect a verified cloud worker endpoint if authorized. Otherwise record addressed
attention in the mailbox for the worker's next invocation. If the routine's available
interval is slower than required, ask for an approved external event controller;
do not silently create per-worker routines or claim a faster cadence than exists.
This package does not ship a Claude cloud dispatcher.

### ChatGPT and Claude browser conversations

Choose one coordinator conversation with the verified source connector. During an
active turn, read the shared mailbox and route using only messaging actually exposed
and authorized in that product. A browser tab is not a persistent daemon.

For unattended operation, use an authorized external central listener to maintain
source state and issue a supported notification or mailbox update. If a product
offers scheduled jobs, verify the source tools and recipient route in a real job
before selecting it as the listener; ordinary chat tools or custom-agent availability
do not prove scheduled connector access. Without that evidence, workers resume
interactively from the mailbox. Do not emulate wake delivery with UI automation or
assume an API conversation is the same as the user's browser conversation.

### OpenClaw Gateway

Create or reuse one dedicated listener agent on the existing Gateway. Inspect its
effective tool policy, bind exact authorized worker session keys, and route through
[session tools](https://docs.openclaw.ai/concepts/session-tool), such as permitted
`sessions_send`. Do not widen session visibility or bypass denied agent-to-agent
access as a setup step.

Use verified Gateway event ingress first; otherwise configure that listener agent's
[heartbeat](https://docs.openclaw.ai/gateway/heartbeat) or one approved automation.
Keep the Gateway and listener recovery state available independently of workers.
Reconstruct source obligations before sending after restart. Gateway acceptance or
external-channel delivery is not proof of worker consumption. Coord provides no
OpenClaw-specific dispatcher; this is an operator-integrated host route.

### Hermes Gateway, desktop and terminal

For a continuously available Gateway, designate one listener session. Its
[session heartbeat](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat)
can invoke the same idle conversation. For a self-contained durable poll, use one
[cron job](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
instead, and rehydrate source state because cron uses a fresh agent session.
Do not confuse a terminal-only heartbeat with an always-running Gateway.

Bind the exact permitted Gateway conversation/notification destination. Cron's
normal final-response delivery is a notification, not automatically a turn in an
existing worker session; avoid a second send to the same configured destination.
If native worker-session dispatch is not exposed and verified, use the mailbox
fallback. The package ships no Hermes-specific dispatcher or scheduler installer.

### Gemini CLI, GitHub Copilot CLI and Cursor CLI

Use one external controller for the enrolled host, keeping source/delivery state
outside worker processes. After journaled attention, invoke an explicitly authorized
headless worker through the product's documented interface:
[Gemini headless mode](https://geminicli.com/docs/cli/headless/),
[Copilot CLI automation](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli),
or [Cursor headless CLI](https://cursor.com/docs/cli/headless).

These are candidate worker entry points, not shipped Coord dispatchers. Verify the
installed version, exact identity/context, completion correlation, tool permissions,
busy behavior and bounded provider spend before unattended use. Do not enable
unrestricted tool approval to make the recipe run. Without an approved adapter,
leave attention in the mailbox; launching a headless worker does not prove it
wakes an existing interactive CLI or editor session.

### IDE agents, custom API agents and containers/CI

For VS Code, JetBrains and other IDE agents, put the central source listener in an
approved external controller or host extension, not each editor tab. Deliver through
a documented extension API or enrolled backend worker route only when verified;
otherwise show/read the durable mailbox on the next interactive turn. A vendor's
CLI being available does not establish an IDE-session messaging API.

For custom API agents, run one controller that reads sources, journals attention
and enqueues exact authorized worker jobs. Retain operation IDs, worker-context
checkpoints and delivery evidence independently of model calls. This architecture
is a recommendation; application-specific worker invocation must be implemented.

For containers, remote hosts and CI, keep journals in approved persistent storage,
use one owner-controlled event consumer or scheduled job, and prevent overlapping
controllers for the same scope. Ephemeral jobs must recover before acting and must
not rely on a previous runner's local disk. Use the provider's existing secret store;
never put bearer tokens in workflow files or public artifacts. If a durable mailbox
or supported recipient adapter is absent, mark enrollment interactive/unavailable
rather than launching a nominal daemon that cannot safely route.

## Acceptance record

Capture harness/version, listener registration, actual event reception/execution time, scheduled time when applicable, source coverage, native delivery intent/receipt, receiver acknowledgment and observed model usage when available. Test idle, busy, restart, outage, credentials, permission prompts and version drift separately. Unknown usage remains unknown. A 24-hour cost/latency experiment is a separate measurement, not an extrapolation from one successful wake.
