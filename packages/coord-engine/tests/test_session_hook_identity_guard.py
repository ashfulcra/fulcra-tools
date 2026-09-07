"""The SessionStart hook must not turn a store document into shell source.

codex-reviewer P1 on PR 706. `.claude/hooks/session-start.sh` resolves the coord
team from `/coord-bootstrap.json` when the environment does not name one, and
that value goes to two dangerous places:

  1. interpolated into a STORE PATH (`team/<team>/_coord/...`), where `../..`
     or an embedded slash reaches a document nobody intended; and
  2. written into `$CLAUDE_ENV_FILE`, which the session SOURCES — so an
     unquoted `export NAME=${value}` carrying whitespace, a newline, `;` or
     `$(...)` does not set a variable, it adds a statement to the shell program.

The document is mutable and any agent with write access can change it. That the
current one was written by this team is a fact about today, not a property of
the input. So the hook validates against a closed grammar AND emits quoted
assignments; these tests drive the grammar with values that would be hostile if
it were absent.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
HOOK = REPO / ".claude" / "hooks" / "session-start.sh"


def _valid_identity_fn() -> str:
    """The real `valid_identity` definition, lifted from the hook."""
    text = HOOK.read_text()
    start = text.index("valid_identity() {")
    # Close on a `}` at the START OF A LINE. My first cut used the next `}`
    # anywhere, which matched the one inside `${1:-}` and returned a truncated
    # function that refused every legitimate identity — a test failure that
    # looked exactly like a real defect in the hook.
    rest = text[start:].splitlines(keepends=True)
    out = []
    for line in rest:
        out.append(line)
        if line.rstrip("\n") == "}":
            break
    else:
        raise AssertionError("valid_identity() has no closing brace on its own line")
    return "".join(out)


def _accepts(value: str) -> bool:
    script = (
        f'{_valid_identity_fn()}\n'
        'if valid_identity "$1"; then echo YES; else echo NO; fi\n')
    proc = subprocess.run(["bash", "-c", script, "_", value],
                          capture_output=True, text=True)
    return proc.stdout.strip() == "YES"


@pytest.mark.parametrize("value", ["fulcra", "acme", "team-two", "a", "x_1.2-3"])
def test_ordinary_identities_are_accepted(value):
    assert _accepts(value), f"a legitimate identity was refused: {value!r}"


@pytest.mark.parametrize(
    "value",
    [
        "../../etc",              # path traversal into the store
        "a/b",                    # a slash reaches another document
        "fulcra; rm -rf /",       # statement separator
        "$(id)",                  # command substitution
        "`id`",                   # legacy command substitution
        "a b",                    # whitespace splits the assignment
        "a\nexport EVIL=1",       # a newline appends a whole statement
        "'; export EVIL=1; '",    # escapes single quoting
        '"',                      # a bare quote
        "",                       # empty
        " ",                      # whitespace only
        "-startswithdash",        # must begin with an alphanumeric
        "x" * 65,                 # over the length bound
    ],
)
def test_hostile_values_are_refused(value):
    assert not _accepts(value), (
        f"a hostile identity was ACCEPTED and would reach a store path and "
        f"shell source: {value!r}")


def test_the_hook_emits_quoted_assignments_not_bare_interpolation():
    """Belt and braces: the grammar makes quoting redundant and the quoting
    makes a future grammar mistake harmless. Neither alone is worth relying on
    for something that becomes shell source, so assert the quoting is there."""
    text = HOOK.read_text()
    assert "export FULCRA_COORD_TEAM=${team}" not in text, (
        "unquoted interpolation into $CLAUDE_ENV_FILE has come back")
    assert "printf \"export FULCRA_COORD_TEAM='%s'\\n\"" in text
    assert "printf \"export FULCRA_COORD_COORDINATOR='%s'\\n\"" in text


def test_every_write_to_the_env_file_is_gated_on_validation():
    """A validated value that is then written by an ungated line is not
    validated. Every append to CLAUDE_ENV_FILE must sit behind the check."""
    for line in HOOK.read_text().splitlines():
        if "CLAUDE_ENV_FILE" in line and ">>" in line:
            assert "valid_identity" in line, (
                f"an append to CLAUDE_ENV_FILE is not gated on validation: {line.strip()}")
