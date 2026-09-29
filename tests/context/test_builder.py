"""ContextBuilder / estimate_tokens / Middleware 的 TDD 测试 —— 对应 tasks.md Task 7 的 RED 条目。"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

from harness.config import RuntimeConfig
from harness.context.builder import (
    MEMORY_TOOL_HINT,
    ContextBuilder,
    estimate_tokens,
)
from harness.middleware import LoopState, Middleware
from harness.prompts import SYSTEM_PROMPT

COMPACTION_SUMMARY_NAME = "__compaction_summary__"


def _make_builder(
    history: list[dict[str, Any]],
    memory_index: str | None = None,
) -> tuple[ContextBuilder, MagicMock, MagicMock]:
    """构造注入 mock sessions / mock memory 的 ContextBuilder。"""
    sessions = MagicMock()
    sessions.read_context_messages.return_value = history
    memory = MagicMock()
    memory.render_index.return_value = memory_index
    builder = ContextBuilder(sessions, memory, RuntimeConfig())
    return builder, sessions, memory


class TestContextBuilder:
    def testBuildBasicOrder(self) -> None:
        user1 = {"role": "user", "content": "你好"}
        assistant1 = {"role": "assistant", "content": "你好，有什么可以帮你？"}
        builder, sessions, _ = _make_builder([user1, assistant1])
        out = builder.build("s1", "今天天气")
        sessions.read_context_messages.assert_called_once_with("s1")
        assert len(out) == 4
        assert out[0]["role"] == "system"
        assert out[0]["content"] == SYSTEM_PROMPT
        assert "历史记忆" not in out[0]["content"]
        assert out[1] == user1
        assert out[2] == assistant1
        assert out[3] == {"role": "user", "content": "今天天气"}

    def testMemoryInjected(self) -> None:
        builder, _, memory = _make_builder(
            [], memory_index="- 20260928-143005.md｜用户偏好中文（tags: 偏好）"
        )
        out = builder.build("s1", "你好")
        memory.render_index.assert_called_once_with()
        memory.render_summary.assert_not_called()
        system_content = out[0]["content"]
        assert "历史记忆" in system_content
        assert "用户偏好中文" in system_content
        assert MEMORY_TOOL_HINT in system_content
        assert system_content == (
            SYSTEM_PROMPT
            + "\n\n## 历史记忆\n- 20260928-143005.md｜用户偏好中文（tags: 偏好）"
            + f"\n{MEMORY_TOOL_HINT}"
        )

    def testSummaryMessageKeptFirst(self) -> None:
        summary_msg = {
            "role": "user",
            "name": COMPACTION_SUMMARY_NAME,
            "content": "任务概览：调试登录问题",
        }
        msg1 = {"role": "user", "content": "后来又发生了什么"}
        msg2 = {"role": "assistant", "content": "继续排查"}
        builder, _, _ = _make_builder([summary_msg, msg1, msg2])
        out = builder.build("s1", "新输入")
        assert len(out) == 5
        assert out[0]["role"] == "system"
        assert out[1].get("name") == COMPACTION_SUMMARY_NAME
        assert out[1]["role"] == "user"
        assert out[2] == msg1
        assert out[3] == msg2
        assert out[4] == {"role": "user", "content": "新输入"}

    def testSummaryMessageHasExplicitMarker(self) -> None:
        summary_msg = {
            "role": "user",
            "name": COMPACTION_SUMMARY_NAME,
            "content": "任务概览：调试登录问题",
        }
        builder, _, _ = _make_builder([summary_msg])
        out = builder.build("s1", "继续")
        marked = out[1]
        assert marked["name"] == COMPACTION_SUMMARY_NAME
        assert marked["role"] == "user"
        assert marked["content"] == "以下为此前对话的压缩摘要\n任务概览：调试登录问题"

    def testToolResultTruncated(self) -> None:
        original = "x" * 5000
        tool_msg = {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": original,
        }
        builder, _, _ = _make_builder([tool_msg])
        out = builder.build("s1", "继续")
        truncated = out[1]
        assert truncated["role"] == "tool"
        assert truncated["tool_call_id"] == "call_1"
        assert truncated["content"].startswith(original[:2000])
        assert "全文见会话记录" in truncated["content"]
        assert len(truncated["content"]) <= 2000 + 50
        assert tool_msg["content"] == original

    def testEmptyHistory(self) -> None:
        builder, _, _ = _make_builder([])
        out = builder.build("s1", "你好")
        assert out == [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "你好"},
        ]

    def testEmptyUserInputAppendsNothing(self) -> None:
        """user_input 为空串时不追加当前输入（ReAct 第 2+ 轮复用契约）。"""
        user1 = {"role": "user", "content": "你好"}
        assistant1 = {"role": "assistant", "content": "回答"}
        builder, _, _ = _make_builder([user1, assistant1])
        out = builder.build("s1", "")
        assert out == [
            {"role": "system", "content": SYSTEM_PROMPT},
            user1,
            assistant1,
        ]


class TestEstimateTokens:
    def testDeterministicFormula(self) -> None:
        plain = [
            {"role": "user", "content": "a" * 40},
            {"role": "assistant", "content": "b" * 60},
        ]
        assert estimate_tokens(plain) == math.ceil(100 / 2.5) + 5 * 2
        assert estimate_tokens(plain) == estimate_tokens(plain)

        with_tools = [
            {"role": "user", "content": "c" * 30},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "calc", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "d" * 20},
        ]
        expected = math.ceil(50 / 2.5) + 5 * 3 + 10 * 2 + 8 * 1
        assert estimate_tokens(with_tools) == expected
        assert estimate_tokens(with_tools) == estimate_tokens(with_tools)


class TestMiddleware:
    def testDefaultNoOp(self) -> None:
        state = LoopState(
            session_id="s1",
            round_no=1,
            messages=[{"role": "user", "content": "你好"}],
            user_input="你好",
            trace_id="tid-1",
        )
        snapshot = {
            "session_id": state.session_id,
            "round_no": state.round_no,
            "messages": list(state.messages),
            "user_input": state.user_input,
            "trace_id": state.trace_id,
        }
        middleware = Middleware()
        middleware.before_model(state)
        middleware.after_model(state)
        assert state.session_id == snapshot["session_id"]
        assert state.round_no == snapshot["round_no"]
        assert state.messages == snapshot["messages"]
        assert state.user_input == snapshot["user_input"]
        assert state.trace_id == snapshot["trace_id"]
