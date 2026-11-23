"""把 tests 目录加入 sys.path，便于 `from helpers import ...`。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
