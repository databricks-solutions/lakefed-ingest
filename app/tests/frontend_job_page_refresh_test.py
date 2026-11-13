"""Runs the Node tests of the job page's refresh (app/tests/js/job_page_refresh.test.mjs), which read
app/src/frontend/src/jobPageRefresh.ts as it is, with a real QueryClient and a fake clock. Skipped where Node or the
frontend's packages (npm ci in app/src) are not installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
NODE_TESTS = APP / "tests" / "js" / "job_page_refresh.test.mjs"
QUERY_CORE = APP / "src" / "node_modules" / "@tanstack" / "query-core"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
@pytest.mark.skipif(not QUERY_CORE.exists(), reason="The frontend's packages are not installed (npm ci in app/src)")
def test_the_job_page_refresh_passes_its_node_tests():
    result = subprocess.run(["node", "--test", str(NODE_TESTS)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
