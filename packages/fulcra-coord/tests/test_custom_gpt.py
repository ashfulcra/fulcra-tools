"""Tests for adapters/chatgpt/custom-gpt/ — the turnkey hosted Custom GPT config.

The custom-gpt directory is the copy-paste bundle an operator imports into the
ChatGPT GPT builder: system instructions, the facade OpenAPI schema (with a
{{FACADE_BASE_URL}} placeholder for the tunnel URL), and a setup runbook. These
are config artifacts, not code, so the test surface is intentionally light:

  * all three files exist (a missing file means a broken copy-paste flow), and
  * openapi.yaml is valid YAML that parses cleanly (a malformed schema would be
    rejected by the GPT builder at import time — catch it in CI instead).

yaml is a dev-only dependency (see pyproject `dev` extra); the core package
stays stdlib-only, so this test only runs under `pytest --extra dev`.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

CUSTOM_GPT_DIR = (
    Path(__file__).resolve().parents[1] / "adapters" / "chatgpt" / "custom-gpt"
)


class CustomGptConfigTest(unittest.TestCase):
    def test_three_files_exist(self):
        for name in ("INSTRUCTIONS.md", "openapi.yaml", "SETUP.md"):
            self.assertTrue((CUSTOM_GPT_DIR / name).is_file(),
                            f"missing custom-gpt/{name}")

    def test_openapi_parses(self):
        spec = yaml.safe_load((CUSTOM_GPT_DIR / "openapi.yaml").read_text())
        self.assertIsInstance(spec, dict)
        # Sanity: it's an OpenAPI doc exposing the two facade operations.
        self.assertIn("openapi", spec)
        op_ids = {
            op.get("operationId")
            for path in spec.get("paths", {}).values()
            for op in path.values()
            if isinstance(op, dict)
        }
        self.assertIn("reportMilestone", op_ids)
        self.assertIn("getCoordinationStatus", op_ids)

    def test_openapi_carries_facade_base_url_placeholder(self):
        # The server URL must be the placeholder operators replace with their
        # tunnel URL — not a baked-in host.
        text = (CUSTOM_GPT_DIR / "openapi.yaml").read_text()
        self.assertIn("{{FACADE_BASE_URL}}", text)

    def test_setup_references_bearer_token(self):
        text = (CUSTOM_GPT_DIR / "SETUP.md").read_text()
        self.assertIn("FULCRA_COORD_FACADE_TOKEN", text)
        self.assertIn("Bearer", text)


if __name__ == "__main__":
    unittest.main()
