"""麦麦原生配置模型。"""

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator


class PluginSection(PluginConfigBase):
    config_version: str = Field(default="0.1.0", description="配置结构版本")
    enabled: bool = Field(default=True, description="启用群分析")


class AnalysisConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    allowed_streams: list[str] = Field(
        default_factory=list, description="允许的真实聊天流 ID；空表示允许所有 QQ 群的手动命令"
    )
    scheduled_streams: list[str] = Field(default_factory=list, description="自动日报目标真实聊天流 ID；默认不自动发送")
    send_at: str = Field(default="00:10", description="上海时间每日几点发送昨天的日报，HH:MM")
    model: str = Field(default="", description="分析模型；空表示使用麦麦默认 utils 任务")
    output: str = Field(default="image", description="image 或 text；图片失败时降级文本")
    min_messages: int = Field(default=20, ge=1, le=10000, description="最低分析消息数")
    max_messages: int = Field(default=10000, ge=20, le=100000, description="单日报最大读取条数；超过时报告会标记截断")
    chunk_chars: int = Field(default=18000, ge=1000, le=60000, description="每次模型调用的聊天文本字符上限")
    max_chunks: int = Field(default=20, ge=1, le=100, description="最大分析批次数，超出时明确标记采样")
    max_topics: int = Field(default=8, ge=1, le=30, description="话题数量上限")
    max_quotes: int = Field(default=5, ge=1, le=20, description="金句数量上限")
    quality_enabled: bool = Field(default=True, description="启用聊天质量分析")
    titles_enabled: bool = Field(default=True, description="启用娱乐性群友称号")
    llm_timeout: int = Field(default=120, ge=10, le=600, description="单次模型调用超时秒数")
    cooldown_seconds: int = Field(default=300, ge=0, le=86400, description="同群手动重新分析冷却时间")

    @field_validator("send_at")
    @classmethod
    def valid_time(cls, value: str) -> str:
        from datetime import datetime

        parsed = datetime.strptime(value, "%H:%M")
        return parsed.strftime("%H:%M")

    @field_validator("output")
    @classmethod
    def valid_output(cls, value: str) -> str:
        if value not in {"image", "text"}:
            raise ValueError("输出必须为 image 或 text")
        return value
