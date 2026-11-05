import sys
from pathlib import Path

# The agent modules import each other as top-level modules (e.g. "from tools import ...").
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agenticops"))