# Ash Answers scratchpad bridge

A place for bots to write **answers to Ash's mid-workstream questions** so he can
reference them later and check them off when they're no longer useful — viewed in
a dedicated Linear project ("Ash · Answers"), backed by durable shards on the
coord bus. Separate from the coord issue mirror, so no issue noise.

## Model (each direction single-purpose — no fragile two-way state sync)

- **capture** (bus → Linear, one way): a bot records an answer as a bus shard
  (`team/fulcra/answers/<id>.md`, the durable record) **and** a Linear card in the
  "Ash · Answers" project. Idempotent by id.
- **check-off** (Linear only): Ash marks a card **Done**. Deliberately *not* synced
  back — there is no two-way state channel to go wrong.
- **promote** (Linear → bus, one way, Ash-triggered): Ash labels a card
  `promote`; the bridge creates a bus backlog task (`coord-engine later`), comments
  the slug back on the card, adds `filed`, and moves the card to Done. Some answers
  are factual (reference + check off); some are future work (promote); some both.

## Usage

Creds (`linear.env`, secret) never live in the repo — the script reads
`$ANSWERS_LINEAR_ENV`, else the operator's session scratchpad, else `linear.env`
next to the script. Non-secret Linear IDs are in `answers-linear-ids.json`.

```sh
# a bot files an answer it just gave Ash
python3 answers_bridge.py capture \
  --q "the question Ash asked" \
  --a "the answer" \
  --by "<bot-name>" --type factual|future|both [--ts 2026-07-18]

# open cards, for terminal reference
python3 answers_bridge.py list

# file every card Ash labeled `promote` into the bus backlog (run on a cadence)
python3 answers_bridge.py promote
```

## View

Linear project **Ash · Answers**:
https://linear.app/ash-agent-coordination/project/ash-answers-f6061dbe0349

Filter to hide **Done** for the live checklist. Check off = mark Done. Turn an
answer into future work = add the `promote` label.

## Notes

- Cards live in team BUS but in their own project; their titles carry no
  `[bus:…]` marker, so the (retired) coord→Linear mirror never touches them.
- `promote` runs from the coord-boss hourly watchdog, so labels are filed within
  the hour without a manual trigger.
