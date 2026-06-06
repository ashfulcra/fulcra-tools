"""remote.list_files must normalize the REAL ``fulcra file list`` output.

These tests pin the live contract that was missing in 0.9.0: the real CLI
formats each line as ``"<size>  <date> <tz>  <FILENAME>"`` (size + date +
filename-only), NOT a clean path. list_files used to return those raw display
lines verbatim, so every consumer that then called download_json/delete on the
"path" silently got None / no-op in LIVE — while the test fake backend emits
clean full paths, so the whole suite passed green over a broken surface.

The fix normalizes BOTH shapes (real formatted line AND the fake's clean path)
to a single clean full remote path. test_real_cli_format is the regression test
that fails on the pre-fix code; test_fake_clean_path_format guards against
double-prefixing the already-clean entries the rest of the suite relies on.
"""

import unittest
from unittest import mock

from fulcra_coord import cli, remote


def _completed(stdout: str, returncode: int = 0):
    """A stand-in subprocess.CompletedProcess carrying canned stdout."""
    cp = mock.Mock()
    cp.stdout = stdout
    cp.returncode = returncode
    return cp


class ListFilesNormalizationTests(unittest.TestCase):
    PREFIX = "/coordination/health/"

    def test_real_cli_format_is_normalized_to_full_path(self):
        """The REAL CLI emits ``size  date tz  FILENAME`` (no path). list_files
        must join that bare filename onto the prefix and return a clean full
        remote path — NOT the raw display line. Pre-fix this returns the raw
        string and the assert fails."""
        real_line = "383B    2026-06-06 01:52AM UTC  claude-code-mac-fulcra-coord.json\n"
        with mock.patch("fulcra_coord.remote.subprocess.run",
                        return_value=_completed(real_line)):
            out = remote.list_files(self.PREFIX)
        self.assertEqual(
            out,
            ["/coordination/health/claude-code-mac-fulcra-coord.json"],
        )

    def test_real_cli_multiple_lines(self):
        """Multiple formatted lines each normalize independently."""
        stdout = (
            "383B    2026-06-06 01:52AM UTC  claude-code-mac-fulcra-coord.json\n"
            "  12.0KB  2026-06-06 02:00AM UTC  claude-code-srv-fulcra-coord.json\n"
        )
        with mock.patch("fulcra_coord.remote.subprocess.run",
                        return_value=_completed(stdout)):
            out = remote.list_files(self.PREFIX)
        self.assertEqual(out, [
            "/coordination/health/claude-code-mac-fulcra-coord.json",
            "/coordination/health/claude-code-srv-fulcra-coord.json",
        ])

    def test_fake_clean_path_format_is_unchanged(self):
        """The fake backend (and any CLI that already emits a clean full path)
        must pass through with NO double-prefixing — both formats converge on
        the same clean path. This is the invariant the rest of the suite leans
        on, so it must hold after the fix too."""
        clean = "/coordination/health/claude-code-mac-fulcra-coord.json\n"
        with mock.patch("fulcra_coord.remote.subprocess.run",
                        return_value=_completed(clean)):
            out = remote.list_files(self.PREFIX)
        self.assertEqual(
            out,
            ["/coordination/health/claude-code-mac-fulcra-coord.json"],
        )

    def test_nonzero_returncode_yields_empty(self):
        """Best-effort contract preserved: a failed list returns []."""
        with mock.patch("fulcra_coord.remote.subprocess.run",
                        return_value=_completed("whatever\n", returncode=1)):
            out = remote.list_files(self.PREFIX)
        self.assertEqual(out, [])

    def test_consumer_load_health_records_against_real_format(self):
        """End-to-end: with the REAL formatted listing, _load_health_records must
        successfully download and return records. Pre-fix the formatted line is
        not a path, download_json returns None, and recs is empty in live."""
        prefix = remote.health_prefix()
        real_line = "383B    2026-06-06 01:52AM UTC  claude-code-mac-fulcra-coord.json\n"
        fname = "claude-code-mac-fulcra-coord.json"
        rec = {"host": "mac", "agent": "claude-code:mac:repo"}

        def fake_download_json(path, *, backend=None):
            # Only the correctly-normalized full path resolves; the raw display
            # line (or a double-prefixed path) would miss and return None.
            if path == prefix.rstrip("/") + "/" + fname:
                return rec
            return None

        with mock.patch("fulcra_coord.remote.subprocess.run",
                        return_value=_completed(real_line)), \
             mock.patch("fulcra_coord.cli.remote.download_json",
                        side_effect=fake_download_json):
            recs = cli._load_health_records()
        self.assertEqual(recs, [rec])


if __name__ == "__main__":
    unittest.main()
