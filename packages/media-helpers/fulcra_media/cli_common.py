"""Shared CLI plumbing — the agent-friendly JSON envelope + emit_result.

Every `fulcra-media import <X>` command builds an ImportEnvelope and passes
it to `emit_result`. When --json mode is set, emit_result writes exactly one
line of JSON to stdout (nothing else) and exits non-zero on failure. In
human mode, errors go to stderr.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import click

from .fulcra import ImportResult


# Auth-bearing query params that must be scrubbed before exception strings
# (e.g. httpx.HTTPStatusError) land in --json error envelopes. Adding new
# importers? If your service puts a secret in a URL param, add the name here.
_SECRET_QUERY_PARAMS = (
    "access_token", "api_key", "client_secret", "refresh_token",
    "token", "api_secret", "auth_token", "session_key",
)
_SECRET_RE = re.compile(
    r"(?<=[?&])(" + "|".join(_SECRET_QUERY_PARAMS) + r")=[^&\s'\"]+",
    re.IGNORECASE,
)


def safe_exc_message(exc: BaseException) -> str:
    """str(exc) with credential-shaped URL params scrubbed.

    Use this everywhere an exception string flows into an ImportEnvelope's
    errors[].message. httpx.HTTPStatusError in particular embeds the full
    request URL which often carries our auth params for services that
    don't support header-based auth (Last.fm, Deezer).
    """
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=REDACTED", str(exc))



@dataclass
class ImportEnvelope:
    """Structured per-import result, JSON-serializable.

    Sent verbatim to stdout when --json is set, so the schema is part of the
    public contract. Add fields only at the end; never reorder or rename.
    """
    importer: str
    ok: bool
    total: int = 0
    skipped_existing: int = 0
    posted: int = 0
    verified: int = 0
    since_watermark: str | None = None
    new_watermark: str | None = None
    # Set on --check-only runs: how many events would post if we did run.
    would_post: int | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    cursor: dict[str, str] | None = None


def import_result_to_dict(
    importer: str,
    result: ImportResult,
    *,
    since_watermark: str | None,
    new_watermark: str | None,
    would_post: int | None = None,
    cursor: dict[str, str] | None = None,
) -> ImportEnvelope:
    """Adapter — turn a FulcraClient ImportResult into an ImportEnvelope."""
    return ImportEnvelope(
        importer=importer,
        ok=True,
        total=result.total,
        skipped_existing=result.skipped_existing,
        posted=result.posted,
        verified=result.verified,
        since_watermark=since_watermark,
        new_watermark=new_watermark,
        would_post=would_post,
        cursor=cursor,
    )


def media_failure_envelope(
    importer: str, *, stage: str, message: str, run=None,
    result: ImportResult | None = None, check_only: bool = False,
) -> ImportEnvelope:
    """Copy current invocation evidence after ordinary context cleanup.

    A returned result survives later context-exit failure. Otherwise only the
    local invocation snapshot is evidence; recovery/outbox counts aren't.
    Malformed internal evidence raises instead of manufacturing valid counters.
    """
    observed = result if result is not None else (run.progress if run is not None else None)
    values = {}
    if observed is not None:
        for key in ('total', 'skipped_existing', 'posted', 'verified'):
            value = getattr(observed, key)
            if type(value) is not int or value < 0:
                raise ValueError(f'invalid media progress {key}')
            values[key] = value
        if (values['skipped_existing'] + values['posted'] > values['total']
                or values['verified'] > values['posted']):
            raise ValueError('inconsistent media progress counters')
        if check_only and values['verified'] != 0:
            raise ValueError('preview progress cannot be verified')
    return ImportEnvelope(
        importer=importer, ok=False, **values,
        would_post=values['posted'] if check_only and observed is not None else None,
        errors=[{'stage': stage, 'message': message}],
    )


def resolve_or_emit(
    importer_name: str, path: str, *, json_mode: bool,
) -> Path | None:
    """Wrap library.resolve so missing files become a structured envelope.

    Returns the resolved Path on success; emits + returns None on failure.
    """
    from . import library
    try:
        return Path(library.resolve(path))
    except (FileNotFoundError, RuntimeError) as exc:
        emit_result(
            ImportEnvelope(
                importer=importer_name, ok=False,
                errors=[{"stage": "args", "message": f"resolve {path!r}: {exc}"}],
            ),
            json_mode=json_mode,
        )
        return None


def emit_result(envelope: ImportEnvelope, *, json_mode: bool) -> None:
    """Print the envelope (JSON or human) and exit non-zero on failure.

    Exit code: 0 when ok=True, 2 when ok=False. (Using 2 to distinguish
    "import failed" from click's own exit code 1 for usage errors.)
    """
    payload = asdict(envelope)
    if json_mode:
        click.echo(json.dumps(payload), nl=True)
    else:
        fields = []
        for k, v in payload.items():
            if k in ("errors",):
                continue
            if v is None or v == [] or v == {}:
                continue
            if k == "cursor":
                fields.append(f"cursor={v['state']} reason={v['reason']}")
            else:
                fields.append(f"{k}={v}")
        click.echo(" ".join(fields))
        for err in envelope.errors:
            click.echo(
                f"  error in {err.get('stage', '?')}: {err.get('message', '?')}",
                err=True,
            )
    if not envelope.ok:
        sys.exit(2)
