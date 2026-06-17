"""The packaged skill must have valid frontmatter and intact references."""
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1] / "skill"
SKILL_MD = SKILL_DIR / "SKILL.md"


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL.md must open with a frontmatter block"
    end = text.index("\n---\n", 4)
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip()
    return out


def test_skill_frontmatter_is_valid():
    fm = _frontmatter(SKILL_MD.read_text())
    assert fm["name"] == "fulcra-vault"
    assert fm["description"].strip('"').strip()
    assert fm["user-invocable"] == "true"
    assert fm["homepage"]
    assert fm["license"]


def test_skill_references_exist():
    for ref in ("fulcra-vault-write.md", "fulcra-vault-tier2-http.md"):
        path = SKILL_DIR / "references" / ref
        assert path.is_file(), f"missing reference: {ref}"
        assert path.read_text().strip(), f"empty reference: {ref}"


def test_skill_body_links_its_references():
    body = SKILL_MD.read_text()
    assert "references/fulcra-vault-write.md" in body
    assert "references/fulcra-vault-tier2-http.md" in body
