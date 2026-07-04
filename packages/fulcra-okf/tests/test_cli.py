import pytest
from fulcra_okf.cli import main


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_validate_returns_zero_for_conformant(tmp_path, capsys):
    _write(tmp_path, "a.md", "---\ntype: T\n---\nbody\n")
    assert main(["validate", str(tmp_path)]) == 0


def test_validate_returns_one_for_missing_type(tmp_path):
    _write(tmp_path, "a.md", "---\ntitle: NoType\n---\nbody\n")
    assert main(["validate", str(tmp_path)]) == 1


def test_validate_json_flag_emits_json(tmp_path, capsys):
    _write(tmp_path, "a.md", "---\ntype: T\n---\nbody\n")
    main(["validate", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert '"conformant"' in out


def test_info_prints_counts(tmp_path, capsys):
    _write(tmp_path, "a.md", "---\ntype: Table\n---\nb\n")
    _write(tmp_path, "b.md", "---\ntype: Table\n---\nb\n")
    assert main(["info", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "concepts: 2" in out
    assert "Table" in out


def test_fmt_check_detects_no_change_needed(tmp_path):
    _write(tmp_path, "a.md", "---\ntype: T\n---\nbody\n")
    assert main(["fmt", str(tmp_path), "--check"]) == 0


def test_fmt_check_reports_parse_errors(tmp_path, capsys):
    _write(tmp_path, "bad.md", "---\ntype: T\n")
    assert main(["fmt", str(tmp_path), "--check"]) == 1
    err = capsys.readouterr().err
    assert "error: bad.md:" in err


# ---------------------------------------------------------------------------
# I2 — fmt WRITE path + public render_concept + C1 timestamp end-to-end
# ---------------------------------------------------------------------------

def test_render_concept_is_importable():
    """render_concept must be importable from bundle (public API)."""
    from fulcra_okf.bundle import render_concept  # noqa: F401


def test_fmt_write_path_is_idempotent_and_handles_timestamp(tmp_path):
    """I2 + C1 end-to-end: fmt writes, then --check reports clean."""
    pytest.importorskip("yaml")
    # Concept whose frontmatter is not in canonical form (extra key ordering
    # that pyyaml may normalize) and contains a timestamp field.
    _write(
        tmp_path,
        "a.md",
        "---\ntitle: My Thing\ntype: T\ntimestamp: 2026-05-28T14:30:00Z\n---\nbody\n",
    )
    # fmt should write and return 0
    result = main(["fmt", str(tmp_path)])
    assert result == 0
    # Re-running with --check should return 0 (idempotent)
    result2 = main(["fmt", str(tmp_path), "--check"])
    assert result2 == 0


def test_fmt_error_returns_nonzero_without_traceback(tmp_path, capsys, monkeypatch):
    """fmt must catch unexpected render errors and return non-zero (not crash)."""
    import fulcra_okf.bundle as bmod

    _write(tmp_path, "a.md", "---\ntype: T\n---\nbody\n")

    def _explode(concept):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(bmod, "render_concept", _explode)
    # Import cli after monkeypatching bundle — cli already holds a reference
    # to render_concept; patch it there too.
    import fulcra_okf.cli as cmod
    monkeypatch.setattr(cmod, "render_concept", _explode)

    result = main(["fmt", str(tmp_path)])
    assert result == 1
    err = capsys.readouterr().err
    assert "simulated render failure" in err
