"""env_float / env_int — the shared "explicit override > env > default, garbage
falls back to default" knob readers that consolidated ~10 copy-pasted readers
across views/cli/remote. These pin the contract every migrated caller relies on.
"""

import unittest
from unittest import mock

from fulcra_coord import env_float, env_int


class EnvFloatTests(unittest.TestCase):
    KEY = "FULCRA_COORD_TEST_FLOAT_KNOB"

    def test_override_wins_over_env_and_default(self):
        with mock.patch.dict("os.environ", {self.KEY: "7"}):
            self.assertEqual(env_float(self.KEY, 1.0, override=3.5), 3.5)

    def test_override_zero_is_honored_not_treated_as_absent(self):
        # 0.0 is a legitimate override; the guard is `is not None`, not truthiness.
        self.assertEqual(env_float(self.KEY, 9.0, override=0.0), 0.0)

    def test_override_is_coerced_to_float(self):
        # Matches the readers that did `float(arg)`; an int override normalizes.
        self.assertEqual(env_float(self.KEY, 1.0, override=4), 4.0)
        self.assertIsInstance(env_float(self.KEY, 1.0, override=4), float)

    def test_env_used_when_no_override(self):
        with mock.patch.dict("os.environ", {self.KEY: "2.5"}):
            self.assertEqual(env_float(self.KEY, 1.0), 2.5)

    def test_blank_and_nonnumeric_env_fall_back_to_default(self):
        for bad in ("", "   ", "abc", "1.2.3"):
            with mock.patch.dict("os.environ", {self.KEY: bad}):
                self.assertEqual(env_float(self.KEY, 1.0), 1.0)

    def test_absent_env_uses_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(env_float(self.KEY, 8.0), 8.0)


class EnvIntTests(unittest.TestCase):
    KEY = "FULCRA_COORD_TEST_INT_KNOB"

    def test_env_used_when_numeric(self):
        with mock.patch.dict("os.environ", {self.KEY: "42"}):
            self.assertEqual(env_int(self.KEY, 5), 42)

    def test_noninteger_env_falls_back_to_default(self):
        # int(raw) — a float-looking string is NOT truncated, it falls back.
        # A caller wanting truncation must compose int(env_float(...)).
        with mock.patch.dict("os.environ", {self.KEY: "2.9"}):
            self.assertEqual(env_int(self.KEY, 5), 5)

    def test_blank_and_garbage_fall_back(self):
        for bad in ("", "   ", "xyz"):
            with mock.patch.dict("os.environ", {self.KEY: bad}):
                self.assertEqual(env_int(self.KEY, 5), 5)

    def test_override_wins_and_is_coerced(self):
        with mock.patch.dict("os.environ", {self.KEY: "1"}):
            self.assertEqual(env_int(self.KEY, 5, override=9), 9)


if __name__ == "__main__":
    unittest.main()
