"""只同步明确允许的上游材料，不覆盖麦麦适配层或用户运行配置。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vendor_engine import patch_source

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "SXP-Simon/astrbot_plugin_qq_group_daily_analysis"


def sync(archive: bytes, commit: str, root: Path = ROOT, asset_manifest: bytes | None = None) -> None:
    """验证来源提交并仅提取固定文件，不解压未知路径或覆盖适配层。

    Args:
        archive: 固定提交的 codeload 压缩包。
        commit: 已查询的上游提交 SHA。
        root: 本插件仓库根目录。

    Raises:
        ValueError: 提交标识、统一消息契约或提示词变量发生不兼容变动。
    """
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        if bundle.comment.decode() != commit:
            raise ValueError("上游压缩包提交标识不匹配")
        prefix = bundle.namelist()[0]
        message = bundle.read(prefix + "src/domain/value_objects/unified_message.py")
        ast.parse(message)
        schema = json.loads(bundle.read(prefix + "_conf_schema.json"))
        prompts = {
            key: item["default"]
            for section in schema["prompts"]["items"].values()
            for key, item in section["items"].items()
        }
        for key, variable in {
            "topic_prompt": "${messages_text}",
            "user_title_prompt": "${users_text}",
            "golden_quote_v2_prompt": "${messages_text}",
            "quality_v2_prompt": "${messages_text}",
            "quality_summary_prompt": "${reviews_text}",
        }.items():
            if variable not in prompts[key]:
                raise ValueError(f"上游提示词变量不兼容：{key}")
        license_text = bundle.read(prefix + "LICENSE")
        if b"MIT License" not in license_text:
            raise ValueError("上游许可发生变化，请人工检查")
        files = {}
        for member in bundle.infolist():
            name = member.filename.removeprefix(prefix)
            parts = PurePosixPath(name).parts
            if not parts or ".." in parts or "\\" in name or name.startswith("/") or member.is_dir():
                continue
            if name.startswith("src/") or name in {"_conf_schema.json", "metadata.yaml", "LICENSE"}:
                if member.file_size > 8 * 1024 * 1024:
                    raise ValueError("上游文件超过同步预算")
                files[name] = bundle.read(member)
        files = patch_source(files)
        if asset_manifest is None:
            asset_manifest = fetch(
                f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/assets/profile_assets/manifest.json"
            )
        json.loads(asset_manifest)
        files["assets/profile_assets/manifest.json"] = asset_manifest
        # 全部校验完成后才写入固定目标；不导入或执行下载的脚本。
        (root / "vendor").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as stage:
            staged = Path(stage) / "upstream"
            for name, contents in files.items():
                destination = staged / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(contents)
            target = root / "vendor/upstream"
            previous = Path(stage) / "previous"
            if target.exists():
                target.rename(previous)
            try:
                shutil.move(str(staged), target)
            except BaseException:
                if previous.exists():
                    previous.rename(target)
                raise
        (root / "vendor/unified_message.py").write_bytes(message)
        (root / "vendor/prompts.json").write_text(
            json.dumps(prompts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (root / "LICENSE").write_bytes(license_text)
        lock = {"repository": REPOSITORY, "commit": commit, "archive_sha256": hashlib.sha256(archive).hexdigest()}
        (root / "upstream.lock.json").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "maibot-daily-analysis-upstream-sync"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read(25 * 1024 * 1024)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.archive:
        if not args.commit:
            parser.error("本地同步必须指定 --commit")
        sync(args.archive.read_bytes(), args.commit)
    else:
        remote = json.loads(fetch(f"https://api.github.com/repos/{REPOSITORY}/commits/main"))
        commit = remote["sha"]
        current = json.loads((ROOT / "upstream.lock.json").read_text(encoding="utf-8"))
        if commit != current["commit"]:
            sync(fetch(f"https://codeload.github.com/{REPOSITORY}/zip/{commit}"), commit)
            print(f"已同步上游 {commit}")
        else:
            print("上游无变化")
