# FLEET-TOPOLOGY — why your team needs one, and why it does not live here

A **fleet topology register** answers, for one team: which identity runs on
which machine, which identities are not agents at all, and who can reach what.

**This repo does not contain one, and should not.** A topology register names
hosts, addresses, sessions and credential holders. That is operational detail
about a specific deployment; it belongs in that team's file store, alongside its
tasks and its presence records — not in a general toolkit, and never in a public
repository.

This document is the *pattern*. Your register goes on your bus.

## Why the register exists at all

It was written after one coordinator mis-routed a single P0 **three times in
three hours** — to an agent on a machine that was never involved, then to an
identity that was not an agent, then correctly but to a harness that could not
execute it. None of those was carelessness. The topology was not written down
anywhere, so it was being re-derived from presence strings on every incident.

**The mechanical cause is worth stating on its own, because most fleets have
it:** the health register lists **hosts** with no agent field; the presence
register lists **agent identities** with no host field. Neither carries the
other's key, so **there is no join** — and every routing decision that needs one
becomes a guess wearing the clothes of an inference.

## The rules the register enforces

**Before asserting that A killed B, or that A can fix B, confirm A and B are on
the same host — and confirm A is an actor at all.** Neither is inferable from a
name.

**One machine can carry several identities, and usually only one of them
heartbeats.** A tool identity, a bus identity, and a presence row can all name
the same box while meaning different things — and the presence row is often the
only one with a timestamp. Resolve *which name you are reasoning about* before
the correlation, not after the dispatch.

**Some identities are emitters, not actors.** A heartbeat process appears in
presence exactly like an agent and cannot be assigned work. Assigning to one is
a dispatch that will never be refused and never be done.

**A stale health record is not a dead agent.** The health leg and the agent are
different things; only one of them is evidence of liveness. Reading a health
table as an aliveness roster is the same error as reading a config file as
evidence its reader is running (HARNESS-MAP wall 14).

## What to record

| field | why |
| --- | --- |
| identity → **what it actually is** | agent, emitter, tool, or a presence row for one of those |
| identity → **host** | closes the join that neither register carries |
| host → **what runs there** | so "restart it" has an address |
| **capability** — who can reach which host, and how | the register that would have routed the P0 in one hop |
| **known unknowns**, explicitly | see below |

## Known unknowns are load-bearing, not filler

Every question you cannot answer **from an artifact** gets written down as an
open question rather than inferred. Guessing at this layer is what produces
mis-routed P0s, and a register full of confident guesses is worse than no
register — it launders inference into fact for everyone who reads it later.

## Where it goes

Your team's store, next to the rest of the team's durable state — for the
reference layout this toolkit assumes, see
[`GET-ON-THE-BUS.md`](GET-ON-THE-BUS.md) and [`BUS-V3.md`](BUS-V3.md).

Keep it current in the same pass that discovers a change, exactly like
[`HARNESS-MAP.md`](HARNESS-MAP.md). A topology register that lags is one that
will be trusted while wrong, which is the failure it exists to prevent.
