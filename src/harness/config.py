"""RuntimeConfig —— Agent runtime 全部数值常量的集中定义与环境变量覆盖。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL: str = "deepseek-flash"
DEFAULT_BASE_URL: str = "https://api.deepseek.com"

_HARNESS_ENV_NUMERIC_FIELDS: tuple[tuple[str, type], ...] = (
    ("max_rounds", int),
    ("compact_rounds", int),
    ("compact_tokens", int),
    ("keep_recent_rounds", int),
    ("idle_seconds", int),
    ("scan_interval_seconds", int),
    ("llm_timeout_seconds", float),
    ("llm_max_retries", int),
    ("tool_result_max_chars", int),
)


@dataclass
class RuntimeConfig:
    """持有 runtime 运行所需的全部可配置项及其默认值。

    字段含义：model/base_url 为 LLM 接入参数；thinking_enabled 与
    stream_enabled 控制思考模式与流式输出；max_rounds 为单次请求
    决策轮上限；compact_rounds / compact_tokens / keep_recent_rounds
    为超长压缩触发与保留参数；idle_seconds / scan_interval_seconds
    为闲置会话总结参数；llm_* 为 LLM 调用超时与重试参数；
    tool_result_max_chars 为工具结果进上下文的截断长度；data_dir
    为运行期产物（会话/记忆/trace）根目录。
    """

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    thinking_enabled: bool = True
    stream_enabled: bool = True
    max_rounds: int = 15
    compact_rounds: int = 60
    compact_tokens: int = 100_000
    keep_recent_rounds: int = 5
    idle_seconds: int = 7200
    scan_interval_seconds: int = 300
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    tool_result_max_chars: int = 2000
    data_dir: Path = Path("data")

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        """从环境变量构造配置实例。

        LLM_MODEL / LLM_BASE_URL 存在时覆盖 model 与 base_url；
        HARNESS_ 前缀环境变量覆盖同名字段（如 HARNESS_MAX_ROUNDS），
        非法值（如 "abc"）回退默认值：不抛异常、不产生 None。
        """
        overrides: dict[str, object] = {}
        model = os.environ.get("LLM_MODEL")
        if model:
            overrides["model"] = model
        base_url = os.environ.get("LLM_BASE_URL")
        if base_url:
            overrides["base_url"] = base_url
        for field_name, parser in _HARNESS_ENV_NUMERIC_FIELDS:
            raw = os.environ.get(f"HARNESS_{field_name.upper()}")
            if not raw:
                continue
            try:
                overrides[field_name] = parser(raw)
            except ValueError:
                logger.warning(
                    "环境变量 HARNESS_%s=%r 非法（期望 %s），回退默认值",
                    field_name.upper(),
                    raw,
                    parser.__name__,
                )
        return cls(**overrides)

