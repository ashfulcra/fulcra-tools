"""The operator-facing setup surfaces must agree on the OAuth client shape.

Three places tell an operator how to create the OAuth client: the wizard's
Cloud-Console clickpath (`collect_plugin._CLOUD_CONSOLE_CLICKPATH`), the
package README's "Auth setup (operator)" section, and the repo-root AGENTS.md
`packages/gmail` entry. They drifted once — the wizard said Desktop app while
the README still said Internal/Web and told operators to register a redirect URI
— which produces the WRONG client type (Internal excludes personal @gmail.com,
Web makes the secret non-shippable, and Desktop clients have no redirect field
to register at all). These assertions keep the three surfaces honest.
"""
from __future__ import annotations

from pathlib import Path

from fulcra_gmail.collect_plugin import _CLOUD_CONSOLE_CLICKPATH

_README = Path(__file__).resolve().parents[1] / "README.md"
#: repo root = .../packages/gmail/tests -> up 3
_AGENTS = Path(__file__).resolve().parents[3] / "AGENTS.md"


def _operator_surfaces() -> dict[str, str]:
    surfaces = {"wizard clickpath": _CLOUD_CONSOLE_CLICKPATH}
    if _README.exists():
        surfaces["packages/gmail/README.md"] = _README.read_text()
    if _AGENTS.exists():
        surfaces["AGENTS.md"] = _AGENTS.read_text()
    return surfaces


def test_operator_surfaces_say_desktop_and_external():
    for name, text in _operator_surfaces().items():
        low = text.lower()
        assert "desktop app" in low, f"{name} must specify the Desktop-app client type"
        assert "external" in low, f"{name} must specify the External user type"


def test_operator_surfaces_do_not_instruct_web_or_internal_client():
    # Guard the INSTRUCTION forms, not contrastive prose: saying "Desktop app
    # (not Web application)" is helpful, but "Application type: Web application"
    # or "Internal / Web" would send the operator to the wrong client.
    for name, text in _operator_surfaces().items():
        low = text.lower()
        assert "application type: web application" not in low, (
            f"{name} instructs Application type: Web application — the relay uses "
            f"a Desktop app client"
        )
        assert "internal / web" not in low and "internal/web" not in low, (
            f"{name} names an Internal / Web client — External + Desktop is "
            f"required (External so personal @gmail.com accounts can authorize)"
        )
        assert "user type: internal" not in low, (
            f"{name} instructs User Type: Internal — External is required"
        )


def test_operator_surfaces_do_not_instruct_redirect_registration():
    # Desktop clients have no redirect-URI field; telling an operator to register
    # one sends them back to a Web client.
    for name, text in _operator_surfaces().items():
        low = text.lower()
        assert "authorized redirect uri" not in low, (
            f"{name} tells the operator to register a redirect URI — Desktop "
            f"clients auto-allow the 127.0.0.1 loopback, there is no field"
        )
