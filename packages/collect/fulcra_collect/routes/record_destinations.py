"""Owned v1 record destinations: source specs, proof and fenced local binding."""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from fulcra_common.data_types import DataTypeSpec, _owned_types
from fulcra_common.v1 import _destination
from fulcra_common.record_exclusions import find_exclusion_types

from .. import config, db, state
from ..plugin import RunContext, normalize_legacy_plugin_settings
from ..v1_outbox import has_pending_records
from ..v1_session import CollectRecordSession


class DestinationBindBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str = Field(min_length=1, max_length=128)
    data_type_id: str | None = Field(default=None, max_length=64)
    use_or_create: StrictBool = False
    new_name: str | None = Field(default=None, max_length=256)


@contextmanager
def _errors():
    try:
        yield
    except HTTPException:
        raise
    except config.ConfigConflictError:
        raise
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            raise HTTPException(401, "Reconnect to Fulcra in Collect.") from None
        raise HTTPException(502, f"Fulcra rejected destination setup ({status}); no binding was saved.") from None
    except httpx.TimeoutException:
        raise HTTPException(504, "Fulcra destination setup timed out; retry to reconcile.") from None
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach Fulcra; no binding was saved.") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None


def _context(plugin):
    cfg = config.load()
    raw = deepcopy(cfg.plugin_settings.get(plugin.id, {}))
    return RunContext(plugin_id=plugin.id, config=normalize_legacy_plugin_settings(plugin, raw),
        credentials={}, state=state.load(plugin.id), log=logging.getLogger("fulcra_collect.destinations"),
        _emit=lambda _: None, config_epoch=cfg.plugin_epochs.get(plugin.id, ""), _persisted_config=raw,
        _plugin_kv_get=lambda key, default: db.get_plugin_kv(db.open(), plugin.id, key, default),
        _record_outbox_get=lambda key, default: db.get_record_outbox(db.open(), plugin.id, key, default))


def _specs(plugin, run_ctx) -> dict[str, DataTypeSpec]:
    if plugin.record_data_types is None:
        raise HTTPException(409, "This source has not switched to v1 record destinations.")
    specs = plugin.record_data_types(deepcopy(run_ctx.config))
    if (not isinstance(specs, dict) or not specs
            or any(not isinstance(key, str) or not key or not isinstance(spec, DataTypeSpec)
                   for key, spec in specs.items())):
        raise ValueError("source v1 destination contract is malformed")
    return specs


def _pending(plugin, run_ctx):
    try:
        pending = plugin.record_pending(run_ctx) if plugin.record_pending is not None else has_pending_records(run_ctx)
        if type(pending) is not bool:
            raise ValueError
    except ValueError:
        raise HTTPException(409, "Pending record state is unreadable; reconcile it before changing destinations.") from None
    if pending:
        raise HTTPException(409, "Finish pending record uploads before changing destinations.")


def register(app: FastAPI, route_ctx) -> None:
    daemon = route_ctx.daemon
    authorized = [Depends(route_ctx.require_token)]

    def plugin_for(plugin_id):
        plugin = daemon.registry.plugins.get(plugin_id)
        if plugin is None:
            raise HTTPException(404, "Unknown source.")
        return plugin

    @app.get("/api/plugin/{plugin_id}/data-types", dependencies=authorized)
    def list_destinations(plugin_id: str, slot: str | None = None):
        with _errors():
            plugin = plugin_for(plugin_id)
            run_ctx = _context(plugin)
            specs = _specs(plugin, run_ctx)
            if slot is None:
                if len(specs) != 1:
                    return {"slots": [{"key": key, "name": spec.name, "base_type": spec.base_type}
                                      for key, spec in specs.items()]}
                slot = next(iter(specs))
            if slot not in specs:
                raise HTTPException(400, "Unknown destination slot.")
            spec = specs[slot]
            mappings = run_ctx.config.get("v1_destination_ids", {})
            if not isinstance(mappings, dict):
                raise ValueError("source v1 destination mapping is malformed")
            with CollectRecordSession(run_ctx, require_enabled=False) as session:
                catalog = _owned_types(session.client._client(), session.client._authed_headers(), session.account_id)
                binding = session.record_policy_binding()
                controls = {binding} if binding else find_exclusion_types(
                    client=session.client, account_id=session.account_id, owned_types=catalog)
                destinations = []
                for row in catalog:
                    if (row.get("fulcra_userid") != session.account_id or row.get("api_version") != "v1"
                            or row.get("recordable") is not True or row.get("deprecated")
                            or not str(row.get("id", "")).startswith(spec.base_type + "/")):
                        continue
                    destination = _destination(row["id"], spec.base_type)
                    if destination in controls:
                        continue
                    destinations.append({"id": destination, "name": row.get("name", destination),
                                         "base_type": spec.base_type, "created_at": row.get("created_at")})
                selected = mappings.get(slot)
                session.guard()
                return {"slot": slot, "name": spec.name, "base_type": spec.base_type,
                        "destinations": destinations, "selected": selected,
                        "requires_legacy_mapping": bool(getattr(run_ctx.state, "definition_id", None))}

    @app.get("/api/plugin/{plugin_id}/data-type/recent", dependencies=authorized)
    def recent_destination(plugin_id: str, slot: str, data_type_id: str):
        with _errors():
            plugin = plugin_for(plugin_id)
            run_ctx = _context(plugin)
            specs = _specs(plugin, run_ctx)
            if slot not in specs:
                raise HTTPException(400, "Unknown destination slot.")
            with CollectRecordSession(run_ctx, require_enabled=False) as session:
                resolved = session.resolve_destination(specs[slot], pinned_id=data_type_id, allow_create=False)
                latest = session.get_latest(resolved.data_type)
                session.guard()
                display = {key: value for key, value in (latest or {}).items()
                           if key in {"note", "title", "value", "unit", "start_time", "end_time"}}
                return {"entries": [display] if latest is not None else [], "window_days": 30}

    @app.post("/api/plugin/{plugin_id}/data-type", dependencies=authorized)
    def bind_destination(plugin_id: str, body: DestinationBindBody):
        with _errors():
            plugin = plugin_for(plugin_id)
            run_ctx = _context(plugin)
            specs = _specs(plugin, run_ctx)
            if body.slot not in specs:
                raise HTTPException(400, "Unknown destination slot.")
            if bool(body.data_type_id) == body.use_or_create:
                raise HTTPException(400, "Choose an existing destination or use/create a matching one.")
            if body.new_name is not None and not body.use_or_create:
                raise HTTPException(400, "A new name is only valid when creating a destination.")
            _pending(plugin, run_ctx)
            spec = specs[body.slot]
            if body.new_name and body.new_name.strip():
                name = body.new_name.strip()
                if name != spec.name:
                    suffix = hashlib.sha256(name.encode()).hexdigest()[:16]
                    spec = replace(spec, key=f"{spec.key}.named.{suffix}", name=name)
            with CollectRecordSession(run_ctx, require_enabled=False) as session:
                resolved = session.resolve_destination(spec, pinned_id=body.data_type_id,
                    allow_create=body.use_or_create)
                session.prepare_record_policy()
                mappings = deepcopy(run_ctx.config.get("v1_destination_ids", {}))
                if not isinstance(mappings, dict):
                    raise ValueError("source v1 destination mapping is malformed")
                mappings[body.slot] = resolved.data_type

                def validate():
                    session.guard()
                    _pending(plugin, run_ctx)

                config.update_plugin_settings_if_current(plugin_id, expected_epoch=session.epoch,
                    expected_settings=session.settings, values={"v1_destination_ids": mappings}, validate=validate)
            daemon.handle_request({"cmd": "reload"})
            return {"ok": True, "slot": body.slot, "data_type_id": resolved.data_type, "api_version": "v1"}
