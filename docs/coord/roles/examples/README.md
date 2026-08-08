# Example roles

Five worked examples of agent roles for a coordination fleet, extracted from
real long-running deployments and **generalized**: no team names, no hosts, no
deployment-specific schedules (cadences shown are illustrative). Use them as starting charters — copy one to your team's store
(`team/<team>/roles/<name>/charter.md`), fill in the particulars there, and
keep the particulars there. The structural rule this repo follows: **examples
generalize; anything particular to one team rides that team's bus.**

Each example carries the same sections: mission, what it holds, operating
rules, wake pattern, and the failure modes actually observed in the role —
because a role definition that omits how the role fails is a charter for the
easy days.

| example | extracted from | the one-line mission |
| --- | --- | --- |
| [Coordinator](coordinator.md) | a persistent cloud coordinator session | route work, issue rulings, own the pins and the merges |
| [Coder](coder.md) | a gated-harness implementation agent | build to spec, verify before claiming, hand off what the harness blocks |
| [Reviewer](reviewer.md) | an independent exact-head reviewer | verdicts at exact heads; refuse what cannot be verified |
| [Maintainer](maintainer.md) | infrastructure-minding agents | keep one subsystem alive; alarm on bad state, not on change |
| [LocalAgent](localagent.md) | agents needing a local machine | do what only a local session can; report capability honestly |
