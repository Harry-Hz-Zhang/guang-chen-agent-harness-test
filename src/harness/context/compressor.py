"""ContextCompressor 与 CompactionMiddleware —— 超长上下文的基础压缩。

触发与统计口径均为「未压缩窗口」（已压缩区间不计入），压缩后计数
回落、不会每轮重复触发。压缩不删除任何原始消息：仅对窗口内保留区
之前的历史生成 5 字段结构化摘要（COMPACTION_PROMPT），并把压缩记录
（区间上限 ordinal + 摘要）追加写入会话文件；保留最近 keep_recent_rounds
轮原文，切点保证 assistant(tool_calls)/tool 配对不拆散（必要时回退到
轮起点）。压缩的 LLM 调用经 start_llm_span(kind="compaction") 挂 span；
失败降级为告警，不阻断主循环（由 CompactionMiddleware 保证）。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.config import RuntimeConfig
from harness.context.builder import estimate_tokens
from harness.llm import Usage
from harness.middleware import LoopState, Middleware
from harness.prompts import COMPACTION_PROMPT
from harness.session.store import COMPACTION_SUMMARY_NAME

logger = logging.getLogger(__name__)

_RENDER_TOOL_RESULT_MAX_CHARS: int = 500
_RENDER_TRUNCATION_NOTE: str = "…[工具结果超长已截断，全文见会话记录]"
_OLD_SUMMARY_HEADER: str = "（此前压缩摘要）\n"


class ContextCompressor:
    """按未压缩窗口判定触发并执行压缩（依赖全部注入）。"""

    def __init__(
        self, sessions: Any, llm: Any, trace: Any, config: RuntimeConfig
    ) -> None:
        """注入会话存储、LLM 客户端、追踪收集器与运行配置。"""
        self._sessions = sessions
        self._llm = llm
        self._trace = trace
        self._config = config

    def should_compact(self, session_id: str) -> bool:
        """判定是否触发压缩：未压缩窗口内对话轮或估算 token 任一达阈值。"""
        if self.uncompressed_rounds(session_id) >= self._config.compact_rounds:
            return True
        context = self._sessions.read_context_messages(session_id)
        tokens = estimate_tokens(
            [m for m in context if m.get("name") != COMPACTION_SUMMARY_NAME]
        )
        return tokens >= self._config.compact_tokens

    def uncompressed_rounds(self, session_id: str) -> int:
        """统计未压缩窗口内的对话轮数（user 消息数；已压缩区间不计入）。"""
        window = self._uncompressed_window(session_id)
        return sum(
            1
            for record in window
            if record["message"].get("role") == "user"
        )

    def compact(self, session_id: str, trace_id: str) -> None:
        """执行压缩：生成摘要并追加压缩记录（保留最近 keep_recent_rounds 轮原文）。

        LLM 调用经 start_llm_span(kind="compaction") 包裹挂 trace_id
        下；链式压缩时旧摘要并入渲染输入。
        """
        window = self._uncompressed_window(session_id)
        if not window: return

        cut = self._compute_cut(window)
        if cut is None: return

        to_compress = [r for r in window if r["ordinal"] <= cut]
        if not to_compress: return

        rendering = self._render_for_summary(session_id, to_compress)
        request: list[dict[str, Any]] = [
            {"role": "user", "content": COMPACTION_PROMPT + rendering}
        ]
        span_id = self._trace.start_llm_span(
            trace_id, self._config.model, request, kind="compaction"
        )
        message = self._llm.invoke(request)
        usage = getattr(message, "usage", None)
        if usage is None:
            usage = Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        self._trace.end_llm_span(
            span_id,
            {"content": str(getattr(message, "content", "") or "")},
            usage,
            str(getattr(message, "finish_reason", "") or ""),
        )
        self._sessions.append_compaction(
            session_id, cut, str(message.content), self._config.model
        )

    def _message_records(self, session_id: str) -> list[dict[str, Any]]:
        """读取会话全部 message 记录（含 ordinal，损坏行已被 store 过滤）。"""
        raw = self._sessions.load_records(session_id)
        return [
            record
            for record in raw
            if record.get("kind") == "message"
            and isinstance(record.get("ordinal"), int)
            and isinstance(record.get("message"), dict)
        ]

    def _uncompressed_window(self, session_id: str) -> list[dict[str, Any]]:
        """返回最后一次压缩之后的 message 记录（按 ordinal 升序）。"""
        records = self._message_records(session_id)
        window_start = self._last_compressed_up_to(session_id)
        return [r for r in records if r["ordinal"] > window_start]

    def _last_compressed_up_to(self, session_id: str) -> int:
        """取最后一次压缩记录的区间上限 ordinal（无压缩记录返回 -1 表示全部未压缩）。"""
        raw = self._sessions.load_records(session_id)
        last: int = -1
        for record in raw:
            if record.get("kind") == "compaction":
                value = record.get("compressed_up_to")
                if isinstance(value, int):
                    last = value
        return last

    def _compute_cut(self, window: list[dict[str, Any]]) -> int | None:
        """计算保留边界：保留最近 keep_recent_rounds 轮，返回被压区间上限 ordinal。

        切点回退：若保留边界把某对 assistant(tool_calls)/tool 配对拆到
        两侧，回退到该轮（配对所属 assistant 所在轮）的起点之前。
        """
        user_positions = [
            i
            for i, record in enumerate(window)
            if record["message"].get("role") == "user"
        ]
        if len(user_positions) <= self._config.keep_recent_rounds:
            return None
        boundary = user_positions[-self._config.keep_recent_rounds]
        boundary = self._retreat_for_tool_pairs(window, boundary)
        if boundary <= 0:
            return None
        return window[boundary - 1]["ordinal"]

    def _retreat_for_tool_pairs(
        self, window: list[dict[str, Any]], boundary: int
    ) -> int:
        """检查保留边界是否拆散工具配对，必要时回退到配对所属轮的起点。"""
        compressed_ids = self._tool_call_ids(window[:boundary])
        for index in range(boundary, len(window)):
            message = window[index]["message"]
            if message.get("role") != "tool":
                continue
            call_id = message.get("tool_call_id")
            if call_id in compressed_ids:
                # 配对被拆散：回退到发出该调用的 assistant 所在轮的起点（user 消息处）
                pair_index = self._find_assistant_with_call(window[:boundary], call_id)
                if pair_index is not None:
                    round_start = self._round_start_index(window, pair_index)
                    return self._retreat_for_tool_pairs(window, round_start)
        return boundary

    def _tool_call_ids(self, records: list[dict[str, Any]]) -> set[str]:
        """收集记录区间内全部 assistant 工具调用的 id。"""
        ids: set[str] = set()
        for record in records:
            message = record["message"]
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    ids.add(call["id"])
        return ids

    def _find_assistant_with_call(
        self, records: list[dict[str, Any]], call_id: str
    ) -> int | None:
        """返回发出指定工具调用的 assistant 在 window 中的索引（找不到为 None）。"""
        for index in range(len(records) - 1, -1, -1):
            message = records[index]["message"]
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id") == call_id:
                    return index
        return None

    def _round_start_index(
        self, window: list[dict[str, Any]], index: int
    ) -> int:
        """从指定索引向前找到所在轮的起点（最近的 user 消息索引）。"""
        for i in range(index, -1, -1):
            if window[i]["message"].get("role") == "user":
                return i
        return 0

    def _render_for_summary(
        self, session_id: str, to_compress: list[dict[str, Any]]
    ) -> str:
        """渲染被压缩区间为提示词输入（含旧摘要链式并入与工具结果截断）。"""
        parts: list[str] = []
        old_summary = self._last_summary(session_id)
        if old_summary:
            parts.append(_OLD_SUMMARY_HEADER + old_summary)
        for record in to_compress:
            parts.append(self._render_message(record["message"]))
        return "\n".join(parts)

    def _render_message(self, message: dict[str, Any]) -> str:
        """渲染单条消息：普通消息为「角色: 内容」，工具调用与结果带标记。"""
        role = message.get("role", "unknown")
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        if role == "tool":
            if len(text) > _RENDER_TOOL_RESULT_MAX_CHARS:
                text = text[:_RENDER_TOOL_RESULT_MAX_CHARS] + _RENDER_TRUNCATION_NOTE
            return f"[工具结果 {message.get('tool_call_id', '')}] {text}"
        calls = message.get("tool_calls")
        if role == "assistant" and calls:
            rendered_calls = "; ".join(
                f"{call.get('function', {}).get('name', '')}({call.get('function', {}).get('arguments', '')})"
                for call in calls
                if isinstance(call, dict)
            )
            return f"assistant: {text}[调用工具: {rendered_calls}]"
        return f"{role}: {text}"

    def _last_summary(self, session_id: str) -> str | None:
        """取最后一次压缩记录的摘要正文（无则 None）。"""
        raw = self._sessions.load_records(session_id)
        last: str | None = None
        for record in raw:
            if record.get("kind") == "compaction":
                summary = record.get("summary")
                if isinstance(summary, str):
                    last = summary
        return last


class CompactionMiddleware(Middleware):
    """把压缩检查挂在 before_model 的中间件（失败降级不阻断）。"""

    def __init__(self, compressor: ContextCompressor) -> None:
        """注入压缩器。"""
        self._compressor = compressor

    def before_model(self, state: LoopState) -> None:
        """每轮模型调用前检查触发压缩；压缩失败仅告警，不修改 state。"""
        try:
            if self._compressor.should_compact(state.session_id):
                self._compressor.compact(state.session_id, state.trace_id)
        except Exception as exc:
            logger.warning("上下文压缩失败（已降级为不压缩）：%s", exc)
