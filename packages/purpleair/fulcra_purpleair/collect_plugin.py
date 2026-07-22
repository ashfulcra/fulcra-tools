"""fulcra-collect plugin: PurpleAir air-quality readings.

A ``scheduled`` / ``live_polled`` plugin (every 10 min by default — the
PurpleAir cloud API refreshes a sensor about every 2 min but rate-limits
keys, so 10 min is the safe cadence). Each poll fetches one reading per
configured sensor — from the PurpleAir cloud API (``mode=api``, needs an
API key + sensor index) or directly from a sensor on the LAN
(``mode=local``, needs the sensor's IP) — and writes each present measure
(PM2.5, PM10, EPA AQI, temperature, humidity, pressure) to its own Fulcra
NumericAnnotation track.

Idempotency is per-reading: a sensor's own observation timestamp keys the
daemon-backed dedup claim, so re-fetching the same sample (the cloud API
returns the last cached reading between refreshes) never double-writes.
"""
from __future__ import annotations

from datetime import timedelta

from fulcra_collect.plugin import (
    Credential,
    HealthResult,
    Plugin,
    RunContext,
    Setting,
    SetupStep,
)
from fulcra_common.client import BaseFulcraClient

from .client import fetch_api, fetch_local
from .definitions import METRICS, NUMERIC_EXPECTED_SPEC
from .ingest import build_records, post_records
from .models import Reading

PLUGIN_ID = "purpleair"
DEFAULT_INTERVAL = timedelta(minutes=10)

MODE_API = "api"
MODE_LOCAL = "local"

# Plugin-scoped KV keys.
_DEFS_KV_KEY = "definition_ids"          # {metric.key: fulcra definition id}
_LAST_OBSERVED_KV_KEY = "last_observed"  # ISO ts of newest sample written


def _split(raw: object) -> list[str]:
    """Split a comma-/newline-separated setting into a clean list."""
    if not raw:
        return []
    text = str(raw).replace(",", "\n")
    return [part.strip() for part in text.split("\n") if part.strip()]


def _mode(ctx: RunContext) -> str:
    mode = (ctx.config.get("mode") or MODE_API).strip().lower()
    if mode not in (MODE_API, MODE_LOCAL):
        raise RuntimeError(
            f"purpleair: unknown mode {mode!r} (expected {MODE_API!r} or {MODE_LOCAL!r})"
        )
    return mode


def _fetch_readings(ctx: RunContext, mode: str) -> list[Reading]:
    """Fetch one reading per configured sensor for the selected mode."""
    if mode == MODE_LOCAL:
        ips = _split(ctx.config.get("sensor_ips"))
        if not ips:
            raise RuntimeError("purpleair: local mode needs 'sensor_ips' set")
        return [fetch_local(ip) for ip in ips]
    sensor_index = ctx.config.get("sensor_index")
    if not sensor_index:
        raise RuntimeError("purpleair: api mode needs 'sensor_index' set")
    api_key = ctx.credentials.get("api_key")
    if not api_key:
        raise RuntimeError("purpleair: api mode needs the 'api_key' credential set")
    return [fetch_api(str(sensor_index), api_key)]


def _resolve_definition_ids(ctx: RunContext) -> dict[str, str]:
    """Resolve (find-or-create) the six per-measure NumericAnnotation defs,
    caching each id in plugin KV keyed by measure.

    ``resolved_definition_id`` caches a SINGLE id in ``state.definition_id``
    and returns it for any name, so it cannot be called six times as-is.
    Drive it per-measure by presetting the slot to this measure's cached id
    (or ``None`` for a fresh resolve) — the same mechanism ``ensure_definition``
    uses internally. It then validates-or-recreates that id against the live
    account (self-healing an account switch) and we read the result back into
    our own per-measure cache.
    """
    cache = dict(ctx.kv_get(_DEFS_KV_KEY, {}) or {})
    for metric in METRICS:
        ctx.state.definition_id = cache.get(metric.key)
        cache[metric.key] = ctx.resolved_definition_id(
            NUMERIC_EXPECTED_SPEC,
            canonical_name=metric.canonical_name,
            create_extra=metric.create_extra(),
        )
    ctx.kv_set(_DEFS_KV_KEY, cache)
    return cache


def _write_reading(
    ctx: RunContext,
    reading: Reading,
    definition_ids: dict[str, str],
    client: BaseFulcraClient,
) -> int:
    """Build, claim, and ingest one reading. Returns the record count written
    (0 when the reading carried no values or was already ingested).

    Records are built BEFORE the claim so an all-empty sample never burns the
    per-reading dedup key (which would suppress a later non-empty sample that
    happened to share the timestamp)."""
    records = build_records(reading, definition_ids)
    if not records:
        return 0
    key = reading.dedup_key()
    if not ctx.claim_dedup_keys({key}):
        ctx.log.info("purpleair: reading %s already ingested — skipping", key)
        return 0
    try:
        post_records(client, records)
    except Exception:
        # Release the claim so the next run retries this reading rather than
        # dropping it forever after a transient POST failure.
        ctx.unclaim_dedup_keys({key})
        raise
    ctx.annotation(f"PurpleAir {reading.sensor_id}: {len(records)} measures")
    return len(records)


def run(ctx: RunContext) -> None:
    """One poll pass: fetch every configured sensor, write present measures."""
    mode = _mode(ctx)
    readings = _fetch_readings(ctx, mode)
    definition_ids = _resolve_definition_ids(ctx)
    client = BaseFulcraClient()

    written = 0
    newest = None
    for reading in readings:
        count = _write_reading(ctx, reading, definition_ids, client)
        written += count
        if count and (newest is None or reading.observed_at > newest):
            newest = reading.observed_at

    if newest is not None:
        ctx.kv_set(_LAST_OBSERVED_KV_KEY, newest.isoformat())
    ctx.progress(mode=mode, sensors=len(readings), records=written)


def health_check(ctx: RunContext) -> HealthResult:
    """Best-effort: fetch every configured sensor once and report the latest
    PM2.5 / AQI so the wizard's test-connection step shows data flowing."""
    try:
        mode = _mode(ctx)
        readings = _fetch_readings(ctx, mode)
    except Exception as exc:  # noqa: BLE001 — surface any config/fetch failure
        return HealthResult(ok=False, summary=f"{type(exc).__name__}: {exc}"[:200])
    if not readings:
        return HealthResult(ok=False, summary="No sensors configured.")
    preview = [
        {
            "sensor_id": r.sensor_id,
            "pm2_5": r.pm2_5,
            "aqi": r.aqi,
            "observed_at": r.observed_at.isoformat(),
        }
        for r in readings
    ]
    summary = ", ".join(f"{r.sensor_id}: PM2.5 {r.pm2_5} (AQI {r.aqi})" for r in readings)
    return HealthResult(ok=True, summary=summary[:200], preview=preview)


PLUGIN = Plugin(
    id=PLUGIN_ID,
    name="PurpleAir air quality",
    kind="scheduled",
    collect_mode="live_polled",
    run=run,
    description=(
        "Captures air-quality readings (PM2.5, PM10, EPA AQI, temperature, "
        "humidity, pressure) from a PurpleAir sensor — via the PurpleAir cloud "
        "API or a sensor on your LAN — as per-measure Fulcra tracks. Polls "
        "every 10 minutes."
    ),
    default_interval=DEFAULT_INTERVAL,
    category="other",
    required_credentials=(
        Credential(
            key="api_key",
            label="PurpleAir API key",
            help=(
                "A read key from https://develop.purpleair.com/ — needed for the "
                "cloud 'api' source only; leave unset when reading a sensor on your LAN."
            ),
        ),
    ),
    required_settings=(
        Setting(
            key="mode",
            label="Source",
            kind="enum",
            enum_values=(MODE_API, MODE_LOCAL),
            enum_labels=("PurpleAir cloud API", "Sensor on my LAN"),
            default=MODE_API,
            help=(
                "Read from the PurpleAir cloud API (needs an API key + sensor index) "
                "or directly from a sensor on your local network (needs its IP)."
            ),
        ),
        Setting(
            key="sensor_index",
            label="Sensor index",
            kind="text",
            required=False,
            help=(
                "The numeric PurpleAir sensor index (cloud 'api' source). Find it on "
                "the sensor's page at map.purpleair.com."
            ),
        ),
        Setting(
            key="sensor_ips",
            label="Sensor IP addresses",
            kind="long_text",
            required=False,
            help=(
                "One or more LAN IPs of PurpleAir sensors ('local' source), comma- or "
                "newline-separated. Each must serve http://<ip>/json."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What PurpleAir does",
            body_md=(
                "PurpleAir sensors report local air quality — fine particulates "
                "(PM2.5), PM10, temperature, humidity, and pressure. This plugin "
                "polls one or more sensors every 10 minutes, derives the US EPA "
                "Air Quality Index from PM2.5, and writes each measure to its own "
                "Fulcra track."
            ),
        ),
        SetupStep(
            kind="input",
            title="Choose a source",
            body_md=(
                "**PurpleAir cloud API** works from anywhere but needs a free API "
                "key and the sensor's numeric index. **Sensor on my LAN** talks "
                "straight to the sensor's `http://<ip>/json` endpoint — no key, "
                "but the daemon must be on the same network as the sensor."
            ),
            settings_keys=("mode",),
        ),
        SetupStep(
            kind="external_action",
            title="Get a PurpleAir API key",
            body_md=(
                "Request a **read** key at https://develop.purpleair.com/ and paste "
                "it in the next step, then find your sensor's numeric index on its "
                "page at map.purpleair.com."
            ),
            external_link="https://develop.purpleair.com/",
            condition={"mode": (MODE_API,)},
        ),
        SetupStep(
            kind="input",
            title="PurpleAir API key and sensor index",
            settings_keys=("api_key", "sensor_index"),
            condition={"mode": (MODE_API,)},
        ),
        SetupStep(
            kind="input",
            title="Sensor IP addresses",
            body_md=(
                "Enter one or more LAN IP addresses of your PurpleAir sensors, "
                "comma- or newline-separated."
            ),
            settings_keys=("sensor_ips",),
            condition={"mode": (MODE_LOCAL,)},
        ),
        SetupStep(
            kind="test_connection",
            title="Test the connection",
            body_md="We'll fetch each sensor once to confirm it's reachable and reporting.",
        ),
        SetupStep(
            kind="done",
            title="PurpleAir is set",
            body_md=(
                "The plugin polls every 10 minutes. Each measure lands on its own "
                "Fulcra track (PM2.5, PM10, Air Quality Index, Temperature, "
                "Humidity, Barometric Pressure)."
            ),
        ),
    ),
    health_check=health_check,
)
