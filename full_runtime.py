"""复用原插件完整应用服务、渲染、漫画、增量和追踪实现。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from datetime import date
from pathlib import Path
from typing import Any, cast

from maibot_sdk import PluginContext
from PIL import Image

from .compatibility import REPORT_DAY, ModelContext, NativeConfig, RuntimePaths, report_datetime
from .configuration import AnalysisConfig
from .domain import Quality, Quote, Report, Topic, UserTitle, day_bounds
from .platform_bridge import WINDOW, BotManager, OneBotHTTP
from .storage import Store
from .vendor.upstream.src.application.services.analysis_application_service import AnalysisApplicationService
from .vendor.upstream.src.application.services.analysis_serializer import AnalysisResultSerializer
from .vendor.upstream.src.application.services.comic_application_service import ComicApplicationService
from .vendor.upstream.src.application.services.crash_recovery_service import CrashRecoveryService
from .vendor.upstream.src.application.services.task_lifecycle_service import TaskLifecycleService
from .vendor.upstream.src.domain.services.analysis_domain_service import AnalysisDomainService
from .vendor.upstream.src.domain.services.incremental_merge_service import IncrementalMergeService
from .vendor.upstream.src.domain.services.statistics_service import StatisticsService
from .vendor.upstream.src.domain.value_objects import AnalysisResultPayload
from .vendor.upstream.src.infrastructure.analysis.llm_analyzer import LLMAnalyzer
from .vendor.upstream.src.infrastructure.config.config_manager import ConfigManager
from .vendor.upstream.src.infrastructure.drawing.drawing_client import DrawingClient
from .vendor.upstream.src.infrastructure.persistence.checkpoint_store import CheckpointStore
from .vendor.upstream.src.infrastructure.persistence.history_manager import HistoryManager
from .vendor.upstream.src.infrastructure.persistence.incremental_store import IncrementalStore
from .vendor.upstream.src.infrastructure.persistence.trace_sqlite_store import TraceSQLiteStore
from .vendor.upstream.src.infrastructure.reporting.generators import ReportGenerator
from .vendor.upstream.src.infrastructure.visualization.activity_charts import ActivityVisualizer
from .vendor.upstream.src.shared.trace_context import TraceContext

SOURCE = Path(__file__).parent / "vendor/upstream"


class EngineRuntime:
    """麦麦宿主实例：原服务仍使用强类型的领域模型与仓储协议。"""

    def __init__(self, ctx: PluginContext, settings: AnalysisConfig, store: Store) -> None:
        self.ctx, self.settings, self.store = ctx, settings, store
        self.plugin_data_dir = ctx.paths.data_dir
        RuntimePaths.root = self.plugin_data_dir
        previous = store.kv_get("native_root_settings", {})
        if not isinstance(previous, dict):
            previous = {}
        override = json.loads(settings.advanced_json) if previous.get("advanced_json") != settings.advanced_json else {}
        self.native_config = NativeConfig(SOURCE, self.plugin_data_dir / "upstream_config.json", override)
        mapping = {
            "max_messages": ("basic", "max_messages"),
            "min_messages": ("basic", "min_messages_threshold"),
            "model": ("llm", "llm_provider_id"),
            "llm_timeout": ("llm", "llm_hard_timeout"),
            "max_topics": ("analysis_features", "max_topics"),
            "max_quotes": ("analysis_features", "max_golden_quotes"),
            "titles_enabled": ("analysis_features", "user_title_analysis_enabled"),
            "quality_enabled": ("analysis_features", "chat_quality_analysis_enabled"),
        }
        mapping.update({"report_template": ("basic", "report_template"), "output": ("basic", "output_format")})
        current = {name: getattr(settings, name) for name in mapping}
        current["advanced_json"] = settings.advanced_json
        for name, (group, key) in mapping.items():
            if self.native_config.first_deploy or (name in previous and previous[name] != current[name]):
                value = current[name] or "utils" if name == "model" else current[name]
                self.native_config[group][key] = [value] if name == "output" else value
        self._root_settings = current
        self.config_manager = ConfigManager(self.native_config)
        onebot = OneBotHTTP(settings.onebot_endpoint, settings.onebot_token) if settings.onebot_endpoint else None
        self.bot_manager = BotManager(ctx, onebot)
        self.context = ModelContext(ctx, settings.model)
        self.context.persona_manager.profiles = json.loads(settings.persona_profiles_json)
        self.context.external_drawing_api = settings.external_drawing_api
        self.history_manager = HistoryManager(self)
        self.incremental_store = IncrementalStore(self)
        self.checkpoint_store = CheckpointStore(self.plugin_data_dir / "traces.db")
        self.trace_store = TraceSQLiteStore(self.plugin_data_dir / "traces.db")
        self.lifecycle = TaskLifecycleService(self.trace_store)
        TraceContext.set_global_store(self.trace_store)
        TraceContext.set_global_lifecycle_service(self.lifecycle)
        self.report_generator = ReportGenerator(self.config_manager, self.plugin_data_dir)
        self.llm_analyzer = LLMAnalyzer(self.context, self.config_manager)
        self.analysis_service = AnalysisApplicationService(
            self.config_manager,
            cast(Any, self.bot_manager),
            self.history_manager,
            self.report_generator,
            self.llm_analyzer,
            StatisticsService(ActivityVisualizer()),
            AnalysisDomainService(),
            incremental_store=self.incremental_store,
            incremental_merge_service=IncrementalMergeService(),
            checkpoint_store=self.checkpoint_store,
            html_render=self.html_render,
        )
        self.comic_service = ComicApplicationService(
            self.llm_analyzer,
            DrawingClient(self.config_manager),
            self.config_manager,
            self.plugin_data_dir,
            context=self.context,
        )

    async def start(self) -> None:
        await self.bot_manager.refresh()
        self.native_config.save_config()
        await asyncio.to_thread(self.store.kv_put, "native_root_settings", self._root_settings)
        recovery = CrashRecoveryService(
            self.trace_store, self.checkpoint_store, cast(Any, self), cast(Any, self), self.lifecycle
        )
        await recovery.recover_crashed_tasks()

    async def close(self) -> None:
        await self.report_generator.close()
        TraceContext.set_global_store(None)
        TraceContext.set_global_lifecycle_service(None)

    async def get_kv_data(self, key: str, default: object = None) -> object:
        return await asyncio.to_thread(self.store.kv_get, key, default)

    async def put_kv_data(self, key: str, value: object) -> None:
        await asyncio.to_thread(self.store.kv_put, key, value)

    async def html_render(
        self,
        html: str,
        data: dict | None = None,
        return_url: bool = False,
        options: dict | None = None,
        **kwargs: object,
    ) -> bytes:
        """将原渲染器的选项映射为 Host 公开截图参数。"""
        options = options or {}
        viewport = options.get("viewport", {"width": 1000, "height": 600})
        if not isinstance(viewport, dict):
            viewport = {"width": 1000, "height": 600}
        card = await self.ctx.render.html2png(
            html, full_page=bool(options.get("full_page", True)), allow_network=True, viewport=viewport
        )
        if not card.get("image_base64"):
            raise RuntimeError("Host 渲染未返回图片")
        return base64.b64decode(card["image_base64"])

    async def analyze(self, stream: str, day: date, mode: str = "daily", manual: bool = True) -> Report:
        """在正确的自然日窗口执行原用例；所有检查点和批次仍由原仓储维护。"""
        await self.bot_manager.refresh()
        item = self.bot_manager.streams[stream]
        platform = str(item.get("platform", "qq"))
        token = WINDOW.set(day_bounds(day))
        day_token = REPORT_DAY.set(day)
        try:
            async with TraceContext(group_id=stream, platform=platform, operation=mode, trigger_type="manual"):
                if mode == "incremental":
                    result = await self.analysis_service.execute_incremental_analysis(stream, platform)
                    if result.get("success") and self.config_manager.get_incremental_report_immediately():
                        result = await self.analysis_service.execute_incremental_final_report(stream, platform)
                elif mode == "final":
                    result = await self.analysis_service.execute_incremental_final_report(stream, platform)
                    if not result.get("success") and self.config_manager.get_incremental_fallback_enabled():
                        result = await self.analysis_service.execute_daily_analysis(stream, platform, manual=manual)
                else:
                    result = await self.analysis_service.execute_daily_analysis(stream, platform, manual=manual)
                if not result.get("success"):
                    if result.get("reason") in {"muted", "no_messages", "insufficient_messages", "below_threshold"}:
                        raise AnalysisSkipped(str(result.get("reason")))
                    raise RuntimeError(str(result.get("error") or result.get("reason") or "分析未完成"))
                payload = result.get("analysis_result")
                if not isinstance(payload, dict):
                    # 增量任务只写入批次，不假装返回全天报告。
                    raise IncrementalBatchSaved(str(result.get("batch_id", "已保存")))
                report = self.to_report(stream, day, cast(AnalysisResultPayload, payload))
                await asyncio.to_thread(self.store.save, report)
                return report
        finally:
            WINDOW.reset(token)
            REPORT_DAY.reset(day_token)

    async def resume_analysis(
        self, trace_id: str, group_id: str, platform_id: str | None = None, date_str: str | None = None
    ) -> dict:
        day = date.fromisoformat(date_str) if date_str else report_datetime().date()
        window, selected = WINDOW.set(day_bounds(day)), REPORT_DAY.set(day)
        try:
            async with TraceContext(group_id=group_id, platform=platform_id or "", trigger_type="resume") as trace:
                result = await self.analysis_service.resume_analysis(
                    trace.trace_id, group_id, platform_id, date_str=day.isoformat(), checkpoint_trace_id=trace_id
                )
                payload = result.get("analysis_result")
                if result.get("success") and isinstance(payload, dict):
                    report = self.to_report(group_id, day, cast(AnalysisResultPayload, payload))
                    await asyncio.to_thread(self.store.save, report)
                return dict(result)
        finally:
            WINDOW.reset(window)
            REPORT_DAY.reset(selected)

    async def dispatch(
        self, group_id: str, analysis_result: AnalysisResultPayload, platform_id: str | None = None
    ) -> bool:
        """开机恢复完成后的原分发协议，使用麦麦发送并验证结果。"""
        item = self.bot_manager.streams.get(group_id)
        if (
            not item
            or not self.settings.plugin.enabled
            or (self.settings.allowed_streams and group_id not in self.settings.allowed_streams)
        ):
            return False
        target = f"{item.get('platform', 'qq')}:GroupMessage:{item.get('group_id', group_id)}"
        if not self.config_manager.is_group_allowed(target):
            return False
        day = report_datetime().date()
        report = self.to_report(group_id, day, analysis_result)
        await asyncio.to_thread(self.store.save, report)
        adapter = self.bot_manager.get_adapter(platform_id)
        if not adapter:
            return False
        for output in self.config_manager.get_output_format():
            path = await self.render(report, output)
            if output == "image":
                sent = await adapter.send_image(group_id, str(path))
            elif output in {"text", "markdown"}:
                sent = await adapter.send_text(group_id, path.read_text(encoding="utf-8"))
            else:
                sent = await adapter.send_file(group_id, path)
            if not sent:
                raise RuntimeError("恢复任务产物未成功投递")
        return True

    def to_report(self, stream: str, day: date, payload: AnalysisResultPayload) -> Report:
        from .domain import Statistics

        stats = payload["statistics"]
        users = payload.get("user_analysis", {})
        counts = {}
        names = {}
        for uid, details in users.items():
            if isinstance(details, dict):
                counts[str(uid)] = int(details.get("message_count", details.get("count", 0)))
                names[str(uid)] = str(details.get("nickname", details.get("name", uid)))
        adapter = self.bot_manager.get_adapter(str(self.bot_manager.streams[stream].get("platform", "qq")))
        if adapter:
            names.update(adapter.names)
        activity = stats.activity_visualization.hourly_activity
        hours = [int(activity.get(index, 0)) for index in range(24)]
        review = payload.get("chat_quality_review")
        report = Report(
            stream_id=stream,
            day=day.isoformat(),
            statistics=Statistics(
                message_count=stats.message_count,
                participant_count=stats.participant_count,
                total_characters=stats.total_characters,
                emoji_count=stats.emoji_count,
                hours=hours,
                most_active_period=stats.most_active_period,
                users=counts,
                names=names,
            ),
            topics=[Topic(topic=t.topic, contributors=t.contributors, detail=t.detail) for t in payload["topics"]],
            titles=[UserTitle.model_validate(t.to_dict()) for t in payload["user_titles"]],
            quotes=[Quote.model_validate(q.to_dict()) for q in stats.golden_quotes],
            quality=Quality.model_validate(review.to_dict()) if review else None,
            engine_payload=AnalysisResultSerializer.serialize(dict(payload)),
        )
        return report

    def templates(self) -> list[dict]:
        manager = self.report_generator.html_templates
        if manager is None:
            raise RuntimeError("模板管理器尚未初始化")
        return [dict(item) for item in manager.get_available_templates()]

    def payload(self, report: Report) -> AnalysisResultPayload:
        if not report.engine_payload:
            raise ValueError("旧版日报没有完整引擎快照，请重新分析后使用高级输出")
        return AnalysisResultSerializer.deserialize(report.engine_payload)

    async def render(self, report: Report, output: str, template: str = "") -> Path:
        token = REPORT_DAY.set(date.fromisoformat(report.day))
        try:
            return await self._render(report, output, template)
        finally:
            REPORT_DAY.reset(token)

    async def _render(self, report: Report, output: str, template: str = "") -> Path:
        """生成全部原生主题的图片/HTML/Markdown，并提供 PDF 文件。"""
        payload = self.payload(report)
        theme = template or self.config_manager.get_report_template()
        platform = str(self.bot_manager.streams[report.stream_id].get("platform", "qq"))
        adapter = self.bot_manager.get_adapter(platform)
        avatar_getter = adapter.get_user_avatar_url if adapter else None
        directory = self.plugin_data_dir / "reports"
        directory.mkdir(exist_ok=True)
        stamp = f"{abs(hash(report.stream_id)):x}_{report.day}_{time.time_ns()}"
        if output == "html":
            path, _ = await self.report_generator.generate_html_report(
                payload,
                report.stream_id,
                template_theme=theme,
                avatar_url_getter=avatar_getter,
                avatar_cache_namespace=platform,
                nickname_getter=self.nickname_getter(report),
            )
            if not path:
                raise RuntimeError("HTML 报告生成失败")
            return Path(path)
        if output in {"markdown", "text"}:
            text = self.report_generator.generate_text_report(payload)
            if output == "markdown":
                text, _ = await self.report_generator.generate_markdown_report(payload)
            path = directory / f"{stamp}.{'md' if output == 'markdown' else 'txt'}"
            path.write_text(text, encoding="utf-8")
            return path
        image, html = await self.report_generator.generate_image_report(
            payload,
            report.stream_id,
            html_render_func=self.html_render,
            template_theme=theme,
            avatar_url_getter=avatar_getter,
            avatar_cache_namespace=platform,
            nickname_getter=self.nickname_getter(report),
        )
        if not image:
            raise RuntimeError("所有图片渲染方案均失败，请检查渲染日志与字体资源")
        if isinstance(image, str) and image.startswith(("base64://", "data:image/")):
            encoded = image.removeprefix("base64://") if image.startswith("base64://") else image.split(",", 1)[1]
            data = base64.b64decode(encoded)
        elif isinstance(image, str) and Path(image).is_file():
            data = Path(image).read_bytes()
        elif isinstance(image, bytes):
            data = image
        else:
            raise RuntimeError("渲染结果不是可读取的本地图片")
        path = directory / f"{stamp}.{'pdf' if output == 'pdf' else 'png'}"
        if output == "pdf":
            await asyncio.to_thread(self.image_pdf, data, path)
        else:
            path.write_bytes(data)
        return path

    @staticmethod
    def image_pdf(data: bytes, target: Path) -> None:
        with Image.open(io.BytesIO(data)) as image:
            image.convert("RGB").save(target, "PDF", resolution=144)

    @staticmethod
    def nickname_getter(report: Report):
        async def get(uid: str) -> str | None:
            return report.statistics.names.get(uid)

        return get

    async def comic(self, report: Report) -> tuple[bytes | None, str | None]:
        payload = self.payload(report)
        await self.bot_manager.refresh()
        item = self.bot_manager.streams[report.stream_id]
        target = f"{item.get('platform', 'qq')}:GroupMessage:{item.get('group_id', report.stream_id)}"
        if not self.config_manager.is_comic_group_allowed(target):
            raise PermissionError("该群不在漫画生成允许名单中")
        async with TraceContext(group_id=report.stream_id, operation="comic", trigger_type="manual"):
            return await self.comic_service.generate_comic(payload["topics"], report.stream_id, umo=report.stream_id)


class IncrementalBatchSaved(Exception):
    """增量批次成功落盘；不应作为任务失败处理。"""


class AnalysisSkipped(Exception):
    """无可分析消息或机器人禁言，自动任务按跳过归档。"""
