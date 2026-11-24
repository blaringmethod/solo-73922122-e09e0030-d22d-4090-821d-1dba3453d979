"""Pytest bootstrap.

Ensures the repository root is importable so ``import ledger`` works under
both ``python -m pytest`` and a bare ``pytest`` invocation, regardless of the
caller's current directory.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
