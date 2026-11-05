"""原引擎完整调用链与 SnowLuma 合约测试，使用真实 SDK 及模拟 RPC。"""

import base64
import hashlib
import io
import json
from datetime import date, datetime

import httpx
import pytest
from daily_plugin.admin import mask_secrets, preserve_secrets
from daily_plugin.full_runtime import AnalysisSkipped, EngineRuntime, IncrementalBatchSaved
from daily_plugin.platform_bridge import OneBotHTTP, SnowLumaAdapter
from PIL import Image
from test_port import Host, make_plugin


class FullHost(Host):
    async def rpc(self, method, plugin_id, payload, **kwargs):
        capability = payload["capability"]
        if capability == "config.get":
            return {"success": True, "value": ""}
        if capability == "chat.get_avatar":
            return {"success": True, "status": "unavailable"}
        if capability == "llm.generate":
            self.calls.append((capability, payload.get("args", {})))
            return {
                "success": True,
                "response": json.dumps(
                    [{"topic": "部署讨论", "contributors": ["测试群友"], "detail": "群友讨论了部署方法并完成验证。"}],
                    ensure_ascii=False,
                ),
            }
        if capability == "render.html2png":
            memory = io.BytesIO()
            Image.new("RGB", (100, 100), "white").save(memory, "PNG")
            return {"success": True, "result": {"image_base64": base64.b64encode(memory.getvalue()).decode()}}
        return await super().rpc(method, plugin_id, payload, **kwargs)


async def test_full_daily_roundtrip_and_all_templates(tmp_path):
    host = FullHost()
    plugin = make_plugin(
        tmp_path,
        host,
        advanced_json=json.dumps(
            {
                "analysis_features": {"golden_quote_analysis_enabled": False},
            }
        ),
    )
    engine = EngineRuntime(plugin.ctx, plugin.settings, plugin.store)
    plugin.runtime = engine
    try:
        await engine.start()
        report = await engine.analyze("stream-a", date(2026, 11, 4))
        assert report.engine_payload and report.topics[0].topic == "部署讨论"
        queried = await plugin.query_report_tool("stream-a", "2026-11-04", "话题")
        assert "部署讨论" in queried
        assert "不能读取" in await plugin.query_report_tool("stream-b", "2026-11-04")
        assert "日期无效" in await plugin.query_report_tool("stream-a", "2026-01-01~2026-11-04")
        assert report.statistics.message_count == 1 and report.statistics.hours[10] == 1
        loaded = plugin.store.get("stream-a", report.day)
        assert loaded and engine.payload(loaded)["topics"][0].topic == "部署讨论"
        templates = engine.report_generator.html_templates.get_available_templates()
        assert len(templates) >= 12
        for template in templates:
            path = await engine.render(report, "html", template["id"])
            assert path.is_file() and "部署讨论" in path.read_text(encoding="utf-8")
        for output in ("text", "markdown", "image", "pdf"):
            path = await engine.render(report, output)
            assert path.stat().st_size > 0
        assert (await plugin.panel_query("config"))["schema"]
        assert isinstance((await plugin.panel_query("traces"))["rows"], list)
        assert (await plugin.panel_query("history"))["rows"][0]["day"] == "2026-11-04"
        rows, _ = engine.trace_store.list_traces()
        calls_before = sum(name == "llm.generate" for name, _ in host.calls)
        resumed = await engine.resume_analysis(str(rows[0]["trace_id"]), "stream-a", "qq", "2026-11-04")
        assert resumed["success"] and resumed["resumed_from"] == "LLM_ANALYSIS"
        assert sum(name == "llm.generate" for name, _ in host.calls) == calls_before
    finally:
        await engine.close()


async def test_default_configuration_never_schedules_all_groups(tmp_path):
    plugin = make_plugin(tmp_path, FullHost())
    engine = EngineRuntime(plugin.ctx, plugin.settings, plugin.store)
    plugin.runtime = engine
    try:
        await engine.start()
        await plugin.schedule_original(datetime(2026, 11, 5, 23, 59))
        assert not plugin.workers
    finally:
        await engine.close()


def test_secrets_roundtrip():
    value = {"token": "abc", "profiles": [{"name": "draw", "api_key": "xyz"}]}
    hidden = mask_secrets(value)
    assert "abc" not in str(hidden) and "xyz" not in str(hidden)
    assert preserve_secrets(value, hidden) == value


async def test_snowluma_retcode_and_chunk_upload(monkeypatch, tmp_path):
    bridge = OneBotHTTP("http://localhost:3000", "test-token")
    requests = []
    content = b"test" * 100000
    path = tmp_path / "report.pdf"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    async def post(self, url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        request = httpx.Request("POST", url)
        params = kwargs["json"]
        requests.append(params)
        if url.endswith("/fail"):
            return httpx.Response(200, json={"retcode": 120, "message": "muted"}, request=request)
        data = (
            {"file_path": "/snowluma/temp/report.pdf", "sha256": digest}
            if params["is_complete"]
            else {"type": "stream"}
        )
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await bridge.stage_file(path) == "/snowluma/temp/report.pdf"
    assert len(requests) == 2 and requests[0]["chunk_index"] == 0
    assert requests[-1]["expected_sha256"] == digest
    assert b"".join(base64.b64decode(row["chunk_data"]) for row in requests) == content
    with pytest.raises(RuntimeError, match="muted"):
        await bridge.call_action("fail")


async def test_incremental_cursor_final_and_saved_config(tmp_path):
    plugin = make_plugin(
        tmp_path,
        FullHost(),
        advanced_json=json.dumps(
            {
                "incremental": {"incremental_min_messages": 1},
                "analysis_features": {"golden_quote_analysis_enabled": False},
            }
        ),
    )
    engine = EngineRuntime(plugin.ctx, plugin.settings, plugin.store)
    plugin.runtime = engine
    try:
        await engine.start()
        with pytest.raises(IncrementalBatchSaved):
            await engine.analyze("stream-a", date(2026, 11, 4), "incremental")
        cursor, _ = await engine.incremental_store.get_last_analyzed_cursor("stream-a")
        assert cursor > 0
        with pytest.raises(AnalysisSkipped):
            await engine.analyze("stream-a", date(2026, 11, 4), "incremental")
        report = await engine.analyze("stream-a", date(2026, 11, 4), "final")
        assert report.statistics.message_count == 1 and report.day == "2026-11-04"
        plugin.save_config_value("basic.max_messages", "555")
    finally:
        await engine.close()
    second = EngineRuntime(plugin.ctx, plugin.settings, plugin.store)
    try:
        await second.start()
        assert second.config_manager.get_max_messages() == 555
    finally:
        await second.close()


async def test_snowluma_detection_and_integer_cursor():
    class API:
        def __init__(self):
            self.calls = []

        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "get_version_info":
                return {"app_name": "SnowLuma", "app_version": "test"}
            return {"messages": []}

    api = API()
    adapter = SnowLumaAdapter(api)
    driver = await adapter._ensure_driver()
    assert driver.name == "snowluma"
    assert driver.build_history_params("111", 20, 123) == {"group_id": 111, "count": 20, "message_id": 123}
    assert not driver.build_history_params("111", 20, 0).get("reverseOrder")


async def test_admin_upload_order_and_artifact_boundary(tmp_path):
    plugin = make_plugin(tmp_path, FullHost())
    engine = EngineRuntime(plugin.ctx, plugin.settings, plugin.store)
    plugin.runtime = engine
    try:
        await engine.start()
        with pytest.raises(ValueError, match="顺序"):
            await plugin.template_chunk("a" * 32, 1, 2, base64.b64encode(b"x").decode())
        with pytest.raises(ValueError, match="序号"):
            await plugin.template_chunk("../../outside", 0, 1, "eA==")
        plugin.admin_status["bad"] = {"artifact": "../outside.png"}
        with pytest.raises(ValueError, match="路径"):
            await plugin.artifact_data("bad")
    finally:
        await engine.close()


async def test_all_analyzers_and_comic_drawing_http(tmp_path, monkeypatch):
    class StageHost(FullHost):
        async def rpc(self, method, plugin_id, payload, **kwargs):
            if payload["capability"] == "llm.generate":
                args = payload.get("args", {})
                model = args.get("model")
                responses = {
                    "titles": [
                        {
                            "name": "测试群友",
                            "user_id": "123",
                            "title": "部署达人",
                            "mbti": "INTJ",
                            "reason": "喜欢研究部署",
                        }
                    ],
                    "quotes": [{"content": "今天讨论部署方法", "sender": "测试群友", "reason": "主题明确"}],
                    "quality": {
                        "title": "技术交流",
                        "subtitle": "一起研究",
                        "dimensions": [{"name": "技术", "percentage": 100, "comment": "聚焦部署"}],
                        "summary": "交流顺畅",
                    },
                    "storyboard": {
                        "scene": 'A comic panel showing a computer and speech bubble with exact Chinese text "部署成功".'
                    },
                }
                if model in responses:
                    self.calls.append(("llm.generate", args))
                    return {"success": True, "response": json.dumps(responses[model], ensure_ascii=False)}
            return await super().rpc(method, plugin_id, payload, **kwargs)

    host = StageHost()
    plugin = make_plugin(
        tmp_path,
        host,
        quality_enabled=True,
        titles_enabled=True,
        advanced_json=json.dumps(
            {
                "llm": {
                    "user_title_provider_id": "titles",
                    "golden_quote_provider_id": "quotes",
                    "quality_provider_id": "quality",
                    "drawing_prompt_provider_id": "storyboard",
                },
                "daily_comic": {
                    "enable_daily_comic": True,
                    "drawing_provider_overrides": [
                        {
                            "__template_key": "openai_images",
                            "name": "drawing-test",
                            "api_key": "test-key",
                            "api_url": "https://draw.example/v1",
                            "model": "test-image",
                        }
                    ],
                },
            }
        ),
    )
    memory = io.BytesIO()
    Image.new("RGB", (100, 100), "blue").save(memory, "PNG")
    image_data = memory.getvalue()
    drawing_calls = []

    async def post(self, url, **kwargs):
        drawing_calls.append((url, kwargs))
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(image_data).decode()}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    engine = EngineRuntime(plugin.ctx, plugin.settings, plugin.store)
    plugin.runtime = engine
    try:
        await engine.start()
        report = await engine.analyze("stream-a", date(2026, 11, 4))
        assert report.titles[0].title == "部署达人"
        assert report.quotes[0].content == "今天讨论部署方法"
        assert report.quality and report.quality.title == "技术交流"
        comic, url = await engine.comic(report)
        assert comic == image_data and url is None
        assert drawing_calls and drawing_calls[0][0] == "https://draw.example/v1/images/generations"
        assert "部署成功" in drawing_calls[0][1]["json"]["prompt"]
    finally:
        await engine.close()
