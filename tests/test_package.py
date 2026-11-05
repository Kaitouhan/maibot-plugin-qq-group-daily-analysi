"""发布 ZIP 必须可独立加载，包含完整模板与页面而不夹带运行数据。"""

import json
import subprocess
import sys
import zipfile
from pathlib import Path


def test_install_archive_is_complete_and_isolated(tmp_path):
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / "scripts/package.py")], cwd=root, check=True, capture_output=True)
    version = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))["version"]
    with zipfile.ZipFile(root / "dist" / f"maibot-qq-group-daily-analysis-{version}.zip") as bundle:
        names = bundle.namelist()
        assert "webui/index.html" in names and "webui.json" in names
        assert "vendor/upstream/src/infrastructure/reporting/typst_worker.py" in names
        assert len([name for name in names if name.endswith("/image_template.html")]) >= 11
        assert not any("__pycache__" in name or name.endswith((".sqlite3", ".db", ".pyc")) for name in names)
        assert "config.toml" not in names
        bundle.extractall(tmp_path)
    loader = """
import importlib.util,sys
from pathlib import Path
root=Path(sys.argv[1]);spec=importlib.util.spec_from_file_location('installed_daily',root/'plugin.py',submodule_search_locations=[str(root)])
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
assert module.create_plugin().get_components()
assert not any(key.startswith('astrbot') for key in sys.modules)
"""
    subprocess.run([sys.executable, "-I", "-c", loader, str(tmp_path)], cwd=tmp_path, check=True, capture_output=True)
