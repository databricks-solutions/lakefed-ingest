"""Put app/src on sys.path so the app's pure modules import without Streamlit or a database."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
