"""上游引擎与麦麦 SDK 之间的显式兼容边界，不注册伪造 AstrBot 模块。"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, time, timedelta, timezone
from datetime import datetime as StandardDatetime
from pathlib import Path
from types import SimpleNamespace

from maibot_sdk import PluginContext

SHANGHAI = timezone(timedelta(hours=8))
REPORT_DAY: ContextVar[date | None] = ContextVar("daily_report_day", default=None)


class LocalDatetime(StandardDatetime):
    @classmethod
    def now(cls, tz=None):
        return super().now(tz or SHANGHAI)

    @classmethod
    def fromtimestamp(cls, timestamp, tz=None):
        return super().fromtimestamp(timestamp, tz or SHANGHAI)


local_datetime_module = SimpleNamespace(
    datetime=LocalDatetime, date=date, time=time, timedelta=timedelta, timezone=timezone, UTC=timezone.utc
)


def report_datetime() -> StandardDatetime:
    day = REPORT_DAY.get()
    return StandardDatetime.combine(day, time.min, SHANGHAI) if day else LocalDatetime.now()


def report_window_end() -> float:
    day = REPORT_DAY.get()
    now = LocalDatetime.now().timestamp()
    return min(now, StandardDatetime.combine(day + timedelta(days=1), time.min, SHANGHAI).timestamp()) if day else now


class RuntimePaths:
    """让上游持久化路径始终落在 Host 授予的目录。"""

    root: Path | None = None

    @classmethod
    def get_data_dir(cls, name: str = "") -> Path:
        if cls.root is None:
            raise RuntimeError("上游引擎数据目录尚未绑定")
        cls.root.mkdir(parents=True, exist_ok=True)
        return cls.root


@dataclass
class LLMResponse:
    role: str = "assistant"
    completion_text: str = ""
    usage: object = None
    raw_completion: object = None
    is_chunk: bool = False


@dataclass
class ProviderMeta:
    id: str


class ModelProvider:
    def __init__(self, bridge: ModelContext, model: str) -> None:
        self.bridge = bridge
        self.model = model
        self.provider_config: dict[str, object] = {"model": model, "type": "maibot", "timeout": 120}

    def meta(self) -> ProviderMeta:
        return ProviderMeta(self.model or "utils")

    async def text_chat_stream(
        self, prompt: str = "", system_prompt: str | None = None, **kwargs: object
    ) -> AsyncIterator[LLMResponse]:
        # Host 2.16 的模型能力返回最终响应；上游流式消费者仍得到一个完整结果。
        yield await self.bridge.llm_generate(
            chat_provider_id=self.model, prompt=prompt, system_prompt=system_prompt, **kwargs
        )


class ModelContext:
    """把上游 Provider 选择、独立模型和人格提示映射为麦麦模型调用。"""

    def __init__(self, ctx: PluginContext, default_model: str = "") -> None:
        self.ctx = ctx
        self.default_model = default_model
        self.persona_manager = PersonaManager(ctx)
        self.conversation_manager = None
        self.external_drawing_api = ""

    async def generate_external_image(
        self, prompt: str, images: list[tuple[bytes, str]] | None, backend: str = "general_plugin"
    ) -> bytes | None:
        if not self.external_drawing_api:
            return None
        result = await self.ctx.api.call(
            self.external_drawing_api,
            prompt=prompt,
            reference_images=[base64.b64encode(data).decode() for data, _ in images or []],
            backend=backend,
        )
        if isinstance(result, dict) and result.get("image_base64"):
            return base64.b64decode(result["image_base64"])
        return None

    def get_provider_by_id(self, provider_id: str = "") -> ModelProvider:
        return ModelProvider(self, provider_id or self.default_model or "utils")

    def get_all_providers(self) -> list[ModelProvider]:
        return [self.get_provider_by_id(self.default_model)]

    async def get_current_chat_provider_id(self, umo: str = "") -> str:
        return self.default_model or "utils"

    async def llm_generate(
        self, chat_provider_id: str = "", prompt: str = "", system_prompt: str | None = None, **kwargs: object
    ) -> LLMResponse:
        messages = (
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
            if system_prompt
            else prompt
        )
        temperature = kwargs.get("temperature")
        result = await self.ctx.llm.generate(
            messages,
            model=chat_provider_id or self.default_model,
            max_tokens=8192,
            temperature=float(temperature) if isinstance(temperature, (int, float)) else None,
        )
        if not result.get("success"):
            raise RuntimeError("麦麦模型调用失败")
        return LLMResponse(
            completion_text=str(result.get("response", "")), usage=result.get("usage"), raw_completion=result
        )


@dataclass
class Persona:
    system_prompt: str


class PersonaManager:
    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.profiles: dict[str, str] = {}

    async def get_persona(self, key: str) -> Persona:
        if key not in self.profiles:
            raise ValueError(f"请在插件人格映射中配置：{key}")
        return Persona(self.profiles[key])

    async def get_default_persona_v3(self, umo: str = "") -> dict[str, str]:
        prompt = await self.ctx.config.get("personality.personality", "")
        style = await self.ctx.config.get("personality.reply_style", "")
        return {"prompt": "\n".join(str(value) for value in (prompt, style) if value)}


class Preferences:
    async def get_async(self, **kwargs: object) -> dict:
        return {}


preferences = Preferences()


def schema_defaults(schema: dict) -> dict:
    """按上游配置 Schema 提取所有分组的真实默认值。"""
    result = {}
    for key, item in schema.items():
        if item.get("type") == "object":
            result[key] = schema_defaults(item.get("items", {}))
        elif "default" in item:
            result[key] = item["default"]
        else:
            result[key] = [] if item.get("type") in {"list", "template_list"} else ""
    return result


def merge_config(base: dict, override: dict) -> dict:
    """递归合并，保留用户已设值与未来上游新增默认字段。"""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_config(base[key], value)
        else:
            base[key] = value
    return base


class NativeConfig(dict):
    """保留上游配置迁移与升级备份协议，存储于数据目录。"""

    def __init__(self, source: Path, path: Path, overrides: dict | None = None) -> None:
        self.schema = json.loads((source / "_conf_schema.json").read_text(encoding="utf-8"))
        self.path = path
        self.first_deploy = not path.exists()
        values = schema_defaults(self.schema)
        if path.exists():
            merge_config(values, json.loads(path.read_text(encoding="utf-8")))
        merge_config(values, overrides or {})
        super().__init__(values)

    def save_config(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
