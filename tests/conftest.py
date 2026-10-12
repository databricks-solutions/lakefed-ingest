import sys
from pathlib import Path

# Make the source modules (state_store.py, ct_engine.py, ...) importable in tests: they are plain
# modules under src/lakefed_ingest, imported by file name (as the notebooks do via sys.path).
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "lakefed_ingest"))
