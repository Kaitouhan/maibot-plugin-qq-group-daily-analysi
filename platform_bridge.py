"""多平台历史、头像、发送与可选 OneBot 扩展的麦麦适配。"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

import httpx
from maibot_sdk import PluginContext

from .domain import normalize_message
from .vendor.upstream.src.domain.value_objects import (
    MessageContent,
    MessageContentType,
    UnifiedGroup,
    UnifiedMember,
    UnifiedMessage,
)
from .vendor.upstream.src.domain.value_objects.platform_capabilities import (
    DISCORD_CAPABILITIES,
    ONEBOT_V11_CAPABILITIES,
    TELEGRAM_CAPABILITIES,
)
from .vendor.upstream.src.infrastructure.platform.adapters.onebot_adapter import OneBotAdapter

WINDOW: ContextVar[tuple[float, float] | None] = ContextVar("daily_analysis_window", default=None)


class OneBotHTTP:
    """使用用户配置的 OneBot HTTP API，复用上游完整分页及群文件/相册逻辑。"""

    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token

    async def call_action(self, action: str, **params: object) -> object:
        if action == "get_group_msg_history":
            for key in ("group_id", "message_id"):
                if key in params:
                    params[key] = int(str(params[key]))
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.endpoint}/{action}", json=params, headers=headers)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or body.get("status") == "failed" or body.get("retcode") != 0:
            detail = str(body.get("message") or body.get("wording") or "") if isinstance(body, dict) else "无效信封"
            raise RuntimeError(f"OneBot {action} 调用失败：{detail}")
        return body.get("data", body)

    async def stage_file(self, path: Path) -> str:
        """按 SnowLuma 文档逐块上传并校验 SHA256，支持麦麦与 QQ 端跨容器部署。"""
        data = path.read_bytes()
        if not data or len(data) > 64 * 1024 * 1024:
            raise ValueError("待投递文件为空或超过 64 MiB")
        digest = hashlib.sha256(data).hexdigest()
        identifier = uuid.uuid4().hex
        chunk_size = 256 * 1024
        total = (len(data) + chunk_size - 1) // chunk_size
        result = None
        try:
            for index in range(total):
                result = await self.call_action(
                    "upload_file_stream",
                    stream_id=identifier,
                    chunk_data=base64.b64encode(data[index * chunk_size : (index + 1) * chunk_size]).decode(),
                    chunk_index=index,
                    total_chunks=total,
                    file_size=len(data),
                    expected_sha256=digest,
                    is_complete=index == total - 1,
                    filename=path.name,
                    file_retention=300000,
                )
            if isinstance(result, dict) and isinstance(result.get("data"), dict):
                result = result["data"]
            if not isinstance(result, dict) or not result.get("file_path") or result.get("sha256") != digest:
                raise RuntimeError("SnowLuma 上传未返回匹配的文件路径与 SHA256")
            return str(result["file_path"])
        except BaseException:
            try:
                await self.call_action("upload_file_stream", stream_id=identifier, reset=True)
            except Exception:
                pass
            raise


class SnowLumaAdapter(OneBotAdapter):
    """补齐上游统一协议中新旧成员接口的别名。"""

    async def get_group_member_list(self, group_id: str) -> list[UnifiedMember]:
        return await self.get_member_list(group_id)

    def remember_user_profile(self, user_id: str, nickname: str | None = None, avatar_url: str | None = None) -> None:
        if avatar_url:
            self._avatar_url_cache[user_id] = (avatar_url, time.time() + self.AVATAR_URL_TTL)


class SDKAdapter:
    """对群聊流显式寻址，跨平台复用麦麦历史库，不把 QQ 群号当流 ID。"""

    def __init__(
        self, ctx: PluginContext, platform: str, streams: dict[str, dict], onebot: OneBotHTTP | None = None
    ) -> None:
        self.ctx, self.platform, self.streams = ctx, platform, streams
        self.platform_id = platform
        self.bot_self_ids: list[str] = []
        self.names: dict[str, str] = {}
        self.onebot = SnowLumaAdapter(onebot) if onebot and platform == "qq" else None
        self.onebot_http = onebot

    async def send_file(self, group_id: str, path: Path, folder_id: str = "") -> bool:
        if not self.onebot_http or self.platform != "qq":
            return bool(
                await self.ctx.send.custom(
                    "file",
                    {"name": path.name, "file": "base64://" + base64.b64encode(path.read_bytes()).decode()},
                    group_id,
                )
            )
        remote = await self.onebot_http.stage_file(path)
        await self.onebot_http.call_action(
            "upload_group_file",
            group_id=int(self.streams[group_id]["group_id"]),
            file=remote,
            name=path.name,
            folder=folder_id,
        )
        return True

    @property
    def capabilities(self):
        return {"telegram": TELEGRAM_CAPABILITIES, "discord": DISCORD_CAPABILITIES}.get(
            self.platform, ONEBOT_V11_CAPABILITIES
        )

    def get_platform_name(self) -> str:
        return "onebot" if self.platform == "qq" else self.platform

    async def fetch_messages(
        self,
        group_id: str,
        days: int = 1,
        max_count: int = 1000,
        before_id: str | None = None,
        since_ts: int | float | None = None,
    ) -> list[UnifiedMessage]:
        start, end = WINDOW.get() or (time.time() - days * 86400, time.time())
        if since_ts is not None:
            start = max(start, float(since_ts))
        rows = await self.ctx.message.get_by_time_in_chat(
            group_id, str(start), str(end), limit=max_count, limit_mode="latest", filter_mai=True, filter_command=True
        )
        messages: dict[str, UnifiedMessage] = {}
        for row in rows:
            raw = normalize_message(row, group_id)
            if raw is None or not start <= raw.timestamp < end:
                continue
            message = UnifiedMessage(
                message_id=raw.message_id,
                sender_id=raw.sender_id,
                sender_name=raw.sender_name,
                sender_card=raw.sender_card,
                group_id=group_id,
                text_content=raw.text_content,
                timestamp=raw.timestamp,
                platform=self.platform,
                contents=tuple(MessageContent(MessageContentType(c.type.value), text=c.text) for c in raw.contents),
            )
            messages[message.message_id or f"{message.timestamp}:{message.sender_id}:{message.text_content}"] = message
            self.names[message.sender_id] = message.get_display_name()
        if self.onebot:
            actual_group = str(self.streams[group_id]["group_id"])
            history = await self.onebot.fetch_messages(
                actual_group, days=days, max_count=max_count, before_id=before_id, since_ts=start
            )
            for msg in history:
                if start <= msg.timestamp < end:
                    messages[msg.message_id] = msg
                    self.names[msg.sender_id] = msg.get_display_name()
        return sorted(messages.values(), key=lambda msg: msg.timestamp)[-max_count:]

    async def send_text(self, group_id: str, text: str) -> bool:
        return bool(await self.ctx.send.text(text, group_id))

    async def send_image(self, group_id: str, image_path: str, caption: str = "") -> bool:
        data = base64.b64encode(Path(image_path).read_bytes()).decode()
        sent = bool(await self.ctx.send.image(data, group_id))
        if sent and caption:
            await self.ctx.send.text(caption, group_id)
        return sent

    async def send_forward_msg(self, group_id: str, nodes: list[dict]) -> bool:
        return bool(await self.ctx.send.forward(nodes, group_id))

    async def send_text_report(self, group_id: str, content: str, fallback_content: str | None = None) -> bool:
        return await self.send_text(group_id, content)

    async def get_group_info(self, group_id: str) -> UnifiedGroup | None:
        item = self.streams.get(group_id)
        if not item:
            return None
        return UnifiedGroup(
            group_id=group_id, group_name=str(item.get("group_name") or group_id), platform=self.platform
        )

    async def get_group_member_list(self, group_id: str) -> list[UnifiedMember] | None:
        if self.onebot:
            return await self.onebot.get_member_list(str(self.streams[group_id]["group_id"]))
        return [UnifiedMember(user_id=uid, nickname=name) for uid, name in self.names.items()]

    async def get_member_info(self, group_id: str, user_id: str) -> UnifiedMember | None:
        if self.onebot:
            return await self.onebot.get_member_info(str(self.streams[group_id]["group_id"]), user_id)
        return UnifiedMember(user_id=user_id, nickname=self.names.get(user_id, user_id))

    async def get_user_avatar_url(self, user_id: str, size: int | None = 40) -> str | None:
        result = await self.ctx.chat.get_avatar(platform=self.platform, target_id=user_id, target_type="user")
        return str(result["url"]) if result.get("status") == "available" else None

    def remember_user_profile(self, user_id: str, nickname: str | None = None, avatar_url: str | None = None) -> None:
        if nickname:
            self.names[user_id] = nickname

    async def is_group_muted(self, group_id: str) -> bool:
        return await self.onebot.is_group_muted(str(self.streams[group_id]["group_id"])) if self.onebot else False

    async def set_reaction(self, group_id: str, message_id: str, emoji: str | int, is_add: bool = True) -> bool:
        if self.onebot:
            return await self.onebot.set_reaction(str(self.streams[group_id]["group_id"]), message_id, emoji, is_add)
        return False

    def convert_to_raw_format(self, messages: list[UnifiedMessage]) -> list[dict]:
        return [
            {
                "time": m.timestamp,
                "message_id": m.message_id,
                "sender": {"user_id": m.sender_id, "nickname": m.sender_name, "card": m.sender_card or ""},
                "message": [{"type": c.type.value, "data": {"text": c.text}} for c in m.contents],
            }
            for m in messages
        ]


class BotManager:
    def __init__(self, ctx: PluginContext, onebot: OneBotHTTP | None = None) -> None:
        self.ctx, self.onebot = ctx, onebot
        self.adapters: dict[str, SDKAdapter] = {}
        self.streams: dict[str, dict] = {}

    async def refresh(self) -> None:
        rows = await self.ctx.chat.get_group_streams(platform="all_platforms")
        self.streams = {}
        for item in rows:
            stream = str(item.get("stream_id") or item["session_id"])
            self.streams[stream] = item
        for platform in {str(item.get("platform", "qq")) for item in self.streams.values()}:
            selected = {key: val for key, val in self.streams.items() if str(val.get("platform", "qq")) == platform}
            if platform in self.adapters:
                self.adapters[platform].streams = selected
            else:
                self.adapters[platform] = SDKAdapter(self.ctx, platform, selected, self.onebot)

    def get_adapter(self, platform_id: str | None = None) -> SDKAdapter | None:
        return self.adapters.get(platform_id or "qq")

    def get_adapter_platform_id(self, adapter: SDKAdapter) -> str:
        return adapter.platform_id
