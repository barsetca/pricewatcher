import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "deploy" / "verify-m4-2.sh"


def run_mode(mode: str):
    environment = os.environ | {"VERIFY_M4_2_RESULT_TEST": mode}
    return subprocess.run(["bash", str(SCRIPT)], text=True, capture_output=True, env=environment, check=False)


def test_verification_script_result_statuses_are_deterministic():
    passed = run_mode("pass")
    assert passed.returncode == 0 and "all required checks PASS" in passed.stdout
    not_verified = run_mode("unverified")
    assert not_verified.returncode == 2 and "NOT VERIFIED" in not_verified.stdout
    failed = run_mode("fail")
    assert failed.returncode == 1 and "FAIL" in failed.stdout


def test_connection_refused_without_counter_increment_is_not_firewall_pass():
    result = run_mode("refused-no-counter")
    assert result.returncode == 1
    assert "connection-refused" in result.stdout
    assert "counter не изменился" in result.stdout
