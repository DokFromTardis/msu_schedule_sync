import os
import sys


def _project_root() -> str:
    here = os.path.dirname(__file__)
    return os.path.abspath(os.path.join(here, os.pardir))


# Ensure project root is importable (so `import serve` works in tests)
root = _project_root()
if root not in sys.path:
    sys.path.insert(0, root)
