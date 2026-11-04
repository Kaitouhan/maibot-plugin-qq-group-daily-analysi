"""真实 SDK 代理上的消息、任务、隔离、降级和持久化回归。"""

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
from daily_plugin import DailyAnalysisPlugin
from daily_plugin.analysis import Analyzer
from daily_plugin.configuration import AnalysisConfig
from daily_plugin.domain import Report, day_bounds, normalize_message, statistics, transcript_chunks
from daily_plugin.reporting import html_report
from daily_plugin.storage import Store
from maibot_sdk import PluginContext, PluginPaths


def raw_message(uid: str = "123", text: str = "今天讨论部署方法", identifier: str = "m1") -> dict:
    return {
        "message_id": identifier,
        "session_id": "stream-a",
        "timestamp": "2026-11-04T10:30:00+08:00",
        "message_info": {"user_info": {"user_id": uid, "user_nickname": "测试群友"}},
        "raw_message": [{"type": "text", "data": text}, {"type": "emoji", "data": ""}],
    }


def sample_report() -> Report:
    msg = normalize_message(raw_message(), "stream-a")
    assert msg
    return Report(stream_id="stream-a", day="2026-11-04", statistics=statistics([msg]))


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.messages = [raw_message()]
        self.render_fail = False
        self.send_fail = False

    async def rpc(self, method: str, plugin_id: str, payload: dict, **kwargs: object) -> dict:
        assert method == "cap.call"
        capability = payload["capability"]
        args = payload.get("args", {})
        self.calls.append((capability, args))
        if capability == "chat.get_group_streams":
            return {"success": True, "streams": [{"stream_id": "stream-a", "group_id": "111"}]}
        if capability == "message.get_by_time_in_chat":
            assert args["chat_id"] == "stream-a"
            assert args["filter_mai"] is True
            return {"success": True, "messages": self.messages}
        if capability == "llm.generate":
            return {"success": True, "response": "[]"}
        if capability == "render.html2png":
            if self.render_fail:
                raise RuntimeError("模拟渲染不可用")
            return {"success": True, "result": {"image_base64": "aW1hZ2U="}}
        if capability.startswith("send."):
            return {"success": not self.send_fail, "sent": not self.send_fail}
        raise AssertionError(capability)


def make_plugin(tmp_path: Path, host: Host, **config: object) -> DailyAnalysisPlugin:
    plugin = DailyAnalysisPlugin()
    plugin._set_context(
        PluginContext(
            "kaitouhan.qq-group-daily-analysis", rpc_call=host.rpc, paths=PluginPaths(tmp_path, tmp_path / "runtime")
        )
    )
    settings = plugin.build_default_config()
    settings.update({"min_messages": 1, "quality_enabled": False, "titles_enabled": False, **config})
    plugin.set_plugin_config(settings)
    plugin.store = Store(tmp_path)
    return plugin


def test_message_contract_and_timezone() -> None:
    msg = normalize_message(raw_message(), "stream-a")
    assert msg and msg.sender_id == "123"
    stats = statistics([msg])
    assert stats.hours[10] == 1
    assert stats.emoji_count == 1
    assert stats.total_characters == len("今天讨论部署方法")
    start, end = day_bounds(date(2026, 11, 4))
    assert end - start == 86400
    assert start <= msg.timestamp < end
    assert normalize_message(raw_message(), "other-stream") is None
    assert normalize_message(raw_message(text="/群分析"), "stream-a") is None


def test_chunk_budget_and_html_escaping() -> None:
    msg = normalize_message(raw_message(text="<script>alert(1)</script>" * 100), "stream-a")
    assert msg
    assert all(len(chunk) <= 1000 for chunk in transcript_chunks([msg] * 4, 1000))
    report = sample_report()
    report.statistics.names["123"] = "<script>alert(1)</script>"
    html = html_report(report)
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_storage_isolation_and_claim(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.save(sample_report())
    assert store.get("stream-a", "2026-11-04")
    assert store.get("stream-b", "2026-11-04") is None
    assert store.claim("stream-a", "2026-11-04")
    assert not Store(tmp_path).claim("stream-a", "2026-11-04")
    assert Store(tmp_path).history("stream-a") == ["2026-11-04"]


async def test_sdk_query_analyze_render_send(tmp_path: Path) -> None:
    host = Host()
    plugin = make_plugin(tmp_path, host)
    await plugin.run_job("stream-a", date(2026, 11, 4), "image", False)
    assert plugin.store and plugin.store.get("stream-a", "2026-11-04")
    assert any(name == "send.image" for name, _ in host.calls)
    assert not plugin.workers


async def test_denied_stream_never_queries_history(tmp_path: Path) -> None:
    host = Host()
    plugin = make_plugin(tmp_path, host, allowed_streams=["stream-b"])
    result = await plugin.handle_analysis(stream_id="stream-a")
    assert result[0] is False
    assert not any(name == "message.get_by_time_in_chat" for name, _ in host.calls)


async def test_render_failure_falls_back_to_text(tmp_path: Path) -> None:
    host = Host()
    host.render_fail = True
    plugin = make_plugin(tmp_path, host)
    await plugin.deliver(sample_report(), "image")
    assert any(name == "send.text" for name, _ in host.calls)


async def test_failed_send_is_not_marked_sent(tmp_path: Path) -> None:
    host = Host()
    host.send_fail = True
    plugin = make_plugin(tmp_path, host)
    assert plugin.store and plugin.store.claim("stream-a", "2026-11-04")
    await plugin.run_job("stream-a", date(2026, 11, 4), "text", True)
    with plugin.store.connect() as db:
        assert db.execute("SELECT status FROM deliveries").fetchone()[0] == "failed"


async def test_forged_quotes_are_removed() -> None:
    class FakeModel:
        async def generate(self, prompt: str, model: str = "", **kwargs: object) -> dict:
            if "金句标准" in prompt:
                response = [
                    {"sender": "[123]", "content": "今天讨论部署方法"},
                    {"sender": "[999]", "content": "伪造发言"},
                ]
            else:
                response = []
            return {"success": True, "response": json.dumps(response)}

    msg = normalize_message(raw_message(), "stream-a")
    assert msg
    config = AnalysisConfig(quality_enabled=False, titles_enabled=False)
    report = await Analyzer(FakeModel(), config).analyze(sample_report(), [msg])
    assert len(report.quotes) == 1
    assert report.quotes[0].sender == "[123]"


async def test_invalid_json_preserves_statistics() -> None:
    class BadModel:
        async def generate(self, prompt: str, model: str = "", **kwargs: object) -> dict:
            return {"success": True, "response": "这不是 JSON"}

    msg = normalize_message(raw_message(), "stream-a")
    assert msg
    report = await Analyzer(BadModel(), AnalysisConfig()).analyze(sample_report(), [msg])
    assert report.statistics.message_count == 1
    assert report.warnings


async def test_shutdown_cancels_workers(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path, Host())
    task = asyncio.create_task(asyncio.sleep(600))
    plugin.workers["stream-a"] = task
    await plugin.on_unload()
    assert task.cancelled() and not plugin.workers


async def test_lifecycle_default_schedule_is_quiet(tmp_path: Path) -> None:
    host = Host()
    plugin = make_plugin(tmp_path, host)
    await plugin.on_load()
    await asyncio.sleep(0)
    await plugin.on_unload()
    assert not any(name.startswith("send.") for name, _ in host.calls)
    assert plugin.scheduler and plugin.scheduler.cancelled()


def test_manifest_and_config_version() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    config = DailyAnalysisPlugin.build_default_config()
    assert manifest["version"] == config["plugin"]["config_version"]
    assert manifest["host_application"]["min_version"] == "1.3.15"
    assert set(manifest["capabilities"]) == {
        "send.text",
        "send.image",
        "chat.get_group_streams",
        "message.get_by_time_in_chat",
        "llm.generate",
        "render.html2png",
    }


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        AnalysisConfig(send_at="25:00")
    with pytest.raises(ValueError):
        AnalysisConfig(chunk_chars=0)
    with pytest.raises(ValueError):
        AnalysisConfig(output="unknown")
