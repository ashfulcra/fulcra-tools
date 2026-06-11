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
    """A stand-in subprocess.CompletedProcess carrying canned stdout.

    ``stderr`` must be a real string, not a Mock attribute: a real
    CompletedProcess always carries one, and the store's transient-failure
    classifier regex-matches it on every non-zero exit."""
    cp = mock.Mock()
    cp.stdout = stdout
    cp.stderr = ""
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


class ListJsonTests(unittest.TestCase):
    """remote.list_json = list_files + parallel download_json, order-preserving,
    dict-guarded, best-effort. It is the shared primitive behind presence/health
    load+prune and the archive cold-index."""

    PREFIX = "/coordination/health/"

    def test_returns_path_record_pairs_in_list_order(self):
        paths = [
            "/coordination/health/a.json",
            "/coordination/health/b.json",
            "/coordination/health/c.json",
        ]
        records = {
            "/coordination/health/a.json": {"host": "a"},
            "/coordination/health/b.json": {"host": "b"},
            "/coordination/health/c.json": {"host": "c"},
        }
        with mock.patch("fulcra_coord.remote.list_files", return_value=paths), \
             mock.patch("fulcra_coord.remote.download_json",
                        side_effect=lambda p, *, backend=None: records.get(p)):
            out = remote.list_json(self.PREFIX)
        # Order MUST mirror list_files order despite concurrent completion.
        self.assertEqual(
            out,
            [(p, records[p]) for p in paths],
        )

    def test_order_is_list_order_not_completion_order(self):
        """The load-bearing claim: results follow list_files order even when a
        LATER-listed path's download completes FIRST. Make the earlier paths sleep
        so, under the thread pool, completion order is the REVERSE of list order;
        the output must still be list order (the synchronous-mock test above can't
        distinguish the two)."""
        import time
        paths = [
            "/coordination/health/a.json",
            "/coordination/health/b.json",
            "/coordination/health/c.json",
        ]
        # a sleeps longest, c returns immediately → completion order c, b, a.
        delays = {paths[0]: 0.06, paths[1]: 0.03, paths[2]: 0.0}

        def dl(p, *, backend=None):
            time.sleep(delays[p])
            return {"host": p[-6]}  # 'a'/'b'/'c' marker char

        with mock.patch("fulcra_coord.remote.list_files", return_value=paths), \
             mock.patch("fulcra_coord.remote.download_json", side_effect=dl):
            out = remote.list_json(self.PREFIX)
        self.assertEqual([p for p, _ in out], paths)

    def test_non_json_paths_are_skipped(self):
        paths = ["/coordination/health/a.json", "/coordination/health/notes.txt"]
        with mock.patch("fulcra_coord.remote.list_files", return_value=paths), \
             mock.patch("fulcra_coord.remote.download_json",
                        side_effect=lambda p, *, backend=None: {"p": p}):
            out = remote.list_json(self.PREFIX)
        self.assertEqual(out, [("/coordination/health/a.json", {"p": "/coordination/health/a.json"})])

    def test_none_and_non_dict_records_are_dropped(self):
        paths = [
            "/coordination/health/ok.json",
            "/coordination/health/missing.json",  # download → None
            "/coordination/health/list.json",     # download → a list (non-dict)
        ]

        def dl(p, *, backend=None):
            if p.endswith("ok.json"):
                return {"host": "ok"}
            if p.endswith("list.json"):
                return ["not", "a", "dict"]
            return None

        with mock.patch("fulcra_coord.remote.list_files", return_value=paths), \
             mock.patch("fulcra_coord.remote.download_json", side_effect=dl):
            out = remote.list_json(self.PREFIX)
        self.assertEqual(out, [("/coordination/health/ok.json", {"host": "ok"})])

    def test_failed_listing_yields_empty(self):
        with mock.patch("fulcra_coord.remote.list_files", return_value=[]):
            self.assertEqual(remote.list_json(self.PREFIX), [])

    def test_download_raise_is_isolated_not_fatal(self):
        paths = ["/coordination/health/boom.json", "/coordination/health/ok.json"]

        def dl(p, *, backend=None):
            if p.endswith("boom.json"):
                raise RuntimeError("network blew up")
            return {"host": "ok"}

        with mock.patch("fulcra_coord.remote.list_files", return_value=paths), \
             mock.patch("fulcra_coord.remote.download_json", side_effect=dl):
            out = remote.list_json(self.PREFIX)
        # The raising item is dropped; the healthy one survives. Never propagates.
        self.assertEqual(out, [("/coordination/health/ok.json", {"host": "ok"})])


if __name__ == "__main__":
    unittest.main()
