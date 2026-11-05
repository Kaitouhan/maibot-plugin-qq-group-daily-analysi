"""MaiBot 1.3.15 群日常分析插件入口。"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from typing import cast

from maibot_sdk import Command

from .admin import AdminMixin
from .analysis import Analyzer, ModelGateway
from .configuration import AnalysisConfig
from .domain import SHANGHAI, Report, day_bounds, normalize_message, statistics
from .full_runtime import AnalysisSkipped, EngineRuntime, IncrementalBatchSaved
from .reporting import html_report, text_report
from .storage import Store


class DailyAnalysisPlugin(AdminMixin):
    """通过宿主 RPC 读取记录和调用模型，不导入 AstrBot 或麦麦内部模块。"""

    config_model = AnalysisConfig

    def __init__(self) -> None:
        super().__init__()
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.last_run: dict[str, float] = {}
        self.scheduler: asyncio.Task[None] | None = None
        self.store: Store | None = None
        self.capacity = asyncio.Semaphore(2)
        self.runtime: EngineRuntime | None = None
        self.admin_status: dict[str, dict] = {}
        self.pending_reload = False

    @property
    def settings(self) -> AnalysisConfig:
        return cast(AnalysisConfig, self.config)

    async def on_load(self) -> None:
        self.store = Store(self.ctx.paths.data_dir)
        self.runtime = EngineRuntime(self.ctx, self.settings, self.store)
        await self.runtime.start()
        self.scheduler = asyncio.create_task(self.schedule_loop())
        self.ctx.logger.info("群日常分析插件已加载，目标 MaiBot 1.3.15")

    async def on_unload(self) -> None:
        tasks = list(self.workers.values())
        if self.scheduler:
            tasks.append(self.scheduler)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        if self.runtime:
            await self.runtime.close()
            self.runtime = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        # SDK 负责替换强类型配置；调度循环每轮读取新配置。
        self.ctx.logger.info("群分析配置已更新：%s %s", scope, version)
        if scope == "self":
            self.pending_reload = True

    async def valid_stream(self, stream: str) -> bool:
        """仅允许宿主存在的群聊天流，并应用平台和群名单。"""
        return await self.is_allowed(stream)

    def launch(
        self, stream: str, day: date, output: str, scheduled: bool = False, mode: str = "daily", delivery_key: str = ""
    ) -> None:
        self.workers[stream] = asyncio.create_task(self.run_job(stream, day, output, scheduled, mode, delivery_key))
        self.last_run[stream] = time.monotonic()

    @Command(
        "daily_analysis",
        description="分析当前群聊的自然日记录",
        permission="operator",
        pattern=r"^/(?:群分析|daily_analysis|group_analysis)(?:\s+(?P<day>\d{4}-\d{2}-\d{2}))?(?:\s+(?P<output>text|image|html|markdown|pdf))?$",
    )
    async def handle_analysis(self, stream_id: str = "", **kwargs: object) -> tuple[bool, str, int]:
        """快速排队并返回；避免长报告超过 Command 处理超时。"""
        if not await self.valid_stream(stream_id):
            return False, "仅支持已授权的平台群聊。", 2
        groups = kwargs.get("matched_groups")
        groups = groups if isinstance(groups, dict) else {}
        try:
            day = date.fromisoformat(str(groups["day"])) if groups.get("day") else datetime.now(SHANGHAI).date()
            if day > datetime.now(SHANGHAI).date():
                raise ValueError("未来日期")
        except ValueError:
            return False, "日期无效，示例：/群分析 2026-11-04 text", 2
        if stream_id in self.workers:
            return False, "本群已有分析任务，请稍后再试。", 2
        if time.monotonic() - self.last_run.get(stream_id, -float("inf")) < self.settings.cooldown_seconds:
            return False, "本群仍在分析冷却期，可通过 /群分析历史 重发已生成日报。", 2
        preferences = await self.prefs(stream_id) if self.runtime else {}
        output = str(
            groups.get("output") or preferences.get("output") or ("auto" if self.runtime else self.settings.output)
        )
        self.launch(stream_id, day, output)
        await self.ctx.send.text(f"已排队分析 {day} 的群聊记录，完成后自动发送。", stream_id)
        return True, "群分析已排队", 2

    @Command("daily_analysis_help", pattern=r"^/(?:群分析帮助|daily_analysis_help)$")
    async def handle_help(self, stream_id: str = "", **kwargs: object) -> tuple[bool, str, int]:
        await self.ctx.send.text(
            "/群分析 [YYYY-MM-DD] [image|text|html|markdown|pdf]：分析当前群\n"
            "/群分析历史 [YYYY-MM-DD]：列出最近14份日报或重发指定日报\n"
            "/群分析状态：查看当前聊天流 ID 与任务状态\n"
            "/群漫画、/增量分析、/增量汇总 [YYYY-MM-DD]\n"
            "/增量状态、/查看模板、/设置模板 名称、/设置格式 格式\n"
            "完整配置、模板安装和链路恢复请打开麦麦侧栏群分析工作台。\n"
            "时间以 Asia/Shanghai 为准；自动日报在配置中指定真实聊天流 ID，默认关闭。",
            stream_id,
        )
        return True, "已发送帮助", 2

    @Command("daily_analysis_status", pattern=r"^/(?:群分析状态|daily_analysis_status)$", permission="operator")
    async def handle_status(self, stream_id: str = "", **kwargs: object) -> tuple[bool, str, int]:
        await self.ctx.send.text(
            f"聊天流 ID：{stream_id}\n任务：{'运行中' if stream_id in self.workers else '空闲'}\n"
            f"定时日报：{'已配置' if stream_id in self.settings.scheduled_streams else '未配置'}",
            stream_id,
        )
        return True, "已发送状态", 2

    @Command(
        "daily_analysis_history",
        permission="operator",
        pattern=r"^/(?:群分析历史|daily_analysis_history)(?:\s+(?P<day>\d{4}-\d{2}-\d{2}))?$",
    )
    async def handle_history(self, stream_id: str = "", **kwargs: object) -> tuple[bool, str, int]:
        if not self.store or not await self.valid_stream(stream_id):
            return False, "当前群不可用。", 2
        groups = kwargs.get("matched_groups")
        groups = groups if isinstance(groups, dict) else {}
        if not groups.get("day"):
            days = await asyncio.to_thread(self.store.history, stream_id)
            await self.ctx.send.text("最近日报：\n" + ("\n".join(days) or "暂无日报"), stream_id)
        else:
            report = await asyncio.to_thread(self.store.get, stream_id, str(groups["day"]))
            if not report:
                return False, "未找到本群该日期的日报。", 2
            preferences = await self.prefs(stream_id) if self.runtime else {}
            await self.deliver(
                report, str(preferences.get("output") or ("auto" if self.runtime else self.settings.output))
            )
        return True, "历史日报查询完成", 2

    async def run_job(
        self, stream: str, day: date, output: str, scheduled: bool, mode: str = "daily", delivery_key: str = ""
    ) -> None:
        """执行读取、分析、持久化、投递；取消时同样回收任务状态。"""
        if self.runtime:
            await self.run_engine_job(stream, day, output, scheduled, mode, delivery_key)
            return
        try:
            async with self.capacity:
                if not self.store or not await self.valid_stream(stream):
                    return
                start, end = day_bounds(day)
                end = min(end, time.time())
                raw = await self.ctx.message.get_by_time_in_chat(
                    chat_id=stream,
                    start_time=str(start),
                    end_time=str(end),
                    limit=self.settings.max_messages + 1,
                    limit_mode="latest",
                    filter_mai=True,
                )
                truncated = len(raw) > self.settings.max_messages
                raw = raw[-self.settings.max_messages :]
                messages = []
                seen: set[str] = set()
                for item in raw:
                    msg = normalize_message(item, stream)
                    if msg and start <= msg.timestamp < end:
                        if msg.message_id and msg.message_id in seen:
                            continue
                        seen.add(msg.message_id)
                        messages.append(msg)
                messages.sort(key=lambda msg: msg.timestamp)
                if len(messages) < self.settings.min_messages:
                    await self.ctx.send.text(
                        f"{day} 有效消息 {len(messages)} 条，未达到分析门槛 {self.settings.min_messages}。", stream
                    )
                    if scheduled:
                        await asyncio.to_thread(self.store.finish, stream, day.isoformat(), "skipped")
                    return
                report = Report(stream_id=stream, day=day.isoformat(), statistics=statistics(messages))
                if truncated:
                    report.warnings.append(
                        f"消息超过上限，仅读取最近 {self.settings.max_messages} 条；统计与分析不代表全天全集。"
                    )
                report = await Analyzer(cast(ModelGateway, self.ctx.llm), self.settings).analyze(report, messages)
                await asyncio.to_thread(self.store.save, report)
                # 卸载/配置禁用后不继续投递。
                if not await self.valid_stream(stream):
                    return
                await self.deliver(report, output)
                if scheduled:
                    await asyncio.to_thread(self.store.finish, stream, day.isoformat(), "sent")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.ctx.logger.exception("群分析任务失败，聊天流 %s 日期 %s", stream, day)
            if scheduled and self.store:
                await asyncio.to_thread(self.store.finish, stream, day.isoformat(), "failed")
            try:
                await self.ctx.send.text("群分析未完成，请检查插件日志。已生成的历史日报不会丢失。", stream)
            except Exception:
                self.ctx.logger.exception("群分析失败通知未能发送")
        finally:
            self.workers.pop(stream, None)

    async def deliver(self, report: Report, output: str) -> None:
        """图片失败自动降级文本，所有发送结果均核对成功标志。"""
        if output == "auto":
            formats = self.engine().config_manager.get_output_format()
            for selected in formats or [self.settings.output]:
                await self.deliver(report, selected)
            return
        if self.runtime and report.engine_payload:
            try:
                preferences = await self.prefs(report.stream_id)
                path = await self.runtime.render(report, output, str(preferences.get("template") or ""))
                await self.send_artifact(report, path, output)
                return
            except Exception:
                if output != "image":
                    raise
                self.ctx.logger.exception("原模板渲染失败，降级文本日报")
                output = "text"
        if output == "image":
            try:
                card = await self.ctx.render.html2png(
                    html_report(report),
                    viewport={"width": 900, "height": 600},
                    full_page=True,
                    allow_network=False,
                )
                if card.get("image_base64") and await self.ctx.send.image(card["image_base64"], report.stream_id):
                    return
            except Exception:
                self.ctx.logger.exception("图片报告失败，降级文本")
        text = text_report(report)
        for offset in range(0, len(text), 1800):
            if not await self.ctx.send.text(text[offset : offset + 1800], report.stream_id):
                raise RuntimeError("文本报告投递失败")

    async def schedule_loop(self) -> None:
        """每分钟检查上海时间，重启后通过持久占位记录避免重复日报。"""
        while True:
            try:
                now = datetime.now(SHANGHAI)
                if self.pending_reload and not self.workers and self.store:
                    if self.runtime:
                        await self.runtime.close()
                    self.runtime = EngineRuntime(self.ctx, self.settings, self.store)
                    await self.runtime.start()
                    self.pending_reload = False
                if self.store and self.settings.plugin.enabled and now.strftime("%H:%M") >= self.settings.send_at:
                    day = now.date() - timedelta(days=1)
                    for stream in self.settings.scheduled_streams:
                        if stream in self.workers or not await self.valid_stream(stream):
                            continue
                        if await asyncio.to_thread(self.store.claim, stream, day.isoformat()):
                            self.launch(stream, day, "auto", scheduled=True)
                if self.runtime and self.settings.plugin.enabled:
                    await self.schedule_original(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("定时群分析检查失败，下轮继续")
            await asyncio.sleep(60)

    async def run_engine_job(
        self, stream: str, day: date, output: str, scheduled: bool, mode: str, delivery_key: str
    ) -> None:
        state = "failed"
        engine = self.engine()
        try:
            async with self.capacity:
                if not await self.valid_stream(stream):
                    state = "skipped"
                    return
                report = await engine.analyze(stream, day, mode, manual=not scheduled)
                await self.deliver(report, output)
                state = "sent"
                cfg = engine.config_manager
                item = engine.bot_manager.streams[stream]
                target = f"{item.get('platform', 'qq')}:GroupMessage:{item.get('group_id', stream)}"
                if (
                    mode != "incremental"
                    and cfg.get_enable_daily_comic()
                    and cfg.get_enable_auto_daily_comic()
                    and cfg.is_comic_group_allowed(target)
                ):
                    try:
                        data, _ = await engine.comic(report)
                        if data:
                            import base64

                            if not await self.ctx.send.image(base64.b64encode(data).decode(), stream):
                                self.ctx.logger.error("日报已投递，但自动漫画投递失败：%s", stream)
                    except Exception:
                        self.ctx.logger.exception("日报已投递，但自动漫画未完成：%s", stream)
        except IncrementalBatchSaved:
            state = "batched"
        except AnalysisSkipped:
            state = "skipped"
        except asyncio.CancelledError:
            state = "aborted"
            raise
        except Exception:
            self.ctx.logger.exception("完整引擎任务失败：群=%s 日期=%s 模式=%s", stream, day, mode)
            if not scheduled:
                await self.ctx.send.text("任务未完成，后台链路页保留了错误和可恢复检查点。", stream)
        finally:
            if scheduled and self.store:
                await asyncio.to_thread(self.store.finish, stream, delivery_key or day.isoformat(), state)
            self.workers.pop(stream, None)

    async def schedule_original(self, now: datetime) -> None:
        """兼容原多时刻定时名单和增量名单；每个时隙持久去重。"""
        engine = self.engine()
        await engine.bot_manager.refresh()
        cfg = engine.config_manager
        interval = self.settings.incremental_interval_minutes * 60
        for stream, item in engine.bot_manager.streams.items():
            if stream in self.workers or not await self.valid_stream(stream):
                continue
            target = f"{item.get('platform', 'qq')}:GroupMessage:{item.get('group_id', stream)}"
            incremental = stream in self.settings.incremental_streams or (
                cfg.get_incremental_enabled() and cfg.is_incremental_group_allowed(target)
            )
            if incremental:
                key = f"incremental/{int(now.timestamp()) // interval}"
                if await asyncio.to_thread(engine.store.claim, stream, key):
                    self.launch(stream, now.date(), "auto", True, "incremental", key)
                    continue
            if cfg.get_enable_auto_analysis() and cfg.is_scheduled_group_allowed(target):
                for slot in cfg.get_auto_analysis_time():
                    if now.strftime("%H:%M") < slot:
                        continue
                    key = f"schedule/{now.date()}/{slot}"
                    if await asyncio.to_thread(engine.store.claim, stream, key):
                        self.launch(stream, now.date(), "auto", True, "final" if incremental else "daily", key)
                        break


def create_plugin() -> DailyAnalysisPlugin:
    """返回 SDK 插件实例。"""
    return DailyAnalysisPlugin()
