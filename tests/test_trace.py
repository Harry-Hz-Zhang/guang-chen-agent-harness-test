"""TraceCollector 与 JsonlExporter 单元测试：span 生命周期、OTel 字段命名、
正文截断与导出故障隔离。

全部依赖注入（内存 fake exporter / pytest tmp_path），零网络、零真实 LLM。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from harness.llm import ToolCall, Usage
from harness.trace import TRACE_MAX_CHARS, JsonlExporter, TraceCollector

_TRACED_SESSION = "s1"
_TRACED_MODEL = "deepseek-flash"


class FakeExporter:
    """内存 fake：把 export 收到的事件按序存入 events 列表。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def export(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class ExplodingExporter:
    """export 恒抛 IOError 的 fake，用于验证导出失败被吞且仅告警。"""

    def export(self, event: dict[str, Any]) -> None:
        raise IOError("trace 磁盘被锁")


def _emit_llm_event(
    exporter: FakeExporter,
    messages: list[dict[str, Any]] | None = None,
    output: dict[str, Any] | None = None,
    usage: Usage | None = None,
    kind: str = "chat",
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """驱动 collector 走完一次完整 LLM span 生命周期，返回唯一事件。"""
    collector = TraceCollector(exporter)
    trace_id = collector.start_trace(_TRACED_SESSION)
    span_id = collector.start_llm_span(
        trace_id,
        _TRACED_MODEL,
        messages if messages is not None else [{"role": "user", "content": "你好"}],
        kind=kind,
    )
    collector.end_llm_span(
        span_id,
        output if output is not None else {"role": "assistant", "content": "你好！"},
        usage if usage is not None else Usage(prompt_tokens=312, completion_tokens=47, total_tokens=359),
        "stop",
        error=error,
    )
    assert len(exporter.events) == 1
    return exporter.events[0]


class TestTrace:
    """覆盖 TraceCollector 的 span 树、字段命名、截断与故障隔离行为。"""

    def testLlmSpanLifecycle(self) -> None:
        """start_trace/start 不发事件；end 后恰 1 条事件且 ID 三件套、OTel 字段、时长齐全。"""
        exporter = FakeExporter()
        collector = TraceCollector(exporter)
        trace_id = collector.start_trace(_TRACED_SESSION)
        assert exporter.events == []
        span_id = collector.start_llm_span(
            trace_id, _TRACED_MODEL, [{"role": "user", "content": "你好"}]
        )
        assert exporter.events == []
        collector.end_llm_span(
            span_id,
            {"role": "assistant", "content": "你好！"},
            Usage(prompt_tokens=312, completion_tokens=47, total_tokens=359),
            "stop",
        )
        assert len(exporter.events) == 1
        event = exporter.events[0]
        assert event["schema_version"] == 1
        assert event["type"] == "llm"
        assert event["trace_id"] == trace_id
        assert event["span_id"] == span_id
        assert event["parent_span_id"] is None
        assert event["status"] == "ok"
        assert event["error"] is None
        attributes = event["attributes"]
        assert attributes["gen_ai.operation.name"] == "chat"
        assert attributes["gen_ai.provider.name"] == "deepseek"
        assert attributes["gen_ai.request.model"] == _TRACED_MODEL
        assert attributes["gen_ai.conversation.id"] == _TRACED_SESSION
        assert attributes["gen_ai.usage.input_tokens"] == 312
        assert attributes["gen_ai.usage.output_tokens"] == 47
        assert attributes["gen_ai.usage.reasoning_tokens"] == 0
        assert attributes["harness.span.kind"] == "chat"
        assert attributes["gen_ai.input.messages"] == [{"role": "user", "content": "你好"}]
        assert attributes["gen_ai.output.messages"] == [
            {"role": "assistant", "content": "你好！"}
        ]
        start_time = datetime.fromisoformat(event["start_time"])
        end_time = datetime.fromisoformat(event["end_time"])
        assert start_time.tzinfo is not None
        assert end_time.tzinfo is not None
        assert event["duration_ms"] >= 0
        assert event["duration_ms"] == int((end_time - start_time).total_seconds() * 1000)

    def testToolSpanParent(self) -> None:
        """工具 span 以所属 LLM span 为父，tool 调用字段与 conversation id 齐全。"""
        exporter = FakeExporter()
        collector = TraceCollector(exporter)
        trace_id = collector.start_trace(_TRACED_SESSION)
        llm_span_id = collector.start_llm_span(
            trace_id, _TRACED_MODEL, [{"role": "user", "content": "查一下愿景"}]
        )
        call = ToolCall(
            id="call_1",
            name="search",
            arguments_raw='{"query": "公司愿景"}',
            args={"query": "公司愿景"},
        )
        tool_span_id = collector.start_tool_span(trace_id, llm_span_id, call)
        collector.end_tool_span(tool_span_id, "命中：公司愿景文本", None)
        assert len(exporter.events) == 1
        event = exporter.events[0]
        assert event["schema_version"] == 1
        assert event["type"] == "tool"
        assert event["trace_id"] == trace_id
        assert event["span_id"] == tool_span_id
        assert event["parent_span_id"] == llm_span_id
        assert event["status"] == "ok"
        assert event["error"] is None
        attributes = event["attributes"]
        assert attributes["gen_ai.operation.name"] == "execute_tool"
        assert attributes["gen_ai.tool.name"] == "search"
        assert attributes["gen_ai.tool.call.id"] == "call_1"
        assert attributes["gen_ai.tool.call.arguments"] == '{"query": "公司愿景"}'
        assert attributes["gen_ai.tool.call.result"] == "命中：公司愿景文本"
        assert attributes["gen_ai.conversation.id"] == _TRACED_SESSION
        assert event["duration_ms"] >= 0

    def testErrorSpan(self) -> None:
        """end_tool_span 传 error dict 时 status=error 且结构化 error 透传入事件。"""
        exporter = FakeExporter()
        collector = TraceCollector(exporter)
        trace_id = collector.start_trace(_TRACED_SESSION)
        llm_span_id = collector.start_llm_span(
            trace_id, _TRACED_MODEL, [{"role": "user", "content": "慢工具"}]
        )
        call = ToolCall(id="call_1", name="search", arguments_raw="{}", args={})
        tool_span_id = collector.start_tool_span(trace_id, llm_span_id, call)
        error = {"error.type": "ToolExecutionError", "message": "工具执行超时"}
        collector.end_tool_span(tool_span_id, None, error)
        event = exporter.events[0]
        assert event["status"] == "error"
        assert event["error"] == error
        assert "超时" in event["error"]["message"]
        assert event["error"]["error.type"] == "ToolExecutionError"
        assert event["attributes"]["gen_ai.tool.call.result"] is None

    def testExporterFailureSwallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """exporter 抛 IOError 时 collector 各方法均不抛异常，仅记录 logging.warning。"""
        collector = TraceCollector(ExplodingExporter())
        with caplog.at_level(logging.WARNING):
            trace_id = collector.start_trace(_TRACED_SESSION)
            span_id = collector.start_llm_span(
                trace_id, _TRACED_MODEL, [{"role": "user", "content": "你好"}]
            )
            collector.end_llm_span(
                span_id,
                {"role": "assistant", "content": "答"},
                Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                "stop",
            )
            tool_span_id = collector.start_tool_span(
                trace_id,
                span_id,
                ToolCall(id="call_1", name="search", arguments_raw="{}", args={}),
            )
            collector.end_tool_span(tool_span_id, "结果", None)
        warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert any("trace 磁盘被锁" in rec.getMessage() for rec in warnings)

    def testJsonlFileWritten(self, tmp_path: Path) -> None:
        """JsonlExporter 按会话落盘：恰 2 行、逐行 json.loads 成功且 schema_version==1。"""
        exporter = JsonlExporter(tmp_path)
        exporter.export(
            {
                "schema_version": 1,
                "type": "llm",
                "attributes": {"gen_ai.conversation.id": _TRACED_SESSION},
            }
        )
        exporter.export(
            {
                "schema_version": 1,
                "type": "tool",
                "attributes": {"gen_ai.conversation.id": _TRACED_SESSION},
            }
        )
        path = tmp_path / f"{_TRACED_SESSION}.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        events = [json.loads(line) for line in lines]
        assert [event["type"] for event in events] == ["llm", "tool"]
        assert all(event["schema_version"] == 1 for event in events)

    def testUniqueTraceIds(self) -> None:
        """连续两次 start_trace 的 trace_id 互不相同且均为 32 位小写 hex。"""
        collector = TraceCollector(FakeExporter())
        first = collector.start_trace(_TRACED_SESSION)
        second = collector.start_trace("s2")
        assert first != second
        assert re.fullmatch(r"[0-9a-f]{32}", first) is not None
        assert re.fullmatch(r"[0-9a-f]{32}", second) is not None

    def testSpanIdSixteenHex(self) -> None:
        """两次 start_llm_span 的 span_id 均为互不相同的 16 位小写 hex。"""
        collector = TraceCollector(FakeExporter())
        trace_id = collector.start_trace(_TRACED_SESSION)
        first = collector.start_llm_span(trace_id, _TRACED_MODEL, [])
        second = collector.start_llm_span(trace_id, _TRACED_MODEL, [])
        assert first != second
        assert re.fullmatch(r"[0-9a-f]{16}", first) is not None
        assert re.fullmatch(r"[0-9a-f]{16}", second) is not None

    def testReasoningTokensRecorded(self) -> None:
        """usage.reasoning_tokens=128 时精确记入 gen_ai.usage.reasoning_tokens。"""
        exporter = FakeExporter()
        event = _emit_llm_event(
            exporter,
            usage=Usage(
                prompt_tokens=312,
                completion_tokens=47,
                total_tokens=359,
                reasoning_tokens=128,
            ),
        )
        assert event["attributes"]["gen_ai.usage.reasoning_tokens"] == 128

    def testContentTruncatedTo2000(self) -> None:
        """5000 字符正文截为前 2000 字符+「已截断」尾注；入参消息同规则且不动原 dict。"""
        long_output = "答" * 5000
        long_input = "问" * 5000
        original_message = {"role": "user", "content": long_input}
        exporter = FakeExporter()
        event = _emit_llm_event(
            exporter,
            messages=[original_message],
            output={"role": "assistant", "content": long_output},
        )
        output_content = event["attributes"]["gen_ai.output.messages"][0]["content"]
        assert output_content.startswith(long_output[:TRACE_MAX_CHARS])
        assert "已截断" in output_content
        assert len(output_content) <= TRACE_MAX_CHARS + 20
        input_content = event["attributes"]["gen_ai.input.messages"][0]["content"]
        assert input_content.startswith(long_input[:TRACE_MAX_CHARS])
        assert "已截断" in input_content
        assert len(input_content) <= TRACE_MAX_CHARS + 20
        assert original_message["content"] == long_input

    def testSpanKindAttribute(self) -> None:
        """kind 透传为 harness.span.kind：compaction / idle_summary，缺省为 chat。"""
        exporter = FakeExporter()
        collector = TraceCollector(exporter)
        trace_id = collector.start_trace(_TRACED_SESSION)
        span_compaction = collector.start_llm_span(
            trace_id, _TRACED_MODEL, [], kind="compaction"
        )
        span_idle = collector.start_llm_span(
            trace_id, _TRACED_MODEL, [], kind="idle_summary"
        )
        span_default = collector.start_llm_span(trace_id, _TRACED_MODEL, [])
        for span_id in (span_compaction, span_idle, span_default):
            collector.end_llm_span(
                span_id,
                {"role": "assistant", "content": "摘要"},
                Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                "stop",
            )
        assert len(exporter.events) == 3
        kinds = {
            event["span_id"]: event["attributes"]["harness.span.kind"]
            for event in exporter.events
        }
        assert kinds[span_compaction] == "compaction"
        assert kinds[span_idle] == "idle_summary"
        assert kinds[span_default] == "chat"

    def testUnknownSpanIdWarnsAndNoop(self, caplog: pytest.LogCaptureFixture) -> None:
        """end_* 传入未注册 span_id：不抛异常、exporter 收 0 事件、逐次记 warning。"""
        exporter = FakeExporter()
        collector = TraceCollector(exporter)
        unknown_llm_span = "0" * 16
        unknown_tool_span = "1" * 16
        with caplog.at_level(logging.WARNING):
            collector.end_llm_span(
                unknown_llm_span,
                {"role": "assistant", "content": "x"},
                Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                "stop",
            )
            collector.end_tool_span(unknown_tool_span, "结果", None)
        assert exporter.events == []
        warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
        assert len(warnings) == 2
        assert unknown_llm_span in warnings[0].getMessage()
        assert unknown_tool_span in warnings[1].getMessage()

    def testExporterWriteFailureWarnsOnly(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        """trace_dir 指向已存在的普通文件（mkdir 失败路径）：export 不抛异常，仅记 warning。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("占位", encoding="utf-8")
        exporter = JsonlExporter(blocker)
        with caplog.at_level(logging.WARNING):
            exporter.export(
                {
                    "schema_version": 1,
                    "type": "llm",
                    "attributes": {"gen_ai.conversation.id": _TRACED_SESSION},
                }
            )
        warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "写盘失败" in warnings[0].getMessage()
        assert blocker.read_text(encoding="utf-8") == "占位"

    def testMissingConversationIdFallsBack(self, tmp_path: Path) -> None:
        """事件缺 gen_ai.conversation.id（attributes 空或无 attributes）：回退写 unknown.jsonl。"""
        exporter = JsonlExporter(tmp_path)
        exporter.export({"schema_version": 1, "type": "llm", "attributes": {}})
        exporter.export({"schema_version": 1, "type": "llm"})
        path = tmp_path / "unknown.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        events = [json.loads(line) for line in lines]
        assert all(event["schema_version"] == 1 for event in events)
