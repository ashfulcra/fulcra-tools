import pytest
from fulcra_okf.bundle import Bundle, RESERVED_NAMES
from fulcra_okf.frontmatter import FrontmatterError


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_load_dir_collects_concepts_and_skips_reserved(tmp_path):
    _write(tmp_path, "index.md", '---\nokf_version: "0.1"\n---\n# Bundle\n')
    _write(tmp_path, "tables/orders.md", "---\ntype: Table\n---\nbody\n")
    _write(tmp_path, "tables/index.md", "# tables\n")
    b = Bundle.load_dir(tmp_path)
    assert set(b.concepts) == {"tables/orders"}
    assert b.okf_version == "0.1"
    assert "index.md" in RESERVED_NAMES and "log.md" in RESERVED_NAMES


def test_load_dir_strict_raises_on_bad_frontmatter(tmp_path):
    _write(tmp_path, "bad.md", "---\ntype: X\nno close\n")
    with pytest.raises(FrontmatterError):
        Bundle.load_dir(tmp_path)


def test_load_dir_lenient_records_parse_errors(tmp_path):
    _write(tmp_path, "bad.md", "---\ntype: X\nno close\n")
    _write(tmp_path, "good.md", "---\ntype: T\n---\nb\n")
    b = Bundle.load_dir(tmp_path, lenient=True)
    assert set(b.concepts) == {"good"}
    assert [rel for rel, _ in b.parse_errors] == ["bad.md"]


def test_write_dir_round_trips_concept(tmp_path):
    _write(tmp_path, "tables/orders.md", "---\ntype: Table\ntitle: Orders\n---\nbody\n")
    b = Bundle.load_dir(tmp_path)
    out = tmp_path / "out"
    b.write_dir(out)
    b2 = Bundle.load_dir(out)
    assert b2.concepts["tables/orders"].type == "Table"
    assert b2.concepts["tables/orders"].title == "Orders"


# --- Finding 2: reserved_files field populated by load_dir ---

def test_load_dir_populates_reserved_files_for_root_log(tmp_path):
    """load_dir must collect log.md text into reserved_files."""
    log_text = "# Log\n\n## 2026-06-20\n* Update\n"
    _write(tmp_path, "log.md", log_text)
    _write(tmp_path, "a.md", "---\ntype: T\n---\nbody\n")
    b = Bundle.load_dir(tmp_path)
    assert "log.md" in b.reserved_files
    assert b.reserved_files["log.md"] == log_text


def test_load_dir_populates_reserved_files_for_nested_log(tmp_path):
    """load_dir must collect subdirectory log.md files too."""
    log_text = "# Log\n\n## 2026-06-19\n* Init\n"
    _write(tmp_path, "sub/log.md", log_text)
    _write(tmp_path, "a.md", "---\ntype: T\n---\nbody\n")
    b = Bundle.load_dir(tmp_path)
    assert "sub/log.md" in b.reserved_files
    assert b.reserved_files["sub/log.md"] == log_text


def test_load_dir_reserved_files_includes_index(tmp_path):
    """index.md files should also appear in reserved_files."""
    idx_text = "---\nokf_version: \"0.1\"\n---\n# Root\n"
    _write(tmp_path, "index.md", idx_text)
    _write(tmp_path, "a.md", "---\ntype: T\n---\nbody\n")
    b = Bundle.load_dir(tmp_path)
    assert "index.md" in b.reserved_files


def test_load_dir_reserved_files_empty_when_no_reserved(tmp_path):
    """When there are no reserved files, reserved_files should be empty."""
    _write(tmp_path, "a.md", "---\ntype: T\n---\nbody\n")
    b = Bundle.load_dir(tmp_path)
    assert b.reserved_files == {}
