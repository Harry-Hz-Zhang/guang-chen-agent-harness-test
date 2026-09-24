"""ContextCompressor 与 CompactionMiddleware 的单元测试（全 mock，零网络）。

注意：本文件 mock 的消息记录 ordinal 采用 0 基（与 tasks.md RED 的
「ordinal 0-29」口径一致），不经过真实 SessionStore。
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.config import RuntimeConfig
from harness.context.compressor import CompactionMiddleware, ContextCompressor
from harness.llm import AIMessage, Usage
from harness.middleware import LoopState
from harness.prompts import COMPACTION_PROMPT


def _msg(ordinal: int, role: str, content: str = "内容") -> dict[str, Any]:
    """构造一条 message 记录（含 ordinal 与消息体）。"""
    return {"ts": "2026-01-01T00:00:00Z", "ordinal": ordinal, "kind": "message",
            "message": {"role": role, "content": content}}


def _pair_records(start_ordinal: int, count: int, prefix: str = "内容") -> list[dict[str, Any]]:
    """构造 count 组 user/assistant 对（返回记录列表，ordinals 连续）。"""
    records: list[dict[str, Any]] = []
    ordinal = start_ordinal
    for i in range(count):
        records.append(_msg(ordinal, "user", f"{prefix}-u{i}"))
        records.append(_msg(ordinal + 1, "assistant", f"{prefix}-a{i}"))
        ordinal += 2
    return records


def _window_messages(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从记录列表提取 message 记录的消息体（跳过 compaction 等非消息记录）。"""
    return [
        r["message"]
        for r in records
        if r.get("kind") == "message" and isinstance(r.get("message"), dict)
    ]


def _make_compressor(
    config: RuntimeConfig,
    records: list[dict[str, Any]],
    context_messages: list[dict[str, Any]] | None = None,
    llm_content: str = "任务概览：调试登录问题",
) -> tuple[ContextCompressor, MagicMock, MagicMock]:
    """构造注入 mock sessions/llm/trace 的 ContextCompressor。"""
    sessions = MagicMock()
    sessions.load_records.return_value = records
    sessions.read_context_messages.return_value = (
        context_messages if context_messages is not None else _window_messages(records)
    )
    llm = MagicMock()
    llm.invoke.return_value = AIMessage(
        content=llm_content,
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    trace = MagicMock()
    trace.start_llm_span.return_value = "span-c"
    compressor = ContextCompressor(sessions, llm, trace, config)
    return compressor, sessions, llm


class TestCompressor:

    def testShouldCompactByRounds(self) -> None:
        """未压缩窗口内对话轮达到阈值即触发。"""
        config = RuntimeConfig(compact_rounds=5, compact_tokens=100_000)
        records = _pair_records(0, 5)
        compressor, _, _ = _make_compressor(config, records)
        assert compressor.should_compact("s1") is True

    def testNoCompactAfterCompaction(self) -> None:
        """已有压缩记录时按未压缩窗口统计：窗口仅 1 轮不触发。"""
        config = RuntimeConfig(compact_rounds=5, compact_tokens=100_000)
        records = _pair_records(0, 10)  # 前 10 轮（ordinal 0-19）
        records.append({
            "ts": "2026-01-01T00:00:00Z", "ordinal": 20, "kind": "compaction",
            "compressed_up_to": 19, "summary": "旧摘要", "summary_model": "m",
        })
        records.extend(_pair_records(21, 1, "新"))  # 窗口内 1 轮（ordinal 21-22）
        compressor, _, _ = _make_compressor(config, records)
        assert compressor.should_compact("s1") is False

    def testShouldCompactByTokens(self) -> None:
        """估算 token 达到阈值即触发（轮次未达也触发）。"""
        config = RuntimeConfig(compact_rounds=60, compact_tokens=100)
        records = _pair_records(0, 1)
        context = [{"role": "user", "content": "字" * 250},
                   {"role": "assistant", "content": "字" * 250}]
        compressor, _, _ = _make_compressor(config, records, context_messages=context)
        assert compressor.should_compact("s1") is True

    def testNoCompactWhenBelow(self) -> None:
        """轮次与 token 均未达阈值：不触发。"""
        config = RuntimeConfig(compact_rounds=5, compact_tokens=100_000)
        records = _pair_records(0, 4)
        compressor, _, _ = _make_compressor(config, records)
        assert compressor.should_compact("s1") is False

    def testCompactKeepsRecentRounds(self) -> None:
        """压缩保留最近 5 轮原文：30 条消息（ordinal 0-29）→ compressed_up_to==19。"""
        config = RuntimeConfig(keep_recent_rounds=5)
        records = _pair_records(0, 15)
        compressor, sessions, _ = _make_compressor(config, records)
        compressor.compact("s1", "tid-9")
        sessions.append_compaction.assert_called_once_with(
            "s1", 19, "任务概览：调试登录问题", config.model
        )

    def testKeepRecentRoundsCountsPairsNotMessages(self) -> None:
        """保留按轮计：前 10 轮带工具（每轮 4 条）+ 末 5 轮纯对话 → 恰保留末 5 轮。"""
        config = RuntimeConfig(keep_recent_rounds=5)
        records: list[dict[str, Any]] = []
        ordinal = 0
        for i in range(10):  # 带 4 条消息（含工具调用对）的 10 轮（ordinal 0-39）
            records.append(_msg(ordinal, "user", f"u{i}"))
            records.append({
                "ts": "t", "ordinal": ordinal + 1, "kind": "message",
                "message": {"role": "assistant", "content": "",
                            "tool_calls": [{"id": f"c{i}", "type": "function",
                                            "function": {"name": "calc", "arguments": "{}"}}]},
            })
            records.append(_msg(ordinal + 2, "tool", "42"))
            records.append(_msg(ordinal + 3, "assistant", f"a{i}"))
            ordinal += 4
        for i in range(5):  # 末 5 轮纯对话（ordinal 40-49）
            records.append(_msg(ordinal, "user", f"tu{i}"))
            records.append(_msg(ordinal + 1, "assistant", f"ta{i}"))
            ordinal += 2
        compressor, sessions, _ = _make_compressor(config, records)
        compressor.compact("s1", "tid-9")
        call = sessions.append_compaction.call_args
        assert call.args[1] == 39  # 保留 ordinal 40-49（末 5 轮 10 条），第 11 轮之前

    def testCutPointNotSplitToolPair(self) -> None:
        """切点落在工具配对中间时回退到该轮起点（配对同侧完整）。"""
        config = RuntimeConfig(keep_recent_rounds=5)
        records: list[dict[str, Any]] = []
        ordinal = 0
        for i in range(9):  # 前 9 轮纯对话（ordinal 0-17）
            records.append(_msg(ordinal, "user", f"u{i}"))
            records.append(_msg(ordinal + 1, "assistant", f"a{i}"))
            ordinal += 2
        # 第 10 轮：u10(18), a10tc(19)——工具调用消息
        records.append(_msg(ordinal, "user", "u10"))
        records.append({
            "ts": "t", "ordinal": ordinal + 1, "kind": "message",
            "message": {"role": "assistant", "content": "",
                        "tool_calls": [{"id": "tc10", "type": "function",
                                        "function": {"name": "calc", "arguments": "{}"}}]},
        })
        ordinal += 2
        # 第 11 轮：u11(20), tool10(21)——上一轮的工具结果落在本轮开头，a11(22)
        records.append(_msg(ordinal, "user", "u11"))
        records.append({
            "ts": "t", "ordinal": ordinal + 1, "kind": "message",
            "message": {"role": "tool", "tool_call_id": "tc10", "content": "42"},
        })
        records.append(_msg(ordinal + 2, "assistant", "a11"))
        ordinal += 3
        for i in range(4):  # 第 12-15 轮纯对话（ordinal 23-30）
            records.append(_msg(ordinal, "user", f"u{i+12}"))
            records.append(_msg(ordinal + 1, "assistant", f"a{i+12}"))
            ordinal += 2
        compressor, sessions, _ = _make_compressor(config, records)
        compressor.compact("s1", "tid-9")
        call = sessions.append_compaction.call_args
        # 朴素切点为 19（u11 前），但 a10tc(19)/tool10(21) 分属两侧 → 回退到第 10 轮起点 u10(18) 之前
        assert call.args[1] == 17

    def testSummaryGeneratedViaPrompt(self) -> None:
        """压缩摘要经 COMPACTION_PROMPT 生成，历史渲染进入输入。"""
        config = RuntimeConfig()
        records = _pair_records(0, 15, "事实")
        compressor, sessions, llm = _make_compressor(config, records)
        compressor.compact("s1", "tid-9")
        request = llm.invoke.call_args.args[0]
        prompt = request[0]["content"]
        assert prompt.startswith(COMPACTION_PROMPT)
        assert "事实-u0" in prompt
        call = sessions.append_compaction.call_args
        assert call.args[2] == "任务概览：调试登录问题"

    def testCompactChainIncludesOldSummary(self) -> None:
        """链式压缩：旧压缩摘要纳入新压缩输入。"""
        config = RuntimeConfig(keep_recent_rounds=5)
        records = _pair_records(0, 10)  # ordinal 0-19
        records.append({
            "ts": "t", "ordinal": 20, "kind": "compaction",
            "compressed_up_to": 19, "summary": "旧摘要", "summary_model": "m",
        })
        records.extend(_pair_records(21, 15, "续"))  # 窗口 ordinal 21-50
        compressor, _, llm = _make_compressor(config, records)
        compressor.compact("s1", "tid-9")
        prompt = llm.invoke.call_args.args[0][0]["content"]
        assert "旧摘要" in prompt

    def testCompactToolResultTruncatedInRendering(self) -> None:
        """压缩输入渲染中超长 tool_result 截断（500 字符上限）。"""
        config = RuntimeConfig(keep_recent_rounds=5)
        records = _pair_records(0, 9)  # ordinal 0-17
        records.append({
            "ts": "t", "ordinal": 18, "kind": "message",
            "message": {"role": "assistant", "content": "",
                        "tool_calls": [{"id": "c9", "type": "function",
                                        "function": {"name": "calc", "arguments": "{}"}}]},
        })
        records.append({
            "ts": "t", "ordinal": 19, "kind": "message",
            "message": {"role": "tool", "tool_call_id": "c9", "content": "结" * 3000},
        })
        records.extend(_pair_records(20, 6, "后"))  # ordinal 20-31
        compressor, _, llm = _make_compressor(config, records)
        compressor.compact("s1", "tid-9")
        prompt = llm.invoke.call_args.args[0][0]["content"]
        assert "结" * 3000 not in prompt
        assert "结" * 500 in prompt


class TestCompactionMiddleware:

    def testFailureDegrades(self, caplog: pytest.LogCaptureFixture) -> None:
        """compact 抛异常时 before_model 降级：不抛、记 warning、state 不变。"""
        compressor = MagicMock()
        compressor.should_compact.return_value = True
        compressor.compact.side_effect = RuntimeError("LLM 炸了")
        middleware = CompactionMiddleware(compressor)
        state = LoopState(
            session_id="s1", round_no=0,
            messages=[{"role": "user", "content": "问"}],
            user_input="问", trace_id="tid-1",
        )
        snapshot = list(state.messages)
        with caplog.at_level("WARNING"):
            middleware.before_model(state)
        assert state.messages == snapshot
        assert any("压缩" in r.message for r in caplog.records)

    def testCompactLlmCallTraced(self) -> None:
        """压缩的 LLM 调用挂 span：start_llm_span 恰 1 次、kind=compaction、trace_id 一致。"""
        config = RuntimeConfig(keep_recent_rounds=5)
        records = _pair_records(0, 15)
        sessions = MagicMock()
        sessions.load_records.return_value = records
        sessions.read_context_messages.return_value = _window_messages(records)
        llm = MagicMock()
        llm.invoke.return_value = AIMessage(content="任务概览：x")
        trace = MagicMock()
        trace.start_llm_span.return_value = "span-c"
        compressor = ContextCompressor(sessions, llm, trace, config)
        compressor.compact("s1", "tid-7")
        assert trace.start_llm_span.call_count == 1
        span_call = trace.start_llm_span.call_args
        assert span_call.args[0] == "tid-7"
        assert span_call.kwargs.get("kind") == "compaction"
        assert trace.end_llm_span.call_count == 1
