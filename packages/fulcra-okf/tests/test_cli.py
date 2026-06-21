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
