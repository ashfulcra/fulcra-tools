"""Poll PurpleAir into account-bound v1 Metrics with a recoverable outbox.

Stable per-measure UUIDs and exact readback replace the old per-sample claim.
Only verified samples advance a receipt/cursor; partial or uncertain delivery
stays durable and is reconciled before fetching the next sensor reading.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fulcra_collect.freshness import FreshnessExpectation
from fulcra_collect.plugin import (
    Credential,
    HealthResult,
    Plugin,
    RunContext,
    Setting,
    SetupStep,
)
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_collect.v1_outbox import source_settings_fingerprint, dispatch_states, pending_dispatch
from fulcra_common.v1 import _destination, record_matches

from .client import fetch_api, fetch_local
from .definitions import METRICS
from .ingest import build_records
from .models import Reading

PLUGIN_ID = "purpleair"
DEFAULT_INTERVAL = timedelta(minutes=10)

#: When silence from this sensor stops being normal.
#
# A PurpleAir sensor samples continuously, so — unlike a human-driven source
# such as a watch or listen history — silence here is UNAMBIGUOUS. Nobody has
# to decide whether the operator simply did not use anything today; a sensor
# that reports nothing is a sensor that is off, unreachable, or refusing us.
# That is what makes this the right first source to monitor.
#
# Twelve hours, not the poll interval. Three reasons, all about not crying wolf:
#   * At the 10-minute default that is ~72 consecutive failed polls, so a
#     dead sensor still surfaces within half a day — the useful range for air
#     quality, which is a trend signal and not an alarm.
#   * The daemon's interval is operator-configurable (`set-interval`), and a
#     bound derived from the default would flap for anyone who widens it.
#   * Sensor reboots, Wi-Fi drops and PurpleAir API maintenance are ordinary
#     multi-hour events. An alert that fires on those gets switched off, and
#     then it is not there for the real one.
#
# Both clocks are declared because they fail apart in one narrow case. A stuck
# sensor clock is caught by yield silence alone: the dedup key embeds the
# reading's timestamp, so a repeated timestamp is claimed once and every later
# poll skips, and nothing is accepted. But a sensor whose clock jumps BACKWARD
# (an NTP correction, a firmware reset) mints fresh dedup keys and keeps
# writing — yields continue while the data it carries is stale. Only the
# upstream bound sees that.
FRESHNESS = FreshnessExpectation(
    max_yield_silence=timedelta(hours=12),
    max_upstream_lag=timedelta(hours=12),
)

MODE_API = "api"
MODE_LOCAL = "local"

# Plugin-scoped KV keys.
# Legacy definition_ids / last_observed remain intact for historical mapping.
_DESTINATIONS_KV_KEY = "v1_destinations"
_PENDING_KV_KEY = "v1_pending"
_LAST_OBSERVED_KV_KEY = "v1_last_observed"


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


def _pending(ctx: RunContext, session: CollectRecordSession) -> dict:
    fingerprint = source_settings_fingerprint(ctx, session)
    pending = ctx.kv_get(_PENDING_KV_KEY, None)
    if pending is None:
        return {"account_id": session.account_id, "epoch": ctx.config_epoch,
                "settings_fingerprint": fingerprint, "items": []}
    if not isinstance(pending, dict) or not isinstance(pending.get("items"), list):
        raise RuntimeError("purpleair: pending outbox is unreadable; reconcile before continuing")
    if pending["items"]:
        if pending.get("account_id") != session.account_id:
            raise RuntimeError("purpleair: pending records belong to another account; reconcile before continuing")
        if pending.get("epoch") != ctx.config_epoch or pending.get("settings_fingerprint") != fingerprint:
            raise RuntimeError("purpleair: pending source configuration changed; reconcile before continuing")
        return pending
    return {"account_id": session.account_id, "epoch": ctx.config_epoch,
            "settings_fingerprint": fingerprint, "items": []}


def _resolve_destinations(ctx: RunContext, session: CollectRecordSession) -> dict[str, str]:
    configured = ctx.config.get("v1_destination_ids", {})
    if not isinstance(configured, dict) or set(configured) - {metric.key for metric in METRICS}:
        raise ValueError("purpleair: invalid v1 destination mapping")
    for pin in configured.values():
        _destination(pin, "Metric")
    cached = ctx.kv_get(_DESTINATIONS_KV_KEY, {}) or {}
    if not isinstance(cached, dict):
        raise ValueError("purpleair: invalid v1 destination cache")
    pins = cached.get("ids", {}) if cached.get("account_id") == session.account_id else {}
    destinations = {}
    for metric in METRICS:
        resolved = session.resolve_destination(metric.data_type_spec(),
            pinned_id=configured.get(metric.key, pins.get(metric.key)), allow_create=metric.key not in configured)
        destinations[metric.key] = resolved.data_type
    session.commit(lambda: ctx.kv_set(_DESTINATIONS_KV_KEY,
                                     {"account_id": session.account_id, "ids": destinations}))
    return destinations


def _deliver_pending(ctx: RunContext, session: CollectRecordSession, pending: dict) -> int:
    written = 0
    while pending["items"]:
        item = pending["items"][0]
        records = item["records"]
        verified = excluded = 0
        for destination, record in records.items():
            dispatch = pending_dispatch(pending, item, destination, [record],
                load=lambda: ctx.kv_get(_PENDING_KV_KEY, None),
                save=lambda value: ctx.kv_set(_PENDING_KV_KEY, value),
                validate=lambda: session.commit(lambda: _pending(ctx, session)))
            result = session.deliver_records(destination, [record], dispatch=dispatch)
            verified += len(result.verified_ids)
            excluded += len(result.excluded_ids)

        def finish():
            if ctx.kv_get(_PENDING_KV_KEY, None) != pending:
                raise RuntimeError("purpleair: original pending source changed; retain batch")
            prior = ctx.kv_get(_LAST_OBSERVED_KV_KEY, {}) or {}
            observed = item["observed_at"]
            if (prior.get("account_id") == session.account_id
                    and datetime.fromisoformat(prior["observed_at"]) > datetime.fromisoformat(observed)):
                observed = prior["observed_at"]
            ctx.kv_set(_LAST_OBSERVED_KV_KEY,
                       {"account_id": session.account_id, "observed_at": observed})
            suffix = f"; {excluded} excluded" if excluded else ""
            ctx.annotation(f"PurpleAir {item['sensor_id']}: {verified} measures{suffix}",
                           observed_at=item["observed_at"])
            pending["items"].pop(0)
            ctx.kv_set(_PENDING_KV_KEY, pending)

        session.commit(finish)
        written += verified
    return written


def run(ctx: RunContext) -> None:
    """Reconcile captured data first, then poll the selected source."""
    mode = _mode(ctx)
    with CollectRecordSession(ctx) as session:
        with session.record_write_scope():
            pending = _pending(ctx, session)
            written = _deliver_pending(ctx, session, pending)
        readings = _fetch_readings(ctx, mode)
        destinations = _resolve_destinations(ctx, session) if readings else {}
        for reading in readings:
            records = build_records(reading, destinations)
            records = {destination: record for destination, record in records.items()
                       if not session.excluded_ids(destination, [record["id"]])}
            if not records or all(record_matches(session.get_record(destination, record["id"]), record)
                                  for destination, record in records.items()):
                continue
            item = {"sensor_id": reading.sensor_id,
                    "observed_at": reading.observed_at.isoformat(), "records": records,
                    "dispatch_version": 1}
            item["dispatch"] = dispatch_states(pending, {d: [r] for d, r in records.items()}, owner=item)
            with session.record_write_scope():
                # Reread before staging: another invocation may have captured work.
                if _pending(ctx, session)["items"]:
                    raise RuntimeError("purpleair: pending source must be reconciled before staging")
                pending["items"].append(item)
                session.commit(lambda: ctx.kv_set(_PENDING_KV_KEY, pending))
                written += _deliver_pending(ctx, session, pending)
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


def _has_pending_records(ctx: RunContext) -> bool:
    pending = ctx.kv_get(_PENDING_KV_KEY, None)
    if pending is None:
        return False
    if not isinstance(pending, dict) or not isinstance(pending.get("items"), list):
        raise ValueError("purpleair pending records are unreadable")
    return bool(pending["items"])


PLUGIN = Plugin(
    id=PLUGIN_ID,
    name="PurpleAir air quality",
    kind="scheduled",
    collect_mode="live_polled",
    run=run,
    record_data_types=lambda _settings: {metric.key: metric.data_type_spec() for metric in METRICS},
    record_pending=_has_pending_records,
    description=(
        "Captures air-quality readings (PM2.5, PM10, EPA AQI, temperature, "
        "humidity, pressure) from a PurpleAir sensor — via the PurpleAir cloud "
        "API or a sensor on your LAN — as per-measure Fulcra tracks. Polls "
        "every 10 minutes."
    ),
    default_interval=DEFAULT_INTERVAL,
    freshness=FRESHNESS,
    category="other",
    required_credentials=(
        Credential(
            key="api_key",
            label="PurpleAir API key",
            help=(
                "A read key from https://develop.purpleair.com/ — needed for the "
                "cloud 'api' source only; leave unset when reading a sensor on your LAN."
            ),
            # Optional: the LAN ('local') source needs no key, so its absence
            # must not block the run. run() requires it only in 'api' mode.
            required=False,
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
