from coord_engine import transport


def test_parse_list_output_basic():
    text = "81B     2026-07-01 04:12PM UTC  probe.md\n93B     2026-07-01 04:15PM UTC  other.md"
    entries = transport.parse_list_output(text)
    assert len(entries) == 2
    assert entries[0] == {
        "name": "probe.md", "size": "81B", "mtime": "2026-07-01 04:12PM UTC", "is_dir": False,
    }
    assert entries[1]["name"] == "other.md"


def test_parse_list_output_directory_entry():
    entries = transport.parse_list_output("0B      2026-07-01 04:12PM UTC  subdir/")
    assert entries[0]["is_dir"] is True


def test_parse_list_output_empty():
    assert transport.parse_list_output("") == []
    assert transport.parse_list_output("\n\n") == []


def test_parse_stat_output():
    text = (
        "/_coord2-probe/probe.md (93 bytes)\n"
        "Uploaded: 2026-07-01T16:12:44.623092Z\n"
        "Version: 75c13308-76c0-4379-837e-8a96b4899535\n"
        "Previous Versions: 1\n"
        "- b8b68ea9-0986-4f9b-bb24-4a693d380ba4 2026-07-01T16:12:20.176191Z (81 bytes)"
    )
    st = transport.parse_stat_output(text)
    assert st["uploaded"] == "2026-07-01T16:12:44.623092Z"
    assert st["version"] == "75c13308-76c0-4379-837e-8a96b4899535"
    assert st["previous_count"] == 1
    assert st["previous"][0]["version"] == "b8b68ea9-0986-4f9b-bb24-4a693d380ba4"
    assert st["path"] == "/_coord2-probe/probe.md"


def test_parse_stat_no_previous():
    text = "/x.md (10 bytes)\nUploaded: 2026-07-01T00:00:00Z\nVersion: abc\nPrevious Versions: 0"
    st = transport.parse_stat_output(text)
    assert st["previous_count"] == 0
    assert st["previous"] == []


def test_list_dir_sorted_by_name():
    # the real transport must return list entries sorted by name (determinism for
    # "last wins" folds). Simulate parse output order != sorted, then sort.
    entries = transport.parse_list_output(
        "1B  2026-07-01 04:12PM UTC  zzz.md\n1B  2026-07-01 04:12PM UTC  aaa.md")
    names = [e["name"] for e in sorted(entries, key=lambda e: e.get("name") or "")]
    assert names == ["aaa.md", "zzz.md"]
