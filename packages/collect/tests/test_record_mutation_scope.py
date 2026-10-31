"""Source writes and Undo on one host must share the same process lock."""
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("same_account", [False, True])
def test_record_mutation_lease_serializes_processes_by_account(collect_home, same_account):
    from fulcra_collect.config_leases import record_mutation_scope
    account = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    other = account if same_account else "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
    code = """import sys
from fulcra_collect.config_leases import record_mutation_scope
try:
 with record_mutation_scope(sys.argv[1], timeout_s=0.1):
  pass
except TimeoutError:
 sys.exit(7)
"""
    with record_mutation_scope(account):
        result = subprocess.run([sys.executable, "-c", code, other], env=os.environ.copy(),
                                timeout=5, capture_output=True, text=True)
    assert result.returncode == (7 if same_account else 0), result.stderr
    with record_mutation_scope(account, timeout_s=0.1):
        pass


def test_record_mutation_lease_can_be_reentered_by_its_thread_only(collect_home):
    from concurrent.futures import ThreadPoolExecutor
    from fulcra_collect.config_leases import record_mutation_scope
    account = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    def other_thread():
        with pytest.raises(TimeoutError):
            with record_mutation_scope(account, timeout_s=0.1):
                pytest.fail("another thread must not reuse the held lease")
    with record_mutation_scope(account):
        with record_mutation_scope(account, timeout_s=0.1):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(other_thread).result(timeout=3)
