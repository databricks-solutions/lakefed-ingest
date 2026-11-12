"""Runs the Node tests of the Run now and Retry now confirmation text (app/tests/js/run_dialog.test.mjs), which read
app/src/frontend/src/runDialog.ts as it is. Skipped where Node is not installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

NODE_TESTS = Path(__file__).resolve().parents[1] / "tests" / "js" / "run_dialog.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_run_dialog_text_passes_its_node_tests():
    result = subprocess.run(["node", "--test", str(NODE_TESTS)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
