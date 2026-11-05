"""打包可安装插件，不包含 Git 元数据、测试缓存或运行时配置。"""

import json
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
version = manifest["version"]
tag = os.environ.get("GITHUB_REF_NAME", "")
if tag.startswith("v") and tag != f"v{version}":
    raise ValueError("发布标签与插件 manifest 版本不一致")
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
    bundle.write(ROOT / "webui.json", "webui.json")
    for directory in ("vendor", "webui"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                bundle.write(path, path.relative_to(ROOT))
print(target)
