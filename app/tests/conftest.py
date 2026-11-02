import pytest
"""Put app/src on sys.path so the app modules import without a database."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture(autouse=True)
def _no_shared_reads_between_tests():
    """The app shares monitoring reads for a few seconds (main._shared); one test's answers must not
    reach the next."""
    import sys
    main = sys.modules.get("main")
    if main is not None:
        main._shared_reads.clear()
    yield
    if sys.modules.get("main") is not None:
        sys.modules["main"]._shared_reads.clear()


@pytest.fixture(autouse=True)
def _run_poller_table_missing(monkeypatch):
    """The run poller's table is missing unless a test says otherwise, so the readers answer from the Jobs API (as
    before the poller) and no test reaches Lakebase through the poller."""
    import run_poller
    monkeypatch.setattr(run_poller, "table_ready", lambda: False)


@pytest.fixture(autouse=True)
def _no_workspace(monkeypatch):
    """No unit test reaches a Databricks workspace: the workspace client refuses, so a call a test forgot to stub fails
    at once instead of authenticating against a real workspace. A test that needs one stubs its own call."""
    import sys
    import lakebase

    def refuse():
        raise RuntimeError("unit tests never reach a Databricks workspace: stub the call this test makes")
    real = lakebase.workspace
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "workspace", None) is real:
            monkeypatch.setattr(module, "workspace", refuse)
