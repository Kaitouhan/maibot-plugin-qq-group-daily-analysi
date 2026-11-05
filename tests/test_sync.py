"""上游同步只能更新白名单材料，拒绝不匹配的提交与契约漂移。"""

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("upstream_sync", ROOT / "scripts/sync_upstream.py")
assert spec and spec.loader
sync_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_module)


def archive(commit: str, broken: bool = False) -> bytes:
    memory = io.BytesIO()
    prompts = json.loads((ROOT / "vendor/prompts.json").read_text(encoding="utf-8"))
    if broken:
        prompts["topic_prompt"] = "变量已删除"
    schema = {"prompts": {"items": {"section": {"items": {k: {"default": v} for k, v in prompts.items()}}}}}
    with zipfile.ZipFile(memory, "w") as bundle:
        bundle.comment = commit.encode()
        bundle.writestr("source/", "")
        bundle.writestr(
            "source/src/domain/value_objects/unified_message.py", (ROOT / "vendor/unified_message.py").read_bytes()
        )
        bundle.writestr("source/_conf_schema.json", json.dumps(schema))
        bundle.writestr("source/LICENSE", (ROOT / "LICENSE").read_bytes())
        bundle.writestr("source/metadata.yaml", "version: v5.9.4")
        bundle.writestr(
            "source/src/application/services/comic_application_service.py",
            """class Comic:
    async def _generate_via_general_plugin(self):
        pass
    async def _generate_via_big_banana(self):
        pass
    def following(self):
        pass
""",
        )
        bundle.writestr(
            "source/src/application/services/analysis_application_service.py",
            """from __future__ import annotations
date_str = dt.datetime.now().strftime("%Y-%m-%d")
""",
        )
        bundle.writestr(
            "source/src/infrastructure/reporting/render_data_preparer.py",
            """from __future__ import annotations
value = format_report_date(report_language, datetime.now())
""",
        )
        bundle.writestr(
            "source/src/infrastructure/reporting/typst_data.py",
            """from __future__ import annotations
value = format_report_date(explicit_language, datetime.now())
""",
        )
        bundle.writestr("source/src/infrastructure/analysis/analyzers/base_analyzer.py", "from astrbot.api import sp")
        bundle.writestr("source/../../plugin.py", "不应该解压此文件")
    return memory.getvalue()


def test_sync_does_not_touch_adapter_or_config(tmp_path: Path) -> None:
    (tmp_path / "plugin.py").write_text("原适配代码", encoding="utf-8")
    (tmp_path / "config.toml").write_text("用户配置", encoding="utf-8")
    sync_module.sync(archive("a" * 40), "a" * 40, tmp_path, asset_manifest=b"{}")
    assert (tmp_path / "plugin.py").read_text(encoding="utf-8") == "原适配代码"
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == "用户配置"
    lock = json.loads((tmp_path / "upstream.lock.json").read_text(encoding="utf-8"))
    assert lock["commit"] == "a" * 40 and len(lock["archive_sha256"]) == 64
    assert (tmp_path / "vendor/upstream/src/host.py").exists()
    assert not (tmp_path.parent / "plugin.py").exists()


@pytest.mark.parametrize("wrong_commit,broken", [(True, False), (False, True)])
def test_sync_rejects_before_writing(tmp_path: Path, wrong_commit: bool, broken: bool) -> None:
    with pytest.raises(ValueError):
        sync_module.sync(archive("a" * 40, broken), "b" * 40 if wrong_commit else "a" * 40, tmp_path)
    assert not (tmp_path / "upstream.lock.json").exists()
