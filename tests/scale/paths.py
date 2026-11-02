"""Puts the directories the scale harness needs on sys.path: the discovery and ingestion modules (src/lakefed_ingest),
the synthetic server (tests/synthetic) and the operations app's modules (app/src). Every module in this directory
imports it first."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

for relative in ("src/lakefed_ingest", "tests/synthetic", "app/src"):
    entry = str(ROOT / relative)
    if entry not in sys.path:
        sys.path.insert(0, entry)
