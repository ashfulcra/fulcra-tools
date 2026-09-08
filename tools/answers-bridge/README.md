# Answers scratchpad bridge

Record answers to an operator's questions so they can find them later, mark
them done, or promote follow-up work. Configure a dedicated Linear project
and a team store namespace for your deployment.

## Model (each direction single-purpose — no fragile two-way state sync)

- **capture** (bus → Linear, one way): a bot records an answer as a bus shard
  (`team/<team>/answers/<id>.md`, the durable record) **and** a Linear card in the
  "Answers" project. Idempotent by id.
- **check-off** (Linear only): the operator marks a card **Done**. Deliberately *not* synced
  back — there is no two-way state channel to go wrong.
- **promote** (Linear → bus, one way, operator-triggered): the operator labels a card
  `promote`; the bridge creates a bus backlog task (`coord-engine later`), comments
  the slug back on the card, adds `filed`, and moves the card to Done. Some answers
  are factual (reference + check off); some are future work (promote); some both.

## Usage

Creds (`linear.env`, secret) never live in the repo — the script reads
`$ANSWERS_LINEAR_ENV`, else the operator's session scratchpad, else `linear.env`
next to the script. Non-secret Linear IDs are in `answers-linear-ids.json`.

```sh
# a bot files an answer it just gave the operator
python3 answers_bridge.py capture \
  --q "the question the operator asked" \
  --a "the answer" \
  --by "<bot-name>" --type factual|future|both [--ts 2026-07-18]

# open cards, for terminal reference
python3 answers_bridge.py list

# file every card the operator labeled `promote` into the bus backlog (run on a cadence)
python3 answers_bridge.py promote
```

## View

Open your configured **Answers** project in Linear.

Filter to hide **Done** for the live checklist. Check off = mark Done. Turn an
answer into future work = add the `promote` label.

## Notes

- Cards live in the configured team and their own project; their titles carry no
  `[bus:…]` marker, so the (retired) coord→Linear mirror never touches them.
- Schedule `promote` at a cadence appropriate for your team, or run it manually.
