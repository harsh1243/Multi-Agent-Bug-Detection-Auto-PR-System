"""Test configuration for first-party backend modules."""

import os
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = APP_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Settings are created at import time; tests never make an external LLM call.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

