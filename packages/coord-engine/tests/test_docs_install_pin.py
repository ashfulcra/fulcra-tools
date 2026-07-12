"""Contract test: every coord-engine install pin in the docs tracks the release.

The showcase repo is sent COLD to founders' agents; a front-door install pin
lagging the engine (v1.5.0 pinned while the code was 1.6.x) sends a founder into
an engine missing this week's fixes and they evaluate stale behavior. This test
makes that class of drift impossible to merge.

Source of truth: ``coord_engine.__version__``. The release discipline
(packages/coord-engine/README.md → "Releasing") REQUIRES bumping ``__version__``
to ``X.Y.Z`` in the same commit that cuts the ``coord-engine-vX.Y.Z`` tag, so the
version string is the canonical current release AND is available without git tags
(CI shallow clones have no tags). Every primary-install pin of the form
``@coord-engine-vX.Y.Z#subdirectory=packages/coord-engine`` must equal it.

The regex matches ONLY real git-install pins, so prose mentions of past versions
(the README's "v1.5.0 shipped stale" note) are never falsely caught.

Cheap-beats-clever: grep the tracked docs, compare strings.
"""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]            # packages/coord-engine
REPO = PKG.parents[1]                                # repo root

#: the canonical current release (kept in lockstep with the git tag by policy)
VERSION = re.search(
    r'__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
    (PKG / "coord_engine" / "__init__.py").read_text(encoding="utf-8"),
).group(1)

#: an actual `uv tool install "git+…@coord-engine-vX.Y.Z#subdirectory=…"` pin,
#: never a prose version mention
PIN_RE = re.compile(
    r"@coord-engine-v([0-9]+\.[0-9]+\.[0-9]+)#subdirectory=packages/coord-engine"
)

#: every doc that carries a primary install path (add new ones here)
PINNED_DOCS = (
    "README.md",
    "packages/coord-engine/README.md",
    "docs/coord/GET-ON-THE-BUS.md",
    "skills/fulcra-agent-atc/SKILL.md",
)


def _all_pins():
    """(relpath, lineno, pinned_version) for every install pin under the repo,
    so a NEW doc that pins a version is covered without editing this test."""
    hits = []
    for md in REPO.rglob("*.md"):
        rel = md.relative_to(REPO).as_posix()
        if rel.startswith(".superpowers/") or "/.git/" in f"/{rel}":
            continue
        for i, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
            for m in PIN_RE.finditer(line):
                hits.append((rel, i, m.group(1)))
    return hits


def test_every_install_pin_matches_release():
    """No coord-engine install pin may diverge from ``__version__``."""
    stale = [(rel, ln, v) for rel, ln, v in _all_pins() if v != VERSION]
    assert not stale, (
        f"coord-engine install pin(s) stale vs __version__={VERSION}: "
        + ", ".join(f"{rel}:{ln} pins v{v}" for rel, ln, v in stale)
        + " — update the pin(s) to the current release."
    )


def test_expected_docs_carry_a_pin():
    """The known primary-install docs must each still carry a pin — so a pin
    silently deleted (and thus never drift-checked) is caught too."""
    pinned = {rel for rel, _, _ in _all_pins()}
    missing = [d for d in PINNED_DOCS if d not in pinned]
    assert not missing, (
        "expected coord-engine install pin missing from: "
        + ", ".join(missing)
    )
