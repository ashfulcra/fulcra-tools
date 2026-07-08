# Interview doctrine

The interview plan is a **prioritized topic map, not a script**. For each
topic record: why it matters, the hypothesis to test, 2-3 candidate
questions, and which downstream decision it feeds. Priority P1 = the
engagement cannot proceed without an answer; P2 = shapes quality; P3 = nice
to know.

## Topics every engagement must cover

| Topic | Feeds | Always-P1? |
|---|---|---|
| Success criteria — what does "working" mean to the user? | plan, prototype verification | yes |
| Users & actors — who touches the product? | architecture | yes |
| **Tenancy — whose Fulcra account holds whose data?** | architecture (the biggest fork) | yes |
| Data model — entities, streams, sensitivity, retention | architecture | yes |
| Moments of value — when does the product earn its keep? | prototype plan | yes |
| Collectors — Context app, Collect daemon, Attention, CSV imports, custom | architecture | when data is passive |
| Deployment reality — whose machines, what OS, who maintains it? | prototype (rehearsal!) | yes |
| Constraints — budget, timeline, team, existing stack | plan | no |
| Assumptions harvested from the intake brief | everything | mark each validate/kill |

## Execution

- One question at a time. Ask, listen, follow the surprise — a surprising
  answer outranks the next planned question.
- Check topics off in `interview/plan.md` as they resolve; stream findings
  (verbatim where the phrasing matters) into `interview/findings.md` as they
  land, not at the end. Push after every session: `fde-engine sync <slug> push`.
- Every assumption from the intake brief exits the interview marked
  **validated**, **killed**, or **parked** (with why).
- Exit criteria: all P1 topics resolved or explicitly parked with the user's
  consent. Parked P1s become open risks named in `architecture.md`.
