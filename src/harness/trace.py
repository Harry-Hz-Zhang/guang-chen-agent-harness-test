"""TraceCollector 与 JsonlExporter —— LLM 调用与工具调用的迷你 OTel 追踪。

TraceCollector 负责 trace_id / span_id 生成与计时，事件仅在
end_llm_span / end_tool_span 时经 exporter 发出（start_* 不发事件）；
字段命名对齐 OpenTelemetry GenAI 语义约定（gen_ai.* 前缀）。正文字段
逐项截断至 TRACE_MAX_CHARS 字符（全量正文以会话文件为真源，trace
不重复存全量）；导出失败仅告警、绝不向上抛出（不影响主对话流程）。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from harness.llm import ToolCall, Usage

logger = logging.getLogger(__name__)

TRACE_MAX_CHARS: int = 2000
TRUNCATION_SUFFIX: str = "…[已截断，全文见会话记录]"

SCHEMA_VERSION: int = 1
PROVIDER_NAME: str = "deepseek"
FALLBACK_CONVERSATION_ID: str = "unknown"

_TRACE_ID_BYTES: int = 16
_SPAN_ID_BYTES: int = 8


def _truncate(text: str) -> str:
    """正文超长截断：保留前 TRACE_MAX_CHARS 字符并追加固定「已截断」尾注。"""
    if len(text) <= TRACE_MAX_CHARS:
        return text
    return text[:TRACE_MAX_CHARS] + TRUNCATION_SUFFIX


def _utc_now() -> datetime:
    """返回当前 UTC 时刻（带时区）。"""
    return datetime.now(timezone.utc)


def _render_input_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把入参消息浅拷贝后截断 content（不动调用方原 dict，其余键原样保留）。"""
    rendered: list[dict[str, Any]] = []
    for message in messages:
        copied = dict(message)
        content = copied.get("content")
        if isinstance(content, str):
            copied["content"] = _truncate(content)
        rendered.append(copied)
    return rendered


def _render_output_messages(output: dict[str, Any]) -> list[dict[str, Any]]:
    """从输出 dict 渲染单条 assistant 消息（content 截断）。"""
    role = output.get("role", "assistant")
    content = output.get("content", "")
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    return [{"role": role, "content": _truncate(content)}]


class TraceExporter(Protocol):
    """追踪事件导出器协议：export 失败仅告警不抛出。"""

    def export(self, event: dict[str, Any]) -> None: ...


class JsonlExporter:
    """按会话追加写 <trace_dir>/<conversation_id>.jsonl 的事件导出器。

    conversation id 取事件 attributes["gen_ai.conversation.id"]（缺失
    回退 "unknown"），每行一个可独立解析的 JSON 对象；写盘失败仅告警。
    写盘段由内部 threading.Lock 互斥：主线程与 daemon 后台总结线程
    并发 export 时同一文件不出现交错的行（Windows 追加写不保证行
    原子性，故需显式加锁）。
    """

    def __init__(self, trace_dir: Path) -> None:
        """记录目标目录并创建写盘锁（export 时惰性建目录，不在构造期）。"""
        self.trace_dir = Path(trace_dir)
        self._lock = threading.Lock()

    def export(self, event: dict[str, Any]) -> None:
        """把单条事件序列化为一行 JSON 追加写入对应会话的 trace 文件。

        写盘段在锁内执行（mkdir 留锁外，无共享状态）；失败（目录
        不可写、磁盘错误等）仅记录 logging.warning，不抛出。
        """
        attributes = event.get("attributes")
        conversation_id = (
            attributes.get("gen_ai.conversation.id")
            if isinstance(attributes, dict)
            else None
        )
        if not isinstance(conversation_id, str) or not conversation_id:
            conversation_id = FALLBACK_CONVERSATION_ID
        line = json.dumps(event, ensure_ascii=False)
        try:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            path = self.trace_dir / f"{conversation_id}.jsonl"
            with self._lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            logger.warning("trace 事件写盘失败（已忽略，不影响主流程）：%s", exc)


@dataclass
class _LlmSpanMeta:
    """LLM span 的内存元数据（截断只影响事件，不影响此处原文）。"""

    span_id: str
    trace_id: str
    session_id: str
    model: str
    messages: list[dict[str, Any]]
    span_kind: str
    start_time: datetime
    parent_span_id: str | None = None


@dataclass
class _ToolSpanMeta:
    """工具 span 的内存元数据。"""

    span_id: str
    trace_id: str
    session_id: str
    parent_span_id: str
    tool_name: str
    tool_call_id: str
    arguments_raw: str
    start_time: datetime


class TraceCollector:
    """追踪收集器：管 ID 生成、span 树与计时，end_* 时发出事件。

    span 树结构：工具 span 以其所属 LLM span 为父；压缩摘要与闲置
    总结的 LLM 调用分别以 harness.span.kind="compaction" /
    "idle_summary" 标识。exporter 导出失败被捕获并告警（0 次上抛）。
    """

    def __init__(self, exporter: TraceExporter) -> None:
        """注入导出器并初始化 trace 与 span 的内存索引。"""
        self._exporter = exporter
        self._trace_sessions: dict[str, str] = {}
        self._spans: dict[str, _LlmSpanMeta | _ToolSpanMeta] = {}

    def start_trace(self, session_id: str) -> str:
        """生成新 trace_id（32 位小写 hex）并记录其与会话的映射，不发事件。"""
        trace_id = secrets.token_hex(_TRACE_ID_BYTES)
        self._trace_sessions[trace_id] = session_id
        return trace_id

    def start_llm_span(
        self,
        trace_id: str,
        model: str,
        messages: list[dict[str, Any]],
        kind: str = "chat",
    ) -> str:
        """生成 LLM span（16 位小写 hex span_id），记录元数据待 end 时发事件。

        kind 取 "chat" / "compaction" / "idle_summary"，落事件时写入
        attributes["harness.span.kind"]。
        """
        span_id = secrets.token_hex(_SPAN_ID_BYTES)
        self._spans[span_id] = _LlmSpanMeta(
            span_id=span_id,
            trace_id=trace_id,
            session_id=self._trace_sessions.get(trace_id, FALLBACK_CONVERSATION_ID),
            model=model,
            messages=messages,
            span_kind=kind,
            start_time=_utc_now(),
        )
        return span_id

    def end_llm_span(
        self,
        span_id: str,
        output: dict[str, Any],
        usage: Usage,
        finish_reason: str,
        error: dict[str, Any] | None = None,
    ) -> None:
        """结束 LLM span 并发出 type=="llm" 事件（含用量与截断后的正文）。"""
        span = self._spans.pop(span_id, None)
        if not isinstance(span, _LlmSpanMeta):
            logger.warning("end_llm_span 收到未知 span_id：%s（事件被丢弃）", span_id)
            return
        end_time = _utc_now()
        status = "error" if error is not None else "ok"
        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "type": "llm",
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "status": status,
            "start_time": span.start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_ms": int((end_time - span.start_time).total_seconds() * 1000),
            "error": error,
            "attributes": {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": PROVIDER_NAME,
                "gen_ai.request.model": span.model,
                "gen_ai.conversation.id": span.session_id,
                "gen_ai.usage.input_tokens": usage.prompt_tokens,
                "gen_ai.usage.output_tokens": usage.completion_tokens,
                "gen_ai.usage.reasoning_tokens": usage.reasoning_tokens,
                "gen_ai.response.finish_reasons": [finish_reason],
                "gen_ai.input.messages": _render_input_messages(span.messages),
                "gen_ai.output.messages": _render_output_messages(output),
                "harness.span.kind": span.span_kind,
            },
        }
        self._emit(event)

    def start_tool_span(
        self, trace_id: str, parent_span_id: str, call: ToolCall
    ) -> str:
        """生成以所属 LLM span 为父的工具 span，记录元数据待 end 时发事件。"""
        span_id = secrets.token_hex(_SPAN_ID_BYTES)
        self._spans[span_id] = _ToolSpanMeta(
            span_id=span_id,
            trace_id=trace_id,
            session_id=self._trace_sessions.get(trace_id, FALLBACK_CONVERSATION_ID),
            parent_span_id=parent_span_id,
            tool_name=call.name,
            tool_call_id=call.id,
            arguments_raw=call.arguments_raw,
            start_time=_utc_now(),
        )
        return span_id

    def end_tool_span(
        self,
        span_id: str,
        result: str | None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """结束工具 span 并发出 type=="tool" 事件（result 为 None 时记 null）。"""
        span = self._spans.pop(span_id, None)
        if not isinstance(span, _ToolSpanMeta):
            logger.warning("end_tool_span 收到未知 span_id：%s（事件被丢弃）", span_id)
            return
        end_time = _utc_now()
        status = "error" if error is not None else "ok"
        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "type": "tool",
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "status": status,
            "start_time": span.start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_ms": int((end_time - span.start_time).total_seconds() * 1000),
            "error": error,
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": span.tool_name,
                "gen_ai.tool.call.id": span.tool_call_id,
                "gen_ai.tool.call.arguments": _truncate(span.arguments_raw),
                "gen_ai.tool.call.result": (
                    _truncate(result) if result is not None else None
                ),
                "gen_ai.conversation.id": span.session_id,
            },
        }
        self._emit(event)

    def _emit(self, event: dict[str, Any]) -> None:
        """把事件交给 exporter；导出抛任何异常都捕获并告警，不上抛。"""
        try:
            self._exporter.export(event)
        except Exception as exc:
            logger.warning("trace 事件导出失败（已忽略，不影响主流程）：%s", exc)
