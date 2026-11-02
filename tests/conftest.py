import sys
from pathlib import Path

# Make the source modules (state_store.py, ct_engine.py, ...) importable in tests: they are plain
# modules under src/lakefed_ingest, imported by file name (as the notebooks do via sys.path).
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "lakefed_ingest"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _new_run_of_sink_checks():
    """ct_engine remembers the sinks it checked during a run; every test is a new run."""
    import ct_engine
    ct_engine.forget_sinks()
    yield
