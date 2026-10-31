# fulcra-purpleair

Put air-quality readings next to the rest of your Fulcra history. This Collect
plugin polls a PurpleAir sensor, then writes the available measurements to
separate numeric tracks. It can read one sensor through the PurpleAir cloud API
or several sensors on the local network.

This is an [unsupported monorepo experiment](../../README.md), not an air-quality
alarm. In particular, the derived AQI uses older breakpoints; see the limits
below before treating that number as a current EPA AQI.

## Set up

PurpleAir is included in the [Collect Mac installer](../../docs/collect.md#get-started-new-user).
Sign in to Fulcra, choose **PurpleAir air quality → Set up**, and choose a source:

| Source | What to supply | What Collect reads |
|---|---|---|
| `api` (default) | PurpleAir read API key and one `sensor_index` | The latest reading from `https://api.purpleair.com/v1/sensors/<index>` |
| `local` | One or more `sensor_ips`, comma- or newline-separated | Each sensor's plain HTTP `http://<ip>/json` endpoint |

For cloud mode, obtain your own read key from the
[PurpleAir developer portal](https://develop.purpleair.com/) and find the index
on your sensor's page. Provider access and usage limits apply; this package does
not manage your PurpleAir account. Local mode needs no PurpleAir key, but the
machine running Collect must be able to reach every configured sensor.

The wizard's connection test fetches a reading and previews sensor ID, time,
PM2.5, and derived AQI. It does not upload that preview. Finish setup and enable
the plugin to collect on its default ten-minute interval; **Run Now** triggers
an additional poll.

For a source install, follow the [Collect workspace instructions](../collect/README.md#running-from-source).
The Python distribution registers `purpleair` in `fulcra_collect.plugins`; it
has no standalone command. With the workspace commands on PATH, equivalent
configuration is:

```sh
fulcra-collect set-setting purpleair mode api
fulcra-collect set-credential purpleair api_key  # hidden prompt
fulcra-collect set-setting purpleair sensor_index 123456
fulcra-collect enable purpleair
fulcra-collect run purpleair
```

The index is synthetic; replace it with your own. For local mode, set `mode`
to `local` and set `sensor_ips` instead. `set-interval purpleair 600` restores
the default cadence. Running and status inspection need a running daemon.

## What lands in Fulcra

On the v1 development branch, the plugin resolves owned, compatible v1
`Metric/<UUID>` destinations with discrete aggregation and the units below.
It uses Collect's sign-in; an absent or changed sign-in stops the run.
The [migration status](../../docs/collect-v1.md) distinguishes this branch from
the released app. Each present measure becomes one record; missing values are
omitted, while a missing/incompatible destination fails the poll visibly.

The development writer checks the account's queryable replay exclusions before
same-sample duplicate shortcuts and while reconciling pending measures. It
repairs any observed revival with a native tombstone and verifies absence before
progress. Excluded measures stay excluded and do not count as delivered. These
checks use the shared local account mutation lease; native Undo UI and historical
conversion/cutover remain separate migration gates.

| Metric | Unit |
|---|---|
| PM2.5 | `ug/m3` |
| PM10 | `ug/m3` |
| Air Quality Index | `AQI` |
| Temperature (PurpleAir) | `degF` |
| Humidity (PurpleAir) | `%` |
| Barometric Pressure | `hPa` |

All configured sensors share these tracks. Their sensor IDs are encoded in
source IDs, not separate per-sensor datatypes. The plugin uses the sensor's
observation time when available. An absent cloud timestamp or absent/unparseable
LAN timestamp falls back to fetch time, which weakens repeated-sample dedup.

## Limits and storage

- **Latest readings only.** There is no historical backfill or recovery of every
  reading missed while Collect was stopped. API mode accepts one sensor index;
  LAN mode accepts several addresses. A fetch failure stops that poll before
  any readings are uploaded.
- **AQI conversion is dated.** [models.py](fulcra_purpleair/models.py) truncates
  PM2.5 to one decimal and uses the older 0–12.0 µg/m³ band for AQI 0–50,
  capping the result at 500. It does not implement the
  [EPA's 2024 AQI breakpoint changes](https://www.epa.gov/system/files/documents/2024-02/pm-naaqs-air-quality-index-fact-sheet.pdf),
  a NowCast, or a 24-hour average. It converts the single fetched sample.
- **Raw sensor values.** LAN parsing takes the channel-A `*_atm` particulate
  fields. Temperature is the onboard Fahrenheit reading; the plugin applies
  no temperature or humidity correction.
- **Verified delivery with a durable outbox.** Each measure uses a stable UUID
  derived from its v1 destination and `purpleair:<sensor>:<observation-second>:<measure>`
  primary source ID. Exact ID readback compares every submitted field before a
  receipt or observation cursor advances. Unchanged samples are skipped; a newly
  available measure or corrected value at the same sample time can still land.
  Pending samples survive a partial upload, timeout or indexing delay and are
  reconciled before the next sensor fetch. An upload receipt alone is insufficient.
  A pending outbox bound to a different account/configuration epoch blocks the
  poll for explicit reconciliation; it is never silently reassigned or discarded.
  New items retain `dispatch_version=1`, the original sample/scope binding, an
  exact per-destination issued subset/body hash and positive exact source IDs.
  The checkpoint is durable before the actual source POST. Issued absent or
  partially visible measures remain UNKNOWN without another POST; later exact
  readback can reconcile them. An exclusion cannot complete an uncertain issued
  measure from absence unless the original source was positively observed.
  Legacy items retain their original bytes and never assume a fresh attempt.
  Native repair remains a separate durability gate. Follow Collect's
  [upgrade and rollback rules](../collect/README.md) before opening this state
  with another app version.
- **Freshness is a status signal.** The plugin declares twelve-hour limits for
  silence since a yield and lag behind the newest source observation. These are
  separate from whether the worker exited successfully; they do not constitute
  an alarm or independent proof that Fulcra indexed each record.

The API key goes into Collect's OS keychain. Mode, index, and sensor addresses
are plaintext settings in `~/.config/fulcra-collect/config.toml`; cached
v1 destination mappings, the pending outbox and verified observation cursor live
in `state.db`. Legacy definition IDs/claims/cursors remain intact for historical
conversion. The private `v1-type-creations.sqlite` journal in the same Collect
home must accompany state during host migration; do not remove it to retry an
uncertain datatype creation. Sensor IDs, timestamps, and measurements are sent to your Fulcra
account, and IDs/readings can appear in local logs or connection previews.
Local mode keeps acquisition on the LAN but still uploads to Fulcra; it is
not an offline storage mode.

## Development

From the repository root after workspace setup:

```sh
uv run --package fulcra-purpleair --extra dev pytest packages/purpleair/tests/ -q
```

Tests cover synthetic cloud/LAN parsing, the implemented AQI calculation,
definition resolution, typed record construction, plugin configuration,
dedup, and failed-POST retries. HTTP transports and the Collect context are
faked. Those tests do not validate a real sensor, current API account access,
or end-to-end delivery to a Fulcra account.
