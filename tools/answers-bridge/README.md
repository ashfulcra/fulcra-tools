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

Account IDs, project URLs, and credentials are private configuration. The tracked
`answers-linear-ids.example.json` contains invented values only. Copy it to a
private location, replace the IDs with those of your Linear project, team,
workflow states, and labels, and select it explicitly. A `project_url` is optional
and only serves as a local reference.

```sh
mkdir -p "$HOME/.config/answers-bridge"
cp answers-linear-ids.example.json "$HOME/.config/answers-bridge/ids.json"
chmod 600 "$HOME/.config/answers-bridge/ids.json"
# Edit the private copy before continuing; every example ID is invented.
export ANSWERS_LINEAR_CONFIG="$HOME/.config/answers-bridge/ids.json"
export COORD_TEAM="your-team"
# Supply LINEAR_API_KEY through your local secret manager or shell environment.
python3 answers_bridge.py check-config
```

Alternatively, pass `--config /path/to/private/ids.json` **before** the command;
it overrides `ANSWERS_LINEAR_CONFIG`. There is no implicit account config path.
Credentials come from `LINEAR_API_KEY` first, or a UTF-8 file explicitly selected
with `ANSWERS_LINEAR_ENV` containing `LINEAR_API_KEY=value` (optionally quoted).
No credential file is discovered automatically. Keep any such file private and
mode `600`; the legacy `answers-linear-ids.json`, `*.local.json`, and `*.env` names
inside this tool directory are ignored by Git. Files elsewhere in the checkout
are not automatically ignored.

`COORD_TEAM` is required; the bridge never assumes a deployment's namespace.
The config's optional `sender`, `workstream`, and `promotion_source` default to
`user`, `answers`, and `Answers`. For an existing deployment, preserve its previous
values when migrating: these fields shape the promotion command, and changing
them can create a different backlog task when retrying an unfinished promotion.
Back up the old account config outside the repository before replacing it.

`--help` needs no configuration. `check-config` validates local fields and
credential presence without contacting Linear or Fulcra; it does not verify
account access or remote IDs. Missing or invalid setup exits `2` before any
service call and reports the setting to fix without echoing private file paths.
TLS uses the system trust store; Python's standard `SSL_CERT_FILE` setting can
select a deployment-specific CA bundle.

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
- Run the offline configuration and retry tests with
  `python3 -m pytest tools/answers-bridge/ -q` from the repository root. They use
  synthetic account values and stub every Linear/bus interaction.
