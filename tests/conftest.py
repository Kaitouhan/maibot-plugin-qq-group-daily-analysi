"""模拟 Runner 的隔离包加载，避免污染麦麦宿主的 src 命名空间。"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "daily_plugin", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["daily_plugin"] = module
spec.loader.exec_module(module)
