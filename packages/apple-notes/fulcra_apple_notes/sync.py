"""One-way Apple Notes -> Fulcra vault sync, with two-way-ready state.

Failure policy, which is most of the design: a sync that partially fails
must never look like a sync that found nothing. Every per-note failure is
caught, counted and reported, and a note that fails to decode is SKIPPED
rather than written as an empty file -- writing an empty body over a good
note would destroy content in the user's vault, which is far worse than
leaving it stale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import tempfile
import time

from . import notestore, reconcile, reconcile_checkpoint, render, vaultio
from .body import BodyDecodeError, decode

STATE_PATH = "/vault/notes/apple/.sync-state.json"

# The collect worker is killed at 900s (fulcra_collect.runner.DEFAULT_TIMEOUT_S).
# A first sync of a large library takes far longer than that, so the run must
# stop itself BEFORE the kill and leave durable progress behind: a killed run
# that saved nothing makes zero progress and repeats forever, which is the
# failure mode a long-running import falls into by default.
# 600s, not 780s: the deadline can only be observed between units of work,
# and a single note can carry dozens of attachments at ~1.1s each. A 780s
# deadline left only 120s of headroom and a heavy note overshot it -- the
# worker was killed at 900s in production. 300s of headroom absorbs that.
DEFAULT_DEADLINE_S = 600
CHECKPOINT_EVERY = 50
STATE_VERSION = 1
INDEX_PATH = "/vault/notes/apple/Apple Notes.md"

_DELETED_RE = re.compile(r"^apple-deleted:\s*\S+\s*$", re.MULTILINE)


@dataclass
class SyncStats:
    notes_seen: int = 0
    notes_written: int = 0
    notes_unchanged: int = 0
    notes_failed: int = 0
    deletions_marked: int = 0
    attachments_uploaded: int = 0
    attachments_unchanged: int = 0
    attachments_no_file: int = 0
    attachments_too_large: int = 0
    attachments_failed: int = 0
    attachment_bytes: int = 0
    # Quality aggregates. "Did not raise" is not "rendered correctly": a
    # decoder that silently produced empty bodies would report zero
    # failures while destroying every note, so the run has to measure what
    # it actually produced.
    stopped_early: bool = False
    notes_remaining: int = 0
    notes_empty_body: int = 0
    notes_with_attachments: int = 0
    notes_with_structure: int = 0
    markdown_chars: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "errors"}
        d["errors"] = self.errors[:20]
        d["error_count"] = len(self.errors)
        return d


def load_state(*, log, timeout: float = 60) -> dict:
    """Read sync state, distinguishing 'absent' from 'unreadable'.

    A transport failure must NOT be treated as an empty state: that would
    re-upload every note and every attachment, and (worse) would look like
    a successful first run. Absence is proven by MissingFile, nothing else.
    """
    try:
        raw = vaultio.read_text(STATE_PATH, timeout=timeout)
    except vaultio.MissingFile:
        log.info("apple-notes: no sync state yet — treating as first run")
        return {"version": STATE_VERSION, "notes": {}, "attachments": {}}
    except vaultio.VaultIOError:
        raise
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"sync state at {STATE_PATH} is corrupt ({exc}); refusing to run "
            "rather than re-uploading everything over the existing vault"
        ) from exc
    data.setdefault("notes", {})
    data.setdefault("attachments", {})
    return data


def save_state(state: dict, *, now: datetime) -> None:
    """Persist sync state.

    ``updated_at`` is stamped at SAVE time, not from the run's start clock:
    a run can span many minutes across checkpoints, and a future two-way
    sync compares vault-side edit times against this value -- a timestamp
    that is minutes early would make edits look older than they are.
    """
    state["version"] = STATE_VERSION
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["run_started_at"] = now.isoformat()
    vaultio.write_text(STATE_PATH, json.dumps(state, indent=1, sort_keys=True))


def stat_missing(remote: str) -> bool:
    """True only when the file is positively absent."""
    return vaultio.stat(remote) is None


def write_index(stats: "SyncStats", *, now: datetime, total_notes: int) -> None:
    """Write a small overview note so the import is discoverable in the vault.

    Deliberately does NOT touch vault/MAP.md or vault/LOG.md: on a vault
    that is already in use those are curated by whoever owns the vault (and
    rewritten whole-file), so appending to them from here would race that
    owner and could destroy their entry. This note is ours alone.
    """
    body = [
        f"Imported from Apple Notes by `{render.OWNER}`.",
        "",
        f"- Notes synced: **{total_notes}**",
        f"- Attachments stored: **{stats.attachments_uploaded + stats.attachments_unchanged}**",
        f"- Attachments with no file in Apple Notes: {stats.attachments_no_file}",
    ]
    if stats.attachments_too_large:
        body.append(
            f"- Skipped as too large: {stats.attachments_too_large} "
            "(raise `max_attachment_mb` to include them)")
    if stats.deletions_marked:
        body.append(
            f"- Marked deleted in Apple Notes: {stats.deletions_marked} "
            "(kept here — this is now the only copy)")
    body += [
        "",
        "Notes live in this folder, one file per note. Each keeps its body "
        "inside an owner-fenced section; anything you add outside that fence "
        "survives the next sync.",
    ]
    markdown = "\n".join([
        render.frontmatter({
            "title": "Apple Notes",
            "source": "apple-notes",
            "updated-by": render.OWNER,
            "synced-at": now.isoformat(),
        }),
        "",
        render.OPEN_FENCE,
        "\n".join(body),
        render.CLOSE_FENCE,
        "",
        "## Log",
        f"- {now.date().isoformat()} synced {total_notes} notes from Apple Notes",
        "",
    ])
    vaultio.write_text(INDEX_PATH, markdown)


def _mark_deleted_in_place(remote: str, *, now: datetime, log) -> bool:
    """Flip a vault note's apple-deleted flag without re-rendering it.

    The note is gone from Apple Notes, so its body cannot be regenerated --
    the vault copy is now the only copy, and this must not overwrite it.
    """
    try:
        current = vaultio.read_text(remote)
    except vaultio.MissingFile:
        return False
    if re.search(r"^apple-deleted:\s*true\s*$", current, re.MULTILINE):
        return False
    if not _DELETED_RE.search(current):
        return False
    updated = _DELETED_RE.sub("apple-deleted: true", current, count=1)
    updated = updated.rstrip("\n") + (
        f"\n- {now.date().isoformat()} note deleted in Apple Notes; "
        f"vault copy kept by {render.OWNER}\n")
    vaultio.write_text(remote, updated)
    return True


def _sync_attachments(attachments, container: Path, state: dict, stats: SyncStats,
                      *, max_bytes: int, dry_run: bool, log,
                      expired=None) -> tuple[dict[str, str], bool]:
    """Upload each attachment's file once.

    Returns (attachment-id -> vault path, deadline_hit). The deadline is
    checked per ATTACHMENT, not just per note: one note can carry dozens of
    them, and checking only between notes let a heavy note run past the
    worker's kill deadline.
    """
    links: dict[str, str] = {}
    for att in attachments:
        if expired is not None and expired():
            return links, True
        if not att.has_file:
            stats.attachments_no_file += 1
            continue
        known = state["attachments"].get(att.media_uuid)
        if known and known.get("path"):
            links[att.uuid] = known["path"]
        local = notestore.media_file(container, att.media_uuid, att.filename)
        if local is None:
            stats.attachments_no_file += 1
            continue
        try:
            size = local.stat().st_size
        except OSError as exc:
            stats.attachments_failed += 1
            stats.errors.append(f"attachment {att.media_uuid}: stat failed: {exc}")
            continue
        if size > max_bytes:
            stats.attachments_too_large += 1
            continue
        remote_rel = render.attachment_path(att.media_uuid, att.filename or local.name)
        remote = f"/vault/{remote_rel}"
        import hashlib
        try:
            with local.open("rb") as attachment_file:
                fingerprint = hashlib.file_digest(attachment_file, "sha256").hexdigest()
        except OSError as exc:
            stats.attachments_failed += 1
            stats.errors.append(f"attachment {att.media_uuid}: read failed: {exc}")
            continue
        if (known and known.get("sha256") == fingerprint
                and known.get("path") == remote_rel):
            stats.attachments_unchanged += 1
            links[att.uuid] = remote_rel
            continue
        if dry_run:
            stats.attachments_uploaded += 1
            stats.attachment_bytes += size
            links[att.uuid] = remote_rel
            continue
        try:
            vaultio.upload_file(str(local), remote)
        except vaultio.VaultIOError as exc:
            stats.attachments_failed += 1
            stats.errors.append(f"attachment {att.media_uuid}: {exc}")
            continue
        state["attachments"][att.media_uuid] = {"path": remote_rel, "size": size, "sha256": fingerprint}
        stats.attachments_uploaded += 1
        stats.attachment_bytes += size
        links[att.uuid] = remote_rel
    return links, False


def run_sync(*, container: Path | None = None, dry_run: bool = False,
             limit: int | None = None, max_attachment_mb: int = 25,
             deadline_s: float = DEFAULT_DEADLINE_S,
             checkpoint_every: int = CHECKPOINT_EVERY,
             log, progress=None) -> SyncStats:
    container = container or notestore.DEFAULT_GROUP_CONTAINER
    store_path = container / "NoteStore.sqlite"
    now = datetime.now(timezone.utc)
    stats = SyncStats()

    state = load_state(log=log) if not dry_run else {
        "version": STATE_VERSION, "notes": {}, "attachments": {}}

    started = time.monotonic()
    processed_since_checkpoint = 0

    def expired() -> bool:
        return not dry_run and (time.monotonic() - started) > deadline_s

    with tempfile.TemporaryDirectory(prefix="apple-notes-") as td:
        snap = notestore.snapshot(store_path, Path(td))
        with notestore.NoteStore(snap) as store:
            notes = store.notes()
            attachments = store.attachments()

        stats.notes_seen = len(notes)
        by_note: dict[str, list] = {}
        for att in attachments:
            by_note.setdefault(att.note_uuid, []).append(att)

        # Computed from the FULL store read, deliberately before any limit
        # slice: this is the set the deletion sweep tests membership against,
        # and building it from a truncated list would let a limited pass
        # conclude that every note it did not reach had been deleted.
        # Correct by construction here rather than relying on the sweep's
        # own guards, which a later refactor could remove.
        live_uuids = {n.uuid for n in notes}

        if limit is not None:
            notes = notes[:limit]
        max_bytes = max_attachment_mb * 1024 * 1024

        for index, note in enumerate(notes, start=1):
            if progress and index % 100 == 0:
                progress(stage="notes", done=index, total=len(notes))

            # Stop cleanly before the worker is killed, so this run's work
            # is checkpointed and the next run resumes instead of restarting.
            if expired():
                stats.stopped_early = True
                stats.notes_remaining = len(notes) - index + 1
                log.info("apple-notes: stopping at %ds deadline with %d notes "
                         "remaining; progress is saved", deadline_s,
                         stats.notes_remaining)
                break
            try:
                decoded = decode(note.body)
            except BodyDecodeError as exc:
                # Skip, never write. An empty body would overwrite a good
                # note in the vault with nothing.
                stats.notes_failed += 1
                stats.errors.append(f"note {note.uuid[:8]}: body decode failed: {exc}")
                continue

            note_attachments = by_note.get(note.uuid, [])
            links, deadline_hit = _sync_attachments(
                note_attachments, container, state, stats,
                max_bytes=max_bytes, dry_run=dry_run, log=log,
                expired=expired)
            if deadline_hit:
                # Stop BEFORE writing this note: its attachment set is
                # incomplete, and writing it now would record it as synced
                # and leave the remaining attachments unreferenced forever.
                stats.stopped_early = True
                stats.notes_remaining = len(notes) - index + 1
                break
            missing = sum(1 for a in note_attachments if not a.has_file)

            known = state["notes"].get(note.uuid)
            path_rel = (known or {}).get("path") or render.note_filename(
                note.uuid, note.title)
            body_hash = render.content_hash(
                render.resolve_attachments(decoded.markdown, links))
            modified_iso = note.modified.isoformat() if note.modified else ""
            if (known and known.get("hash") == body_hash
                    and known.get("modified") == modified_iso
                    and known.get("deleted") == note.marked_for_deletion):
                stats.notes_unchanged += 1
                continue

            rendered_body = render.resolve_attachments(decoded.markdown, links)
            if not rendered_body.strip():
                stats.notes_empty_body += 1
            stats.markdown_chars += len(rendered_body)
            if links:
                stats.notes_with_attachments += 1
            if any(line.startswith(("#", "- ", "1. ", "    "))
                   for line in rendered_body.split("\n")):
                stats.notes_with_structure += 1

            markdown = render.render_note(
                uuid=note.uuid, title=note.title, folder=note.folder,
                body_markdown=decoded.markdown, modified=note.modified,
                created=note.created, deleted=note.marked_for_deletion,
                synced_at=now, attachment_links=links,
                missing_attachments=missing)

            if not dry_run:
                try:
                    try:
                        existing = vaultio.read_text(f"/vault/{path_rel}")
                    except vaultio.MissingFile:
                        existing = None
                    if existing is not None:
                        markdown = render.merge_note(existing, markdown)
                    vaultio.write_text(f"/vault/{path_rel}", markdown)
                except (vaultio.VaultIOError, ValueError) as exc:
                    stats.notes_failed += 1
                    stats.errors.append(f"note {note.uuid[:8]}: write failed: {exc}")
                    continue
                state["notes"][note.uuid] = {
                    "path": path_rel, "hash": body_hash,
                    "modified": modified_iso, "title": note.title,
                    "deleted": note.marked_for_deletion,
                    # When we wrote it: the freshness check that avoids
                    # downloading every note on reconcile compares the
                    # vault file's mtime against this.
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                }
                processed_since_checkpoint += 1
                if processed_since_checkpoint >= checkpoint_every:
                    save_state(state, now=now)
                    processed_since_checkpoint = 0
            stats.notes_written += 1

        # Notes that vanished from the store: mark, never delete.
        if not dry_run and limit is None and not stats.stopped_early:
            for uuid, meta in list(state["notes"].items()):
                if uuid in live_uuids or meta.get("deleted"):
                    continue
                try:
                    if _mark_deleted_in_place(
                            f"/vault/{meta['path']}", now=now, log=log):
                        stats.deletions_marked += 1
                        meta["deleted"] = True
                except vaultio.VaultIOError as exc:
                    stats.errors.append(f"deletion mark {uuid[:8]}: {exc}")

        if not dry_run:
            save_state(state, now=now)
            # Only refresh the index on a pass that reached the end, so a
            # checkpointed partial run does not advertise a smaller library
            # than the vault actually holds.
            changed = bool(stats.notes_written or stats.deletions_marked)
            if not stats.stopped_early and limit is None:
                try:
                    # Only when something changed, or the index is missing --
                    # a run that found no changes must write nothing, or the
                    # sync is not idempotent and every scheduled pass churns
                    # the vault's history.
                    if changed or stat_missing(INDEX_PATH):
                        write_index(stats, now=now,
                                    total_notes=len(state["notes"]))
                except vaultio.VaultIOError as exc:
                    stats.errors.append(f"index: {exc}")

    return stats


def _links_from_state(attachments, state: dict) -> dict[str, str]:
    """Rebuild attachment links from state alone -- no disk, no network.

    Must reproduce exactly what the forward sync wrote, or every note's
    hash would differ and reconcile would report the whole library as
    changed.
    """
    links: dict[str, str] = {}
    for att in attachments:
        known = state["attachments"].get(att.media_uuid)
        if known and known.get("path"):
            links[att.uuid] = known["path"]
    return links


DEFAULT_RECONCILE_CHECKPOINT = (
    Path.home() / "Library/Logs/fulcra-collect/apple-notes-reconcile-checkpoint.json")
DEFAULT_RECONCILE_DEADLINE_S = 600


def run_reconcile(*, container: Path | None = None, log,
                  limit: int | None = None, download_all: bool = False,
                  deadline_s: float = DEFAULT_RECONCILE_DEADLINE_S,
                  checkpoint_path: Path | None = None) -> dict:
    """Read a bounded portion of a resumable, body-free vault scan.

    Only ``complete: true`` returns a full classification. Checkpoints are
    private local hashes, scoped to the container and sync-state baseline.
    Apple is freshly snapshotted and classified on every invocation. Complete
    scans consume the checkpoint, so the next scan observes new vault edits.
    ``limit`` caps new observations in this invocation, not the returned list.
    ``download_all`` remains accepted; no coarse-mtime shortcut is used.
    """
    started = time.monotonic()
    deadline = started + min(float(deadline_s), DEFAULT_RECONCILE_DEADLINE_S)
    if limit is not None and limit < 0:
        raise ValueError("Reconciliation limit must be nonnegative")
    container = container or notestore.DEFAULT_GROUP_CONTAINER
    checkpoint_path = Path(checkpoint_path or DEFAULT_RECONCILE_CHECKPOINT)
    with reconcile_checkpoint.locked(checkpoint_path):
        return _scan_reconcile(container=container, log=log, limit=limit,
                               deadline=deadline, checkpoint_path=checkpoint_path)


def _scan_reconcile(*, container: Path, log, limit: int | None,
                    deadline: float, checkpoint_path: Path) -> dict:
    now = datetime.now(timezone.utc)
    state = None
    scope = None
    observations = {}
    fetched = 0
    phase = "state"

    def remaining():
        return max(0.0, deadline - time.monotonic())

    def result(complete=False, changes=()):
        total = len(state["notes"]) if state is not None else None
        pending = total - len(observations) if total is not None else None
        if scope is not None:
            if complete:
                reconcile_checkpoint.clear(checkpoint_path)
            else:
                reconcile_checkpoint.save(checkpoint_path, scope, observations)
        return {
            "checked_at": now.isoformat(), "complete": complete,
            "stopped_early": not complete,
            "notes_in_state": total, "notes_remaining": pending,
            "vault_files_fetched": fetched,
            "progress": {"phase": "complete" if complete else phase,
                         "observed": len(observations), "total": total,
                         "remaining": pending},
            "summary": reconcile.summarize(changes),
            "changes": [c.__dict__ | {"status": c.status.value}
                        for c in changes if c.status is not reconcile.Status.UNCHANGED],
        }

    if remaining() <= 0:
        return result()
    state = load_state(log=log, timeout=min(60.0, remaining()))
    scope = reconcile_checkpoint.scope_key(container, state)
    observations = reconcile_checkpoint.load(checkpoint_path, scope)
    observations = {uuid: obs for uuid, obs in observations.items() if uuid in state["notes"]}
    phase = "snapshot"
    if remaining() <= 0:
        return result()
    with tempfile.TemporaryDirectory(prefix="apple-notes-rec-") as td:
        snap = notestore.snapshot(container / "NoteStore.sqlite", Path(td),
                                  timeout=min(notestore.SNAPSHOT_TIMEOUT_S, remaining()))
        if remaining() <= 0:
            return result()
        with notestore.NoteStore(snap) as store:
            notes = store.notes()
            if remaining() <= 0:
                return result()
            attachments = store.attachments()

    by_note = {}
    for att in attachments:
        by_note.setdefault(att.note_uuid, []).append(att)

    phase = "apple"
    apple = {}
    for note in notes:
        if remaining() <= 0:
            return result()
        try:
            decoded = decode(note.body)
        except BodyDecodeError as exc:
            # A failed decode is not evidence that the Apple note was deleted.
            reconcile_checkpoint.save(checkpoint_path, scope, observations)
            raise RuntimeError("Could not decode an Apple note during reconciliation") from exc
        links = _links_from_state(by_note.get(note.uuid, []), state)
        body = render.resolve_attachments(decoded.markdown, links)
        apple[note.uuid] = (render.content_hash(body),
                            note.modified.isoformat() if note.modified else "")

    phase = "vault"
    observed_this_run = 0
    try:
        for uuid, entry in state["notes"].items():
            if uuid in observations:
                continue
            if remaining() <= 0 or (limit is not None and observed_this_run >= limit):
                break
            try:
                text = vaultio.read_text(f"/vault/{entry.get('path', '')}",
                                         timeout=min(60.0, remaining()))
                fetched += 1
            except vaultio.MissingFile:
                text = None
            observations[uuid] = reconcile.observe_vault(text)
            observed_this_run += 1
            if observed_this_run % CHECKPOINT_EVERY == 0:
                reconcile_checkpoint.save(checkpoint_path, scope, observations)
    except vaultio.VaultIOError:
        # Keep successful observations, but never authorize a partial worklist.
        reconcile_checkpoint.save(checkpoint_path, scope, observations)
        raise

    changes = []
    for uuid, entry in state["notes"].items():
        if uuid not in observations:
            continue
        apple_hash, apple_modified = apple.get(uuid, (None, None))
        changes.append(reconcile.classify_observation(
            uuid=uuid, apple_hash=apple_hash, apple_modified=apple_modified,
            vault=observations[uuid], state_entry=entry))
    for note in notes:
        if note.uuid not in state["notes"]:
            changes.append(reconcile.Change(uuid=note.uuid,
                status=reconcile.Status.NEW_IN_APPLE, title=note.title))
    complete = len(observations) == len(state["notes"]) and remaining() > 0
    return result(complete, changes)
