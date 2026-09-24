"""ContextBuilder —— 发送给 LLM 的消息列表组装与字符近似 token 估算。

组装顺序：system（含记忆段，如有）→ 压缩摘要消息（如历史首条为
__compaction_summary__ 摘要，为其加明文标记）→ 其余未压缩历史 →
当前用户输入。工具结果超长时截断并附尾注（全量以会话记录为真源）。
"""

from __future__ import annotations

import math
from typing import Any

from harness.config import RuntimeConfig
from harness.prompts import SYSTEM_PROMPT
from harness.session.store import COMPACTION_SUMMARY_NAME

# 压缩摘要消息 content 的首行明文标记（与原文摘要可区分）
SUMMARY_MARKER_LINE: str = "以下为此前对话的压缩摘要"

# 记忆段注入 system 提示词时的小节标题
MEMORY_SECTION_HEADER: str = "## 历史记忆"

# 工具结果截断尾注（存储留全量，上下文只保留前缀）
TOOL_RESULT_TRUNCATION_SUFFIX: str = "\n…[已截断，全文见会话记录]"


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """按字符近似估算消息列表的 token 数（不依赖真实 tokenizer）。

    公式：ceil(全部消息 content 字符长度之和 / 2.5) + 每条消息 5
    + 每个工具调用块（assistant 消息 tool_calls 列表的条目）10
    + 每个工具结果块（role=="tool" 的消息）8。仅用于压缩触发判断，
    真实用量以 API usage 记入 trace；对同一输入结果幂等。
    """
    total_chars = 0
    message_count = 0
    tool_call_blocks = 0
    tool_result_blocks = 0
    for message in messages:
        message_count += 1
        content = message.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        if message.get("role") == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                tool_call_blocks += len(tool_calls)
        if message.get("role") == "tool":
            tool_result_blocks += 1
    return (
        math.ceil(total_chars / 2.5)
        + 5 * message_count
        + 10 * tool_call_blocks
        + 8 * tool_result_blocks
    )


class ContextBuilder:
    """按固定顺序组装发送给 LLM 的上下文消息列表。

    依赖注入 sessions（SessionStore）与 memory（MemoryStore），
    构造时只保存引用，build 时才读取；历史消息逐条浅拷贝后适配，
    不修改传入的原 dict。
    """

    def __init__(
        self, sessions: Any, memory: Any, config: RuntimeConfig
    ) -> None:
        """注入会话存储、长期记忆存储与运行配置。"""
        self._sessions = sessions
        self._memory = memory
        self._config = config

    def build(
        self, session_id: str, user_input: str
    ) -> list[dict[str, Any]]:
        """组装完整上下文：system → 压缩摘要（如有）→ 未压缩历史 → 当前输入。"""
        history = self._sessions.read_context_messages(session_id)
        memory_summary = self._memory.render_summary(session_id)
        messages: list[dict[str, Any]] = [self._build_system_message(memory_summary)]
        for message in history:
            messages.append(self._adapt_history_message(message))
        messages.append({"role": "user", "content": user_input})
        return messages

    def _build_system_message(
        self, memory_summary: str | None
    ) -> dict[str, Any]:
        """构造 system 消息：无记忆时为 SYSTEM_PROMPT 原文，有记忆时追加记忆段。"""
        if memory_summary:
            content = f"{SYSTEM_PROMPT}\n\n{MEMORY_SECTION_HEADER}\n{memory_summary}"
        else:
            content = SYSTEM_PROMPT
        return {"role": "system", "content": content}

    def _adapt_history_message(
        self, message: dict[str, Any]
    ) -> dict[str, Any]:
        """浅拷贝单条历史消息并按上下文约定适配。

        压缩摘要消息（name 为 __compaction_summary__）content 首行加
        明文标记；tool 消息 content 超过 tool_result_max_chars 时截断
        并附「全文见会话记录」尾注。其余消息原样透传（浅拷贝防突变）。
        """
        adapted = dict(message)
        if adapted.get("name") == COMPACTION_SUMMARY_NAME:
            original = adapted.get("content")
            original_text = original if isinstance(original, str) else ""
            adapted["content"] = f"{SUMMARY_MARKER_LINE}\n{original_text}"
            return adapted
        if adapted.get("role") == "tool":
            content = adapted.get("content")
            if (
                isinstance(content, str)
                and len(content) > self._config.tool_result_max_chars
            ):
                adapted["content"] = (
                    content[: self._config.tool_result_max_chars]
                    + TOOL_RESULT_TRUNCATION_SUFFIX
                )
        return adapted
