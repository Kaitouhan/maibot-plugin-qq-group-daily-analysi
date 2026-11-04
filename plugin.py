"""MaiBot 1.3.15 群日常分析插件入口。"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from typing import cast

from maibot_sdk import Command, MaiBotPlugin

from .analysis import Analyzer, ModelGateway
from .configuration import AnalysisConfig
from .domain import SHANGHAI, Report, day_bounds, normalize_message, statistics
from .reporting import html_report, text_report
from .storage import Store


class DailyAnalysisPlugin(MaiBotPlugin):
    """通过宿主 RPC 读取记录和调用模型，不导入 AstrBot 或麦麦内部模块。"""

    config_model = AnalysisConfig

    def __init__(self) -> None:
        super().__init__()
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.last_run: dict[str, float] = {}
        self.scheduler: asyncio.Task[None] | None = None
        self.store: Store | None = None
        self.capacity = asyncio.Semaphore(2)

    @property
    def settings(self) -> AnalysisConfig:
        return cast(AnalysisConfig, self.config)

    async def on_load(self) -> None:
        self.store = Store(self.ctx.paths.data_dir)
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

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        # SDK 负责替换强类型配置；调度循环每轮读取新配置。
        self.ctx.logger.info("群分析配置已更新：%s %s", scope, version)

    async def valid_stream(self, stream: str) -> bool:
        """仅允许真实 QQ 群聊天流，并应用插件白名单。"""
        if not self.settings.plugin.enabled or (
            self.settings.allowed_streams and stream not in self.settings.allowed_streams
        ):
            return False
        streams = await self.ctx.chat.get_group_streams(platform="qq")
        return any(str(item.get("stream_id") or item.get("session_id")) == stream for item in streams)

    def launch(self, stream: str, day: date, output: str, scheduled: bool = False) -> None:
        self.workers[stream] = asyncio.create_task(self.run_job(stream, day, output, scheduled))
        self.last_run[stream] = time.monotonic()

    @Command(
        "daily_analysis",
        description="分析当前群聊的自然日记录",
        permission="operator",
        pattern=r"^/(?:群分析|daily_analysis)(?:\s+(?P<day>\d{4}-\d{2}-\d{2}))?(?:\s+(?P<output>text|image))?$",
    )
    async def handle_analysis(self, stream_id: str = "", **kwargs: object) -> tuple[bool, str, int]:
        """快速排队并返回；避免长报告超过 Command 处理超时。"""
        if not await self.valid_stream(stream_id):
            return False, "仅支持已授权的 QQ 群聊。", 2
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
        output = str(groups.get("output") or self.settings.output)
        self.launch(stream_id, day, output)
        await self.ctx.send.text(f"已排队分析 {day} 的群聊记录，完成后自动发送。", stream_id)
        return True, "群分析已排队", 2

    @Command("daily_analysis_help", pattern=r"^/(?:群分析帮助|daily_analysis_help)$")
    async def handle_help(self, stream_id: str = "", **kwargs: object) -> tuple[bool, str, int]:
        await self.ctx.send.text(
            "/群分析 [YYYY-MM-DD] [text|image]：分析当前群（操作员权限）\n"
            "/群分析历史 [YYYY-MM-DD]：列出最近14份日报或重发指定日报\n"
            "/群分析状态：查看当前聊天流 ID 与任务状态\n"
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
            await self.deliver(report, self.settings.output)
        return True, "历史日报查询完成", 2

    async def run_job(self, stream: str, day: date, output: str, scheduled: bool) -> None:
        """执行读取、分析、持久化、投递；取消时同样回收任务状态。"""
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
                if self.store and self.settings.plugin.enabled and now.strftime("%H:%M") >= self.settings.send_at:
                    day = now.date() - timedelta(days=1)
                    for stream in self.settings.scheduled_streams:
                        if stream in self.workers or not await self.valid_stream(stream):
                            continue
                        if await asyncio.to_thread(self.store.claim, stream, day.isoformat()):
                            self.launch(stream, day, self.settings.output, scheduled=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("定时群分析检查失败，下轮继续")
            await asyncio.sleep(60)


def create_plugin() -> DailyAnalysisPlugin:
    """返回 SDK 插件实例。"""
    return DailyAnalysisPlugin()
