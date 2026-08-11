"""The fast path must prove the engine IS the pin, not merely that it can talk.

MEASURED (coord-maintainer, 2026-08-11): the sentinel `~/.coord-adopted-pin`
records the pin this user last ADOPTED. That is a CLAIM about a past action, not
evidence about the engine on disk now. The fast path paired it with
`coord-engine bus-v3 --help` — but every pin since bus-v3 shipped carries that
verb, so verb-presence cannot tell pin X from pin Y. The pair proves "I once
adopted X" and "some bus-v3 engine is installed"; nothing proved the installed
engine IS X.

WHY THAT STATE IS REACHABLE, and why it stopped being theoretical: coord-opus-worker
CORRECTED their harness doc on 2026-08-11 to record that their box's snapshot
restore is frequent-but-not-guaranteed AND **not uniform across the
filesystem** — one wake came up with `$HOME` intact while `/tmp` was empty. The
sentinel lives in `$HOME`; the engine lives in the uv tool dir. Once those two
are known to revert independently, "my marker survived" stops implying "the
engine it describes survived". Their box fails safe TODAY only because its image
predates bus-v3, so the verb check catches it. A snapshot taken now would not be
caught by anything.

The fix uses the mechanism this repo already settled on for build identity in PR
598: `direct_url.json`'s `vcs_info.commit_id`, read from the engine's OWN
environment. The sentinel and the pin are both full 40-hex commits, so this is a
direct comparison, not a heuristic.

FAIL-SAFE DIRECTION: any doubt — metadata missing, unreadable, malformed, or an
interpreter that cannot run — falls through to the full install, which is the
script's standing rule. A skipped install on a stale engine is silent and
permanent; a redundant install costs ~30-60s.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "adopt-latest.sh"

PIN = "1111111111111111111111111111111111111111"
OTHER = "2222222222222222222222222222222222222222"


def _fast_path_fn() -> str:
    """`engine_is_current` verbatim from the shipped script.

    Anchored on the definition line and closed by counting to the function's own
    `}` at column zero — the same extraction discipline as
    test_adopt_never_strips_the_store_client.py, so an edit inside the body
    cannot silently truncate what the test drives.
    """
    text = SCRIPT.read_text()
    opener = "engine_is_current() {"
    assert opener in text, (
        "the fast path is not a function — it cannot be driven, which is how "
        "the gap shipped in the first place")
    rest = text[text.index(opener):].splitlines(keepends=True)
    for i, line in enumerate(rest):
        if i and line.rstrip("\n") == "}":
            return "".join(rest[: i + 1])
    raise AssertionError("unterminated engine_is_current() — anchor moved")


def _run(tmp_path, *, sentinel: str | None, installed: str | None,
         bus_v3: bool = True, writer: bool = True,
         metadata: str | None = "json") -> bool:
    """Drive the real function against a stub engine. Returns whether the fast
    path was taken."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tools = tmp_path / "tools"

    if sentinel is not None:
        (home / ".coord-adopted-pin").write_text(sentinel)

    # The engine environment: a python that satisfies the writer import, and the
    # dist-info metadata that records which commit was actually installed.
    env_dir = tools / "coord-engine"
    (env_dir / "bin").mkdir(parents=True)
    py = env_dir / "bin" / "python"
    py.write_text(
        "#!/bin/sh\n"
        # `-c 'import fulcra_common'` is the writer probe.
        f"case \"$*\" in *fulcra_common*) exit {0 if writer else 1};; esac\n"
        # Everything else is the metadata read; defer to the real interpreter so
        # the test exercises the script's actual parsing, not a canned answer.
        "exec /usr/bin/env python3 \"$@\"\n")
    py.chmod(0o755)

    site = env_dir / "lib" / "python3.13" / "site-packages"
    dist = site / "coord_engine-1.11.0.dist-info"
    dist.mkdir(parents=True)
    if metadata == "json" and installed is not None:
        (dist / "direct_url.json").write_text(json.dumps(
            {"url": "https://github.com/ashfulcra/fulcra-tools",
             "vcs_info": {"vcs": "git", "commit_id": installed}}))
    elif metadata == "malformed":
        (dist / "direct_url.json").write_text("{not json at all")
    elif metadata == "no_commit":
        (dist / "direct_url.json").write_text(json.dumps(
            {"url": "https://github.com/ashfulcra/fulcra-tools"}))
    # metadata is None -> the file is simply absent

    (bin_dir / "uv").write_text(
        f"#!/bin/sh\n[ \"$1\" = 'tool' ] && [ \"$2\" = 'dir' ] && echo '{tools}'\n")
    (bin_dir / "uv").chmod(0o755)
    (bin_dir / "coord-engine").write_text(
        f"#!/bin/sh\ncase \"$1\" in bus-v3) exit {0 if bus_v3 else 1};; esac\nexit 0\n")
    (bin_dir / "coord-engine").chmod(0o755)

    script = (
        f'PIN="{PIN}"\n'
        f'SENTINEL="{home}/.coord-adopted-pin"\n'
        + _fast_path_fn()
        + '\nif engine_is_current; then echo FAST; else echo FULL; fi\n')
    out = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home)})
    assert "FAST" in out.stdout or "FULL" in out.stdout, (
        f"the function did not decide: {out.stdout!r} {out.stderr!r}")
    return "FAST" in out.stdout


def test_a_matching_sentinel_over_a_DIFFERENT_build_does_not_skip(tmp_path):
    """THE regression.

    Sentinel says the pin, the engine speaks bus-v3, the writer imports — every
    check the old fast path made passes — but the installed build is a different
    commit. Skipping here leaves the host on the wrong engine for the whole wake
    while `adopt` reports success.
    """
    assert not _run(tmp_path, sentinel=PIN, installed=OTHER), (
        "the fast path skipped the install for an engine that is NOT the pin — "
        "sentinel and verb-presence cannot distinguish one pin from another")


def test_a_matching_sentinel_over_the_MATCHING_build_still_skips(tmp_path):
    """The other direction: the fast path must keep doing its job. It exists to
    stop concurrent identities on a shared box from clobbering each other's
    install, and a check that never passes would reinstate that collision."""
    assert _run(tmp_path, sentinel=PIN, installed=PIN), (
        "the fast path no longer fires when the host IS genuinely at the pin — "
        "that restores the shared-box reinstall collision it was added to fix")


def test_ABSENT_metadata_falls_through_to_the_full_install(tmp_path):
    """Fail-safe. Unreadable metadata is UNKNOWN, and UNKNOWN is never
    'already-current' — the script's standing rule, and the same lesson as
    treating a failed probe as absence."""
    assert not _run(tmp_path, sentinel=PIN, installed=None, metadata=None), (
        "missing build metadata was treated as proof of currency")


def test_MALFORMED_metadata_falls_through_and_does_not_crash(tmp_path):
    """A truncated write or a partially restored snapshot leaves half a file.
    That must fall through, not raise out of the adopt run — this script's whole
    header rule is that a failed adopt leaves the host no worse."""
    assert not _run(tmp_path, sentinel=PIN, installed=None,
                    metadata="malformed"), (
        "malformed build metadata was treated as proof of currency")


def test_metadata_WITHOUT_a_commit_id_falls_through(tmp_path):
    """A non-VCS install (a wheel, a local path) has no `vcs_info`. It is not
    the pin, and absence of the field is not a match."""
    assert not _run(tmp_path, sentinel=PIN, installed=None,
                    metadata="no_commit"), (
        "a build with no recorded commit was treated as the pin")


def test_a_MISSING_sentinel_still_falls_through(tmp_path):
    """Pre-existing behaviour, pinned so the refactor into a function cannot
    quietly drop a check."""
    assert not _run(tmp_path, sentinel=None, installed=PIN)


def test_a_STALE_sentinel_still_falls_through(tmp_path):
    """The case the fast path was always right about: a pin moved, so this user
    has not adopted the current one. Correct even when the build matches,
    because at a pin move it never does."""
    assert not _run(tmp_path, sentinel=OTHER, installed=PIN)


def test_a_missing_bus_v3_verb_still_falls_through(tmp_path):
    """The pre-bus-v3 snapshot case — the one check that DOES catch
    coord-opus-worker's July image today. It must survive the refactor."""
    assert not _run(tmp_path, sentinel=PIN, installed=PIN, bus_v3=False)


def test_a_failing_writer_import_still_falls_through(tmp_path):
    """`fulcra_common` must ride in the SAME environment as the engine or
    `annotate project` and `digest --emit-timeline` become silent no-ops (the
    2026-08-04 digest-darkness root cause). A build at the right commit with no
    writer is still not adopted."""
    assert not _run(tmp_path, sentinel=PIN, installed=PIN, writer=False)
