"""Runs the Node tests of the job page's pending-state helpers (app/tests/js/pending_helpers.test.mjs), which read
app/src/frontend/src/pending.ts as it is. Skipped where Node is not installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
NODE_TESTS = APP / "tests" / "js" / "pending_helpers.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_job_page_pending_helpers_pass_their_node_tests():
    result = subprocess.run(["node", "--test", str(NODE_TESTS)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_a_page_never_goes_white_pass_their_node_tests():
    tests = APP / "tests" / "js" / "page_safety.test.mjs"
    result = subprocess.run(["node", "--test", str(tests)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_bars_write_their_counts_pass_their_node_tests():
    tests = APP / "tests" / "js" / "bars.test.mjs"
    result = subprocess.run(["node", "--test", str(tests)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_a_new_jobs_default_name_pass_their_node_tests():
    tests = APP / "tests" / "js" / "job_name.test.mjs"
    result = subprocess.run(["node", "--test", str(tests)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
