"""The Chosen pickers' server row (src/frontend/src/pages/pickState.ts): ticked when the whole server is chosen under a
filter that covers the databases the page shows, in part when anything else is chosen, empty when nothing is. The
function runs in node (type stripping), so the test is skipped where node is not installed."""
import json
import pathlib
import shutil
import subprocess

import pytest

PICK_STATE = pathlib.Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages" / "pickState.ts"

# (whole {on, filter}, the page's filter, anything else chosen, the tick)
CASES = [
    ({"on": False, "filter": ""}, "", False, "none"),
    ({"on": False, "filter": ""}, "eng", True, "part"),
    ({"on": True, "filter": ""}, "", False, "all"),
    ({"on": True, "filter": ""}, "eng_0", False, "all"),
    ({"on": True, "filter": "eng"}, "", False, "part"),
    ({"on": True, "filter": "eng"}, "eng_0", False, "all"),
    ({"on": True, "filter": "eng_0"}, "eng", False, "part"),
    ({"on": True, "filter": "ENG"}, "eng_00", False, "all"),
    ({"on": True, "filter": "eng_0"}, "other", False, "part"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the picker's function")
def test_the_server_row_is_all_part_or_none_by_what_the_whole_server_covers():
    script = (f"import({json.dumps(PICK_STATE.as_uri())}).then((m) => {{"
              "const cases = JSON.parse(process.argv[1]);"
              "console.log(JSON.stringify(cases.map(([whole, page, anything]) => m.serverRowPick(whole, page, anything))));"
              "});")
    cases = [[whole, page, anything] for whole, page, anything, _ in CASES]
    out = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", script, json.dumps(cases)],
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [tick for *_, tick in CASES]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the picker's functions")
def test_the_pickers_helpers_pass_their_node_tests():
    tests = pathlib.Path(__file__).resolve().parent / "js" / "pick_state.test.mjs"
    result = subprocess.run(["node", "--test", str(tests)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
