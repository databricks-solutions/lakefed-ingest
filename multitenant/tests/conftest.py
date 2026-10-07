import sys
from pathlib import Path

# Make the multitenant source modules (state_store.py) importable in tests.
# The repo's pytest.ini sets pythonpath=src for the production package; the multitenant
# spike keeps its source under multitenant/src/multitenant, added here.
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "multitenant"))
