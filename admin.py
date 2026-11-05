"""麦麦原生后台、管理命令与受控异步操作。"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import cast

import aiohttp
from maibot_sdk import API, Command, MaiBotPlugin

from .configuration import AnalysisConfig
from .domain import SHANGHAI, Report
from .full_runtime import EngineRuntime, IncrementalBatchSaved
from .storage import Store
from .vendor.upstream.src.domain.value_objects import AnalysisResultPayload
from .vendor.upstream.src.infrastructure.logging.plugin_log_buffer import global_log_buffer
from .vendor.upstream.src.infrastructure.reporting.template_installer import (
    install_template_from_github_url,
    install_template_from_zip,
    uninstall_template,
    validate_template_name,
)
from .vendor.upstream.src.infrastructure.reporting.template_resources import TemplateResourceStore
from .vendor.upstream.src.infrastructure.reporting.typst_fonts import download_typst_fonts, typst_font_status


def mask_secrets(value: object) -> object:
    """后台响应不回传已保存的 API Key 或 Token。"""
    if isinstance(value, dict):
        return {
            key: (
                "********"
                if any(term in str(key).lower() for term in ("api_key", "token", "password", "secret")) and val
                else mask_secrets(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def preserve_secrets(old: object, new: object) -> object:
    if new == "********":
        return old
    if isinstance(new, dict):
        previous = old if isinstance(old, dict) else {}
        return {key: preserve_secrets(previous.get(key), val) for key, val in new.items()}
    if isinstance(new, list):
        previous = old if isinstance(old, list) else []
        result = []
        for index, item in enumerate(new):
            match = next(
                (
                    v
                    for v in previous
                    if isinstance(v, dict)
                    and isinstance(item, dict)
                    and item.get("name")
                    and v.get("name") == item["name"]
                ),
                None,
            )
            result.append(
                preserve_secrets(
                    match if match is not None else previous[index] if index < len(previous) else None, item
                )
            )
        return result
    return new


class AdminMixin(MaiBotPlugin):
    runtime: EngineRuntime | None
    store: Store | None
    workers: dict[str, asyncio.Task[None]]
    admin_status: dict[str, dict]

    @property
    def settings(self) -> AnalysisConfig:
        return cast(AnalysisConfig, self.config)

    def engine(self) -> EngineRuntime:
        if self.runtime is None:
            raise RuntimeError("完整引擎尚未启动，请检查插件初始化日志")
        return self.runtime

    async def is_allowed(self, stream: str) -> bool:
        if not self.settings.plugin.enabled or (
            self.settings.allowed_streams and stream not in self.settings.allowed_streams
        ):
            return False
        rows = await self.ctx.chat.get_group_streams(platform="all_platforms")
        for item in rows:
            if str(item.get("stream_id") or item.get("session_id")) != stream:
                continue
            platform = str(item.get("platform", "qq"))
            if "all" not in self.settings.platforms and platform not in self.settings.platforms:
                return False
            target = f"{platform}:GroupMessage:{item.get('group_id', stream)}"
            return self.runtime.config_manager.is_group_allowed(target) if self.runtime else True
        return False

    async def prefs(self, stream: str) -> dict:
        value = await self.engine().get_kv_data("native_preferences_" + stream, {})
        return value if isinstance(value, dict) else {}

    async def send_artifact(self, report: Report, path: Path, output: str) -> None:
        engine = self.engine()
        await engine.bot_manager.refresh()
        platform = str(engine.bot_manager.streams[report.stream_id].get("platform", "qq"))
        adapter = engine.bot_manager.get_adapter(platform)
        if not adapter:
            raise ValueError("找不到当前平台的发送接口")
        if output == "image":
            if not await adapter.send_image(report.stream_id, str(path)):
                raise RuntimeError("图片投递失败")
        elif output in {"text", "markdown"}:
            text = path.read_text(encoding="utf-8")
            for offset in range(0, len(text), 1800):
                if not await adapter.send_text(report.stream_id, text[offset : offset + 1800]):
                    raise RuntimeError("文本投递失败")
        elif not await adapter.send_file(report.stream_id, path):
            raise RuntimeError("该平台未确认文件投递，请在后台查看已保存产物")
        if adapter.onebot:
            cfg = engine.config_manager
            group = str(adapter.streams[report.stream_id]["group_id"])
            if cfg.get_enable_group_file_upload() and output == "image":
                folder = await adapter.onebot.find_or_create_folder(group, cfg.get_group_file_folder())
                await adapter.send_file(report.stream_id, path, folder or "")
            if cfg.get_enable_group_album_upload() and output == "image":
                await adapter.onebot.upload_group_album(
                    group,
                    str(path),
                    album_name=cfg.get_group_album_name(),
                    strict_mode=cfg.get_group_album_strict_mode(),
                )

    async def queue_operation(
        self,
        operation: str,
        stream_id: str = "",
        day: str = "",
        value: str = "",
        template: str = "",
        output: str = "image",
        trace_id: str = "",
    ) -> dict:
        allowed = {
            "daily",
            "comic",
            "incremental",
            "final",
            "rerender",
            "resume",
            "fonts",
            "resources",
            "template_install",
            "probe",
        }
        if operation not in allowed:
            raise ValueError("不支持的操作")
        self.engine()
        if operation not in {"fonts", "resources", "template_install", "probe"} and not await self.is_allowed(
            stream_id
        ):
            raise PermissionError("目标必须是允许的真实群聊流")
        if output not in {"image", "text", "markdown", "html", "pdf"}:
            raise ValueError("无效的报告格式")
        selected = date.fromisoformat(day) if day else datetime.now(SHANGHAI).date()
        if selected > datetime.now(SHANGHAI).date():
            raise ValueError("不能分析未来日期")
        key = stream_id or "@resources"
        if key in self.workers or len(self.workers) >= 8:
            raise ValueError("目标已有任务或队列已满，请等待当前任务完成")
        identifier = uuid.uuid4().hex[:16]
        self.admin_status[identifier] = {
            "id": identifier,
            "operation": operation,
            "stream_id": stream_id,
            "day": selected.isoformat(),
            "status": "queued",
            "started_at": time.time(),
            "message": "已排队",
        }
        if len(self.admin_status) > 100:
            oldest = next(iter(self.admin_status))
            del self.admin_status[oldest]
        self.workers[key] = asyncio.create_task(
            self.admin_job(identifier, key, operation, stream_id, selected, value, template, output, trace_id)
        )
        return {"job_id": identifier, "message": "已排队，可在任务页查看进度"}

    async def admin_job(
        self,
        identifier: str,
        key: str,
        operation: str,
        stream: str,
        day: date,
        value: str,
        template: str,
        output: str,
        trace_id: str,
    ) -> None:
        job = self.admin_status[identifier]
        job["status"] = "running"
        engine = self.engine()
        try:
            if operation == "fonts":
                result = await download_typst_fonts(
                    engine.plugin_data_dir,
                    engine.config_manager.get_t2i_cdn_base_url(),
                    theme=template or self.settings.report_template,
                )
                job["message"] = json.dumps(result, ensure_ascii=False)
            elif operation == "resources":
                resource = self.resource_store()
                async with aiohttp.ClientSession() as session:
                    for group in resource.status():
                        if template and group["theme"] != template:
                            continue
                        for item in group["files"]:
                            job["message"] = f"正在准备 {item['name']}"
                            await resource.ensure(item["name"], session)
                job["message"] = "模板资源准备完成"
            elif operation == "template_install":
                if value.startswith("https://github.com/"):
                    result = await install_template_from_github_url(value, name=template or None)
                elif value.startswith("@upload:"):
                    identifier = value.removeprefix("@upload:")
                    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
                        raise ValueError("模板上传标识无效")
                    uploaded = engine.plugin_data_dir / "template_upload" / f"{identifier}.zip"
                    try:
                        result = await asyncio.to_thread(
                            install_template_from_zip, uploaded.read_bytes(), name=template or None
                        )
                    finally:
                        uploaded.unlink(missing_ok=True)
                else:
                    if len(value) > 6 * 1024 * 1024:
                        raise ValueError("模板压缩包过大")
                    result = await asyncio.to_thread(
                        install_template_from_zip, base64.b64decode(value, validate=True), name=template or None
                    )
                job["message"] = json.dumps(result, ensure_ascii=False)
            elif operation == "probe":
                job["message"] = "渲染服务探测中"
                data = await engine.html_render(
                    "<html><body><h1>麦麦群分析渲染测试</h1><p>中文 ABC 123</p></body></html>"
                )
                destination = engine.plugin_data_dir / "reports" / f"probe_{identifier}.png"
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(data)
                job["artifact"] = str(destination.relative_to(engine.plugin_data_dir))
                job["message"] = "渲染测试成功"
            else:
                report = None
                if operation in {"rerender", "comic"}:
                    report = await asyncio.to_thread(engine.store.get, stream, day.isoformat())
                if operation == "resume":
                    saved = await asyncio.to_thread(engine.trace_store.get_trace, trace_id)
                    if not saved or saved.get("group_id") != stream:
                        raise ValueError("检查点与所选群不一致")
                    result = await engine.resume_analysis(
                        trace_id,
                        stream,
                        str(engine.bot_manager.streams[stream].get("platform", "qq")),
                        date_str=day.isoformat(),
                    )
                    if not result.get("success"):
                        raise RuntimeError(str(result.get("error") or result.get("reason")))
                    payload = result.get("analysis_result")
                    if isinstance(payload, dict):
                        report = engine.to_report(stream, day, cast(AnalysisResultPayload, payload))
                if report is None:
                    if operation == "rerender":
                        raise ValueError("没有该日期的完整分析产物，请先生成日报")
                    report = await engine.analyze(
                        stream, day, mode=operation if operation in {"incremental", "final"} else "daily"
                    )
                if operation == "comic":
                    image, url = await engine.comic(report)
                    if image is None:
                        raise RuntimeError(
                            "漫画未生成，请检查绘图供应商、角色和参考图配置" + (f"；可用地址：{url}" if url else "")
                        )
                    path = engine.plugin_data_dir / "reports" / f"comic_{identifier}.png"
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(image)
                    await self.send_artifact(report, path, "image")
                else:
                    path = await engine.render(report, output, template)
                    await self.send_artifact(report, path, output)
                job["artifact"] = str(path.relative_to(engine.plugin_data_dir))
                job["message"] = "任务完成并投递"
            job["status"] = "succeeded"
        except IncrementalBatchSaved:
            job.update(status="succeeded", message="增量批次已保存，最终日报可使用增量汇总生成")
        except asyncio.CancelledError:
            job.update(status="aborted", message="任务已取消")
            raise
        except Exception as error:
            self.ctx.logger.exception("后台任务失败：%s", operation)
            job.update(status="failed", message=str(error))
        finally:
            job["completed_at"] = time.time()
            self.workers.pop(key, None)

    def resource_store(self) -> TemplateResourceStore:
        engine = self.engine()
        return TemplateResourceStore(engine.plugin_data_dir, engine.config_manager.get_template_assets_base_url())

    @API("panel_query", version="1")
    async def panel_query(
        self,
        section: str = "overview",
        stream_id: str = "",
        day: str = "",
        trace_id: str = "",
        page: int = 1,
        search: str = "",
    ) -> dict:
        engine = self.engine()
        if not 1 <= page <= 10000:
            raise ValueError("页码无效")
        if section == "overview":
            metrics = await asyncio.to_thread(engine.trace_store.get_metrics_summary)
            return {
                "metrics": metrics,
                "jobs": list(self.admin_status.values())[-20:],
                "active": list(self.workers),
                "version": "1.0.0",
                "source": "AstrBot v5.9.4",
            }
        if section == "groups":
            await engine.bot_manager.refresh()
            return {
                "rows": [{"stream_id": key, **val} for key, val in engine.bot_manager.streams.items()],
                "total": len(engine.bot_manager.streams),
            }
        if section == "templates":
            return {
                "rows": engine.templates(),
                "fonts": typst_font_status(engine.plugin_data_dir),
                "resources": self.resource_store().status(),
            }
        if section == "config":
            return {"schema": engine.native_config.schema, "values": mask_secrets(dict(engine.native_config))}
        if section == "references":
            return {"rows": await engine.get_kv_data("reference_images", [])}
        if section == "traces":
            rows, total = await asyncio.to_thread(
                engine.trace_store.list_traces,
                limit=20,
                offset=(page - 1) * 20,
                group_id=stream_id or None,
                search=search or None,
            )
            return {"rows": rows, "total": total, "page": page}
        if section == "trace":
            trace = await asyncio.to_thread(engine.trace_store.get_trace, trace_id)
            return {"trace": trace, "logs": global_log_buffer.get_trace_logs(trace_id)}
        if section == "logs":
            rows, total = global_log_buffer.query(limit=100, offset=(page - 1) * 100, search=search or None)
            return {"rows": rows, "total": total, "page": page}
        if section == "history":
            with engine.store.connect() as db:
                rows = db.execute(
                    "SELECT stream, day FROM reports WHERE (?='' OR stream=?) ORDER BY day DESC LIMIT 20 OFFSET ?",
                    (stream_id, stream_id, (page - 1) * 20),
                ).fetchall()
            return {"rows": [{"stream_id": stream, "day": recorded_day} for stream, recorded_day in rows]}
        if section == "report":
            report = await asyncio.to_thread(engine.store.get, stream_id, day)
            return {"report": report.model_dump(exclude={"engine_payload"}) if report else None}
        if section == "incremental":
            return {"rows": await engine.incremental_store.get_all_batch_summaries(stream_id)}
        if section == "artifact":
            return await self.artifact_data(trace_id, page - 1)
        raise ValueError("未知查询")

    @API("template_chunk", version="1")
    async def template_chunk(self, identifier: str, index: int, total: int, data: str) -> dict:
        """在 WebUI JSON 限额内分块接收 ZIP；不接受客户端路径。"""
        if not re.fullmatch(r"[a-f0-9]{32}", identifier) or not 1 <= total <= 128 or not 0 <= index < total:
            raise ValueError("无效上传序号")
        chunk = base64.b64decode(data, validate=True)
        if not chunk or len(chunk) > 36 * 1024:
            raise ValueError("分块大小无效")
        directory = self.engine().plugin_data_dir / "template_upload"
        directory.mkdir(exist_ok=True)
        # 清理过期临时文件并限制并发上传占用。
        for old in directory.glob("*.part"):
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink()
        path = directory / f"{identifier}.part"
        if index == 0:
            if len(list(directory.iterdir())) >= 8:
                raise ValueError("临时上传过多，请等待或清理后重试")
            path.write_bytes(b"")
        if not path.is_file() or path.stat().st_size != index * 36 * 1024:
            raise ValueError("上传顺序不连续，请重新上传")
        with path.open("ab") as stream:
            stream.write(chunk)
        if index == total - 1:
            path.replace(path.with_suffix(".zip"))
        return {"received": index + 1, "value": "@upload:" + identifier}

    async def artifact_data(self, identifier: str, chunk_index: int = 0) -> dict:
        job = self.admin_status.get(identifier)
        if not job or not job.get("artifact"):
            raise ValueError("找不到任务产物")
        root = self.engine().plugin_data_dir.resolve()
        path = root / job["artifact"]
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("产物路径无效")
        length = path.stat().st_size
        if length > 64 * 1024 * 1024:
            raise ValueError("产物超过预览限制")
        with path.open("rb") as stream:
            stream.seek(chunk_index * 128 * 1024)
            data = stream.read(128 * 1024)
        return {
            "name": path.name,
            "size": length,
            "chunk": base64.b64encode(data).decode(),
            "chunks": (length + 128 * 1024 - 1) // (128 * 1024),
        }

    @API("panel_action", version="1")
    async def panel_action(
        self,
        operation: str,
        stream_id: str = "",
        day: str = "",
        value: str = "",
        template: str = "",
        output: str = "image",
        trace_id: str = "",
    ) -> dict:
        if operation == "cancel":
            task = self.workers.get(stream_id or "@resources")
            if task:
                task.cancel()
            return {"message": "已请求取消"}
        if operation == "save_config":
            if self.workers:
                raise ValueError("请等待当前任务结束后修改高级配置")
            return self.save_config_value(template, value)
        return await self.queue_operation(operation, stream_id, day, value, template, output, trace_id)

    def save_config_value(self, path: str, encoded_value: str) -> dict:
        engine = self.engine()
        parts = path.split(".")
        if len(parts) != 2:
            raise ValueError("配置路径必须为 分组.字段")
        group, key = parts
        schema = engine.native_config.schema
        definition = schema.get(group, {}).get("items", {}).get(key)
        if not definition:
            raise ValueError("配置项不存在")
        value = preserve_secrets(engine.native_config[group].get(key), json.loads(encoded_value))
        expected = {
            "string": str,
            "text": str,
            "int": int,
            "float": (int, float),
            "bool": bool,
            "list": list,
            "template_list": list,
        }
        kind = definition.get("type")
        if kind in expected and not isinstance(value, expected[kind]):
            raise ValueError("配置值的类型与原配置 Schema 不一致")
        engine.native_config[group][key] = value
        engine.config_manager.save_config()
        return {"message": "配置已保存；密钥保持隐藏"}

    @API("panel_delete", version="1")
    async def panel_delete(self, operation: str, stream_id: str = "", day: str = "", value: str = "") -> dict:
        engine = self.engine()
        if self.workers:
            raise ValueError("有任务运行时不能清理数据")
        if operation == "report":
            with engine.store.connect() as db:
                db.execute("DELETE FROM reports WHERE stream=? AND day=?", (stream_id, day))
            return {"message": "所选日报缓存已删除"}
        if operation == "template":
            result = await asyncio.to_thread(uninstall_template, validate_template_name(value))
            return {"message": json.dumps(result, ensure_ascii=False)}
        if operation == "trace":
            await asyncio.to_thread(engine.trace_store.delete_trace, value)
            return {"message": "所选链路已删除"}
        raise ValueError("不支持的清理操作")

    @API("receive_reference", version="1")
    async def receive_reference(self, upload_id: str, label: str = "") -> dict:
        upload = await self.ctx.webui.claim_upload(upload_id)
        root = self.engine().plugin_data_dir.resolve()
        path = Path(upload["path"])
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("上传文件不属于本插件")
        assets = await self.engine().get_kv_data("reference_images", [])
        if not isinstance(assets, list):
            assets = []
        assets.append({"name": label or path.name, "path": str(path), "sha256": upload["sha256"]})
        await self.engine().put_kv_data("reference_images", assets)
        return {"message": "参考图已保存，可将路径填入漫画角色方案", "path": str(path)}

    @Command(
        "full_analysis_operations",
        permission="operator",
        pattern=r"^/(?P<operation>群漫画|增量分析|增量汇总|增量状态|查看模板|设置模板|设置格式|分析设置)(?:\s+(?P<value>.*))?$",
    )
    async def handle_full_operations(self, stream_id: str = "", **kwargs: object) -> tuple[bool, str, int]:
        if not await self.is_allowed(stream_id):
            return False, "当前群未授权", 2
        groups = kwargs.get("matched_groups")
        groups = groups if isinstance(groups, dict) else {}
        operation, value = str(groups.get("operation", "")), str(groups.get("value") or "").strip()
        try:
            if operation == "查看模板":
                templates = self.engine().templates()
                message = "可用模板：\n" + "\n".join(str(row["id"]) for row in templates)
            elif operation in {"设置模板", "设置格式"}:
                prefs = await self.prefs(stream_id)
                if operation == "设置模板":
                    validate_template_name(value)
                    if value not in {row["id"] for row in self.engine().templates()}:
                        raise ValueError("模板不存在，请先运行 /查看模板")
                    prefs["template"] = value
                else:
                    aliases = {"图片": "image", "文本": "text", "网页": "html", "PDF": "pdf"}
                    value = aliases.get(value, value)
                    if value not in {"image", "text", "markdown", "html", "pdf"}:
                        raise ValueError("支持 image/text/markdown/html/pdf")
                    prefs["output"] = value
                await self.engine().put_kv_data("native_preferences_" + stream_id, prefs)
                message = "本群设置已保存"
            elif operation == "增量状态":
                data = await self.panel_query("incremental", stream_id=stream_id)
                message = json.dumps(data, ensure_ascii=False)[:1600]
            elif operation == "分析设置":
                message = (
                    "本群偏好："
                    + json.dumps(await self.prefs(stream_id), ensure_ascii=False)
                    + "\n完整配置请打开麦麦后台 → 群分析工作台。"
                )
            else:
                selected = {"群漫画": "comic", "增量分析": "incremental", "增量汇总": "final"}[operation]
                prefs = await self.prefs(stream_id)
                result = await self.queue_operation(
                    selected,
                    stream_id,
                    day=value,
                    template=str(prefs.get("template", "")),
                    output=str(prefs.get("output", self.settings.output)),
                )
                message = str(result["message"])
            await self.ctx.send.text(message, stream_id)
            return True, message, 2
        except Exception as error:
            return False, str(error), 2
