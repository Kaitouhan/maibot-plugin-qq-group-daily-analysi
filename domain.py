"""与宿主无关的消息转换、统计与分析结果契约。"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from pydantic import BaseModel, Field

from .vendor.unified_message import MessageContent, MessageContentType, UnifiedMessage

SHANGHAI = timezone(timedelta(hours=8))


class Topic(BaseModel):
    topic: str
    contributors: list[str] = Field(default_factory=list)
    detail: str


class UserTitle(BaseModel):
    name: str = ""
    user_id: str
    title: str
    mbti: str = ""
    reason: str = ""


class Quote(BaseModel):
    content: str
    sender: str
    reason: str = ""


class Dimension(BaseModel):
    name: str
    percentage: float = Field(ge=0, le=100)
    comment: str = ""


class Quality(BaseModel):
    title: str = ""
    subtitle: str = ""
    dimensions: list[Dimension] = Field(default_factory=list)
    summary: str = ""


class Statistics(BaseModel):
    message_count: int
    participant_count: int
    total_characters: int
    emoji_count: int
    hours: list[int]
    most_active_period: str
    users: dict[str, int]
    names: dict[str, str]


class Report(BaseModel):
    stream_id: str
    day: str
    statistics: Statistics
    topics: list[Topic] = Field(default_factory=list)
    titles: list[UserTitle] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    quality: Quality | None = None
    warnings: list[str] = Field(default_factory=list)
    analyzed_messages: int = 0
    engine_payload: dict[str, object] = Field(default_factory=dict)


def day_bounds(day: date) -> tuple[float, float]:
    """返回上海时区自然日的左闭右开时间边界。"""
    start = datetime.combine(day, time.min, tzinfo=SHANGHAI)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def normalize_message(raw: dict[str, object], stream_id: str) -> UnifiedMessage | None:
    """将 MaiBot 1.3.15 消息载荷转为上游统一消息。

    Args:
        raw: Host 序列化的单条消息。
        stream_id: 已校验的真实群聊流。

    Returns:
        有效消息，或需要忽略的命令/无效消息对应的 None。
    """
    if raw.get("session_id") and raw["session_id"] != stream_id:
        return None
    if raw.get("is_command") or raw.get("is_notify"):
        return None
    info = raw.get("message_info")
    info = info if isinstance(info, dict) else {}
    user = info.get("user_info", {})
    if not isinstance(user, dict) or not user.get("user_id"):
        return None
    timestamp = raw.get("timestamp", 0)
    try:
        if isinstance(timestamp, str):
            try:
                stamp = float(timestamp)
            except ValueError:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                stamp = parsed.replace(tzinfo=SHANGHAI).timestamp() if parsed.tzinfo is None else parsed.timestamp()
        elif isinstance(timestamp, (int, float)):
            stamp = float(timestamp)
        else:
            return None
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(stamp) or stamp < 0 or stamp > 253402214400:
        return None
    segments = raw.get("raw_message", [])
    contents: list[MessageContent] = []
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            kind = str(segment.get("type", "unknown"))
            data = segment.get("data", "")
            if kind == "text":
                text = str(data.get("text", "")) if isinstance(data, dict) else str(data)
                contents.append(MessageContent(MessageContentType.TEXT, text=text))
            elif kind in {"image", "emoji", "voice", "video"}:
                contents.append(MessageContent(MessageContentType(kind)))
    text = str(raw.get("processed_plain_text") or "")
    if not text:
        text = "".join(item.text for item in contents)
    if text.lstrip().startswith("/"):
        return None
    return UnifiedMessage(
        message_id=str(raw.get("message_id", "")),
        sender_id=str(user["user_id"]),
        sender_name=str(user.get("user_nickname") or user["user_id"]),
        sender_card=str(user.get("user_cardname") or ""),
        group_id=stream_id,
        text_content=text,
        contents=tuple(contents),
        timestamp=int(stamp),
        platform="qq",
    )


def statistics(messages: list[UnifiedMessage]) -> Statistics:
    """计算完整消息集的统计；小时分布固定为上海时区。"""
    users = Counter(msg.sender_id for msg in messages)
    hours = [0] * 24
    for msg in messages:
        hours[datetime.fromtimestamp(msg.timestamp, SHANGHAI).hour] += 1
    peak = max(range(24), key=hours.__getitem__)
    return Statistics(
        message_count=len(messages),
        participant_count=len(users),
        total_characters=sum(len(msg.text_content) for msg in messages),
        emoji_count=sum(msg.get_emoji_count() for msg in messages),
        hours=hours,
        most_active_period=f"{peak:02d}:00-{(peak + 1) % 24:02d}:00",
        users=dict(users.most_common()),
        names={m.sender_id: m.get_display_name() for m in messages},
    )


def decode_json(text: str) -> object:
    """接纳纯 JSON 或一个 JSON 代码块，拒绝额外解释及截断响应。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def transcript_chunks(messages: list[UnifiedMessage], max_chars: int) -> list[str]:
    """按字符预算分批，单条过长消息显式截断，不静默舍弃整批。"""
    chunks: list[str] = []
    current = ""
    for msg in messages:
        if not msg.text_content.strip():
            continue
        clock = datetime.fromtimestamp(msg.timestamp, SHANGHAI).strftime("%H:%M")
        line = f"[{clock}] [{msg.sender_id}]: {msg.text_content}"[: max_chars - 1] + "\n"
        if len(current) + len(line) > max_chars and current:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks
