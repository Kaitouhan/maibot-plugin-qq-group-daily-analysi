"""打包可安装插件，不包含 Git 元数据、测试缓存或运行时配置。"""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
version = manifest["version"]
target = ROOT / "dist" / f"maibot-qq-group-daily-analysis-{version}.zip"
target.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
    for path in sorted(ROOT.glob("*.py")):
        bundle.write(path, path.name)
    for name in (
        "_manifest.json",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "NOTICE.md",
        "CHANGELOG.md",
        "upstream.lock.json",
    ):
        bundle.write(ROOT / name, name)
    for path in sorted((ROOT / "vendor").glob("*")):
        if path.is_file():
            bundle.write(path, path.relative_to(ROOT))
print(target)
