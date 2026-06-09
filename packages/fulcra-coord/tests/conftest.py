"""Hermetic test environment for fulcra-coord.

WHY THIS EXISTS — a real incident, not a nicety:

``cache.cache_root()`` resolves to ``${XDG_CACHE_HOME:-~/.cache}/fulcra-coord``.
So any test that exercises a cache-writing code path (``write_cached_task``,
``_write_task_and_views``, ``cmd_reconcile``, ``_sweep_review_routes`` …)
WITHOUT first redirecting ``XDG_CACHE_HOME`` writes straight into the
*operator's real* ``~/.cache/fulcra-coord``. Most test classes set
``XDG_CACHE_HOME`` by hand in ``setUp`` — but several did not (e.g. the
reviewer-routing sweep tests), and their fixtures (``author:h:r``,
``dead:h:r``, a title-less ``TASK-20260604-rev-00000000``) leaked into the
real cache. ``reconcile`` then read that polluted local cache and PUSHED the
junk tasks to the live remote coordination bus, where they crashed reconcile.
A prior run left 127 stray tasks in a developer's real ``~/.cache`` the same
way.

The fix is to make the cache hermetic BY DEFAULT for every test, so isolation
no longer depends on each test remembering to do it. This autouse fixture
points ``XDG_CACHE_HOME`` at a fresh per-test temp dir and restores the prior
value afterward. Per-test (function) scope — not session — so tests never
share cache state, matching the per-test isolation the careful tests already
did manually (their in-``setUp`` ``XDG_CACHE_HOME`` assignment still wins,
since it runs inside this fixture's redirected world and is harmless).

It also defaults ``FULCRA_COORD_BACKEND`` to ``false`` when unset, so a test
that reaches an unmocked remote file-op can never shell out to the real
``fulcra`` CLI and touch the live account — it hits the no-op ``false`` binary
instead. Tests that inject their own backend (the stateful fake, or an
explicit ``backend=`` argument) override this freely.

NOTE on scope mechanics: the suite is written with ``unittest.TestCase``
classes run under pytest. pytest applies ``autouse`` fixtures around unittest
tests, but it will NOT inject the ``tmp_path`` / ``monkeypatch`` fixtures into
unittest methods. So this fixture manages the temp dir and env vars by hand
(``tempfile`` + ``os.environ``) rather than requesting those fixtures — that
keeps it working for the existing unittest classes AND any future
pytest-style tests.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_cache_and_backend():
    """Redirect XDG_CACHE_HOME to a throwaway dir for the duration of one test,
    and default the file-ops backend to a safe no-op. Restores prior env after."""
    prev_xdg = os.environ.get("XDG_CACHE_HOME")
    prev_backend = os.environ.get("FULCRA_COORD_BACKEND")
    prev_annotations = os.environ.get("FULCRA_COORD_ANNOTATIONS")

    tmp = tempfile.mkdtemp(prefix="fulcra-coord-test-cache-")
    os.environ["XDG_CACHE_HOME"] = tmp

    # Safety net: if a test reaches an unmocked remote file op, ``false`` exits
    # non-output / 0 instead of invoking the real Fulcra CLI. Only set when the
    # test (or its setUp, which runs after this fixture yields) hasn't chosen a
    # backend of its own.
    if prev_backend is None:
        os.environ["FULCRA_COORD_BACKEND"] = "false"

    # Safety net for the OTHER live-write path: annotations. The annotation mode
    # resolves from FULCRA_COORD_ANNOTATIONS (env) > a persisted file under
    # ${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/annotations > off. The HTTP
    # transport writes over urllib DIRECTLY — it does NOT go through
    # FULCRA_COORD_BACKEND, and the persisted file lives under XDG_CONFIG_HOME
    # (which this fixture does not isolate). So on a machine where the operator
    # ran ``fulcra-coord annotations on`` (persisting ``http``), an end-to-end
    # command test that emits a lifecycle annotation as a side effect resolved
    # mode ``http``, obtained a real bearer token, and POSTed fixture titles
    # ("Fix the widget pipeline", "do x", "t1", "resolve me", …) to the
    # operator's LIVE Agent-Tasks timeline — the very surface their
    # situational-awareness reports read from. The pre-push hook runs the whole
    # suite, so every fulcra-coord push re-polluted it. Defaulting the env to
    # ``off`` here (env wins over the persisted file) makes the live path
    # unreachable for every test by default; tests that exercise annotation
    # modes set FULCRA_COORD_ANNOTATIONS themselves and still override this.
    if prev_annotations is None:
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "off"

    try:
        yield tmp
    finally:
        if prev_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = prev_xdg

        if prev_backend is None:
            os.environ.pop("FULCRA_COORD_BACKEND", None)
        else:
            os.environ["FULCRA_COORD_BACKEND"] = prev_backend

        if prev_annotations is None:
            os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        else:
            os.environ["FULCRA_COORD_ANNOTATIONS"] = prev_annotations

        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def coord_backend(tmp_path):
    """Per-test isolated fake Fulcra backend for eventlog I/O tests.

    Points FULCRA_FAKE_ROOT at the per-test ``tmp_path`` (distinct from the
    shared tmpdir used by the autouse hermetic fixture) so each test gets a
    clean, empty store. Returns an explicit backend list — callers pass it as
    ``backend=coord_backend`` — so the env-var ``FULCRA_COORD_BACKEND`` set by
    the autouse fixture (the ``false`` safety net) is bypassed entirely and
    never reaches the fake backend script.
    """
    os.environ["FULCRA_FAKE_ROOT"] = str(tmp_path)
    fake = Path(__file__).resolve().parent / "fake_fulcra_backend.py"
    return [sys.executable, str(fake)]
