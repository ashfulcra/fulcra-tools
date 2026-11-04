# Resolve gaps

Use during Create, Find, Join, Change or a connection check when the first source
is stale, a role is unknown, or a read fails. A gap is a research task before it
is a question for the user. Continue independent work while a real decision is
pending. Keep research within authorized sources and the user's scope.

## Current evidence

Follow the mapped canonical source, then relevant newer primary evidence through
already connected tools: recent meetings, source documents, registries or the
organization's public site. Search the entity and its aliases, inspect the best
matches, and follow their cited records. A map coverage gap does not require
stopping a search of sources the user already authorized.

Separate **event/factual date**, **source revision**, **import/sync time**, and
**your verification time**. A freshly imported old note remains old evidence.
A completed sample test is not a deployed integration; a planned meeting is not
a completed meeting. Prefer evidence that answers the actual question over the
file with the newest upload. Describe conflicting claims with provenance rather
than silently choosing one. If no later evidence is found, state the searched
scope and latest supported state; do not claim complete coverage.

Stop when evidence supports the requested answer, a material contradiction
requires a decision, or further progress needs missing access/information.
Don't trawl unrelated personal data to fill metadata. Save useful canonical
pointers and their provenance in the map. If asked to refresh knowledge content,
use its existing writer/section conventions and preserve historical material;
otherwise offer or deliver a cited answer without rewriting the source. Never
“fix freshness” by changing a timestamp alone.

## Roles that can act

Look in source registries, ownership metadata, handoff notes and relevant specs.
Distinguish the producer, technical maintainer, accountable operator and access
administrator. Record the documented role and evidence; role routing is useful
even when no human has formally accepted accountability. Do not infer a human
administrator from an account label, report author, or proposed/unconfirmed row.

Ask only when the requested action needs an unresolved decision. For example,
“Which team should take over this system?” is necessary for a reassignment;
“Confirm you own the reports?” is unnecessary when a documented maintainer can
answer the current question. If two credible authorities conflict, explain the
specific choice and its effect. Appointing a new owner needs authority;
documenting an existing role from evidence is a factual map correction.

## Access

Check the active identity and exact source account/path/workspace. Classify the
actual failure: expired authentication, confirmed permission denial, unavailable
capability, or transport/unknown. A generic reconnect hint on HTTP 403 is not
proof of expiry, especially when identity and own-source reads succeed. An
empty listing is not proof that a source has been deleted or access revoked.

- **Authentication:** restore the user's own connection through its supported
  sign-in flow. Don't substitute another profile/token to evade the failure.
- **Transport or uncertain result:** retain the last verified pointer/state,
  report what could not be verified and use a bounded retry or independent
  authorized check. Don't request broader access to solve a timeout.
- **Confirmed denial:** identify who can grant the exact source. If the user's
  request already authorizes the specific recipient and scope and the source
  supports it, inspect/reuse the existing narrow grant, make only the authorized
  change, verify its actual recipients/paths, then read as the recipient if
  available. Reconcile an uncertain write before retrying it. No repeated
  approval is needed for the same authorized grant. Otherwise ask for the
  missing recipient/scope/administrator decision, or offer a precise request to
  the appropriate agent within messaging permission.

A map share grants only the map. Provider and imported-copy permissions are
separate; a public canonical equivalent can answer a question when appropriate,
but a privileged account must not launder restricted content to a denied
requester. Use each alternate route only within that person's authorization.
Never infer consent to new grants from an index, connector install or health
check. Don't claim a grant works from its name or owner-side listing alone.

## Close the loop

Report the supported answer, what you actually repaired, what was verified and
what remains, with a named role and next action. Ask the smallest question that
would unblock that action and explain why it matters. Don't ask for an ownership
assignment, new connector or reconnection when existing evidence/routes suffice.
Persist map repairs through the steward; store check history in the reader's
status file. Keep credentials and source content out of the map.
