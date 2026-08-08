"""`_host()` must never mint an unusable fleet identity.

One host has been registered in this fleet as ``coord-reconcile: <control
chars>`` since at least 2026-07-16 (v1.3.0, 238 tasks written, 241h stale). The
id built from a hostname is the KEY for presence, health, roles and leases, so a
hostname the OS hands back unvalidated becomes a permanent hole in a shared
keyspace: unmatched by any fold that keys on name, impossible to `tell`,
invisible to a version-skew audit, unaddressable forever.

`socket.gethostname()` is called in exactly ONE place, so this is the whole mint.
"""

from __future__ import annotations

import pytest

from coord_engine import cli


@pytest.fixture(autouse=True)
def _no_explicit_identity(monkeypatch):
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    monkeypatch.setattr(cli, "_hostname_rewrite_warned", False)


@pytest.mark.parametrize("hostname", [
    "Ashs-MBP-Work",
    "MacBookPro.localdomain",
    "DeskBookPro",
    "host_1.example.com",
    "arc-bot.local",
])
def test_real_hostnames_are_passed_through_unchanged(hostname, monkeypatch):
    """THE no-regression property, and the reason this change is safe to ship.

    Every identity in the current fleet must survive byte-identical — a
    sanitiser that rewrites a live hostname would silently fork that host's
    presence, lease and health history into a new key.
    """
    monkeypatch.setattr(cli.socket, "gethostname", lambda: hostname)
    safe, rewritten = cli._sanitize_hostname(hostname)
    assert safe == hostname and rewritten is False
    assert cli._host() == f"coord-reconcile:{hostname}"


def test_control_characters_are_sanitised_and_reported(monkeypatch, capsys):
    """The live corruption: '\\x00aU' is the shape actually in the keyspace."""
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "\x00aU")
    got = cli._host()
    assert got.startswith("coord-reconcile:aU-")   # collapsed stem + raw digest
    assert "\x00" not in got
    err = capsys.readouterr().err
    assert "not a usable fleet id" in err
    assert "FULCRA_COORD_AGENT" in err, "the warning must name the way to pin it"


def test_a_hostname_with_no_usable_characters_REFUSES_rather_than_inventing(
        monkeypatch):
    """Refuse, do not invent. A process that cannot establish who it is must not
    write to a shared keyspace — minting a placeholder is exactly how the
    phantom-identity traps got there."""
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "\x00\x01\x02")
    with pytest.raises(RuntimeError) as e:
        cli._host()
    assert "FULCRA_COORD_AGENT" in str(e.value)


def test_an_explicit_identity_is_never_rewritten(monkeypatch):
    """FULCRA_COORD_AGENT is an operator's deliberate choice and short-circuits
    before any hostname work. Scope note: the four other sites that read this
    env var directly are NOT covered here — this fix is the hostname mint."""
    monkeypatch.setenv("FULCRA_COORD_AGENT", "coord-maintainer")
    monkeypatch.setattr(cli.socket, "gethostname",
                        lambda: (_ for _ in ()).throw(AssertionError("not read")))
    assert cli._host() == "coord-maintainer"


def test_the_warning_fires_once_per_process_not_per_call(monkeypatch, capsys):
    """_host() is called on many paths; a warning per call would be noise, and
    noise is how a real signal gets tuned out."""
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "bad\x00name")
    for _ in range(5):
        cli._host()
    assert capsys.readouterr().err.count("not a usable fleet id") == 1


# --- injectivity (codex 558 r1) ---------------------------------------------
#
# Collapsing alone is LOSSY: bad/name, bad name and bad-name all reduce to
# bad-name. Three distinct machines would then share one fleet key and MERGE
# their presence, lease, health and role history — strictly worse than the one
# unaddressable identity this function exists to prevent. An unreachable host is
# a hole; a merged one is corruption of two live records.

@pytest.mark.parametrize("raws", [
    ("bad/name", "bad name", "bad\\name"),
    ("a b", "a/b", "a:b\x00"),
    ("-lead", "lead-", "lead"),
    ("x\x00y", "x\x01y", "x\x02y"),
])
def test_distinct_hostnames_can_never_mint_the_same_identity(raws, monkeypatch):
    ids = set()
    for raw in raws:
        monkeypatch.setattr(cli.socket, "gethostname", lambda r=raw: r)
        monkeypatch.setattr(cli, "_hostname_rewrite_warned", False)
        try:
            ids.add(cli._host())
        except RuntimeError:
            ids.add(("REFUSED", raw))   # refusal is per-raw, never a shared key
    assert len(ids) == len(raws), (
        f"distinct hostnames collapsed to a shared fleet key: {raws} -> {ids}"
    )


def test_the_digest_is_derived_from_the_RAW_name_not_the_collapsed_one():
    """If the digest came from the collapsed stem it would be constant across
    exactly the inputs that collide, and the suffix would prevent nothing."""
    a, _ = cli._sanitize_hostname("bad/name")
    b, _ = cli._sanitize_hostname("bad name")
    assert a != b
    assert a.startswith("bad-name-") and b.startswith("bad-name-")


def test_an_unchanged_hostname_never_gains_a_suffix():
    """The no-regression property, restated against the digest: a live hostname
    must not acquire a suffix and fork its own history."""
    for h in ("Ashs-MBP-Work", "MacBookPro.localdomain", "host_1.example.com"):
        safe, rewritten = cli._sanitize_hostname(h)
        assert (safe, rewritten) == (h, False)
