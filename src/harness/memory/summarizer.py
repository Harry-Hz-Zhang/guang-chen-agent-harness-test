"""闲置会话的后台记忆提取器（daemon 线程周期扫描）。

判定口径：会话文件 last_modified 距今超过 config.idle_seconds 视为
闲置；消息 ordinal 超过 state.json 记录的提取进度视为有新消息。
提取的 LLM 调用挂在独立 trace 上（kind="idle_summary"）；LLM 输出须为
JSON 对象 {"memories": [...], "tags": [...]}，非法时本轮跳过（进度
不推进，下轮自然重试）。有效记忆直接追加进全局 MEMORY（不去重、
不合并）；单个会话失败只告警，scan_once 永不抛异常。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from harness.config import RuntimeConfig
from harness.llm import Usage
from harness.prompts import MEMORY_EXTRACT_PROMPT

logger = logging.getLogger(__name__)

_THREAD_NAME: str = "harness-memory-summarizer"
_JOIN_TIMEOUT_SECONDS: float = 2.0


def _render_messages(messages: list[dict[str, Any]]) -> str:
    """把消息列表渲染为「角色: 内容」逐行文本（供提取提示词拼接）。"""
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role", "unknown"))
        content = str(message.get("content", ""))
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _parse_extraction(raw: str) -> tuple[list[str], list[str]]:
    """校验并清洗 LLM 提取输出，返回 (memories, tags)；非法时抛 ValueError。

    memories 缺失或非列表视为非法；tags 非列表时容忍为空（次要信息）。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"提取输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("提取输出不是 JSON 对象")
    memories_raw = data.get("memories")
    if not isinstance(memories_raw, list):
        raise ValueError("提取输出缺少 memories 列表")
    memories = [str(item).strip() for item in memories_raw if str(item).strip()]
    tags_raw = data.get("tags", [])
    if isinstance(tags_raw, list):
        tags = [str(item).strip() for item in tags_raw if str(item).strip()]
    else:
        tags = []
    return memories, tags


class MemorySummarizer:
    """周期扫描闲置会话，把新消息提取为长期记忆并追加进全局 MEMORY。"""

    def __init__(
        self,
        sessions: Any,
        memory: Any,
        llm: Any,
        trace: Any,
        config: RuntimeConfig,
    ) -> None:
        """注入会话存储、记忆存储、LLM 客户端、追踪收集器与运行配置。"""
        self._sessions = sessions
        self._memory = memory
        self._llm = llm
        self._trace = trace
        self._config = config
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def scan_once(self) -> list[str]:
        """扫描全部会话，提取「闲置且有未提取消息」的，返回本次提取的 session_id 列表。"""
        summarized: list[str] = []
        try:
            session_ids = self._sessions.session_ids()
        except Exception as exc:
            logger.warning("读取会话列表失败，本轮扫描跳过：%s", exc)
            return summarized
        for session_id in session_ids:
            try:
                if not self._is_idle(session_id):
                    continue
                messages, last_ordinal = self._new_messages(session_id)
                if not messages:
                    continue
                raw = self._extract(session_id, messages)
                memories, tags = _parse_extraction(raw)
                if memories:
                    self._memory.append(memories, tags)
                self._memory.mark_summarized(session_id, last_ordinal)
                summarized.append(session_id)
            except Exception as exc:
                logger.warning(
                    "会话 %s 记忆提取失败，等待下轮扫描重试：%s", session_id, exc
                )
        return summarized

    def start(self) -> None:
        """启动 daemon 扫描线程（幂等：线程存活时不重复启动）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=_THREAD_NAME, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """请求停止扫描线程并等待其退出（未启动时安全）。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    def _run(self) -> None:
        """扫描线程主体：周期执行 scan_once，直到 stop 事件置位。"""
        while not self._stop_event.is_set():
            self.scan_once()
            self._stop_event.wait(self._config.scan_interval_seconds)

    def _is_idle(self, session_id: str) -> bool:
        """判断会话是否闲置：无 mtime 视为不闲置（无法判定）。"""
        last_modified = self._sessions.last_modified(session_id)
        if last_modified is None:
            return False
        return (time.time() - last_modified) >= self._config.idle_seconds

    def _new_messages(self, session_id: str) -> tuple[list[dict[str, Any]], int]:
        """返回该会话进度之后的消息列表与它们覆盖到的最大 ordinal。"""
        last = self._memory.summarized_ordinal(session_id)
        messages: list[dict[str, Any]] = []
        max_ordinal = last
        for record in self._sessions.load_records(session_id):
            if record.get("kind") != "message":
                continue
            ordinal = record.get("ordinal")
            message = record.get("message")
            if not isinstance(ordinal, int) or not isinstance(message, dict):
                continue
            if ordinal > last:
                messages.append(message)
                max_ordinal = max(max_ordinal, ordinal)
        return messages, max_ordinal

    def _extract(self, session_id: str, messages: list[dict[str, Any]]) -> str:
        """对消息列表做 LLM 记忆提取（挂独立 trace 与 span），返回原始输出文本。

        LLM 调用失败时先以 error 结束 span（失败在 trace 中可观测、
        span 元数据不悬空），再把异常上抛给 scan_once 统一降级。
        """
        prompt = MEMORY_EXTRACT_PROMPT + _render_messages(messages)
        request: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        trace_id = self._trace.start_trace(session_id)
        try:
            span_id = self._trace.start_llm_span(
                trace_id, self._config.model, request, kind="idle_summary"
            )
            try:
                message = self._llm.invoke(request)
            except Exception as exc:
                self._trace.end_llm_span(
                    span_id,
                    {"content": ""},
                    Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    "",
                    error={"error.type": type(exc).__name__, "message": str(exc)},
                )
                raise
            usage = getattr(message, "usage", None)
            if usage is None:
                usage = Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
            self._trace.end_llm_span(
                span_id,
                {"content": str(getattr(message, "content", "") or "")},
                usage,
                str(getattr(message, "finish_reason", "") or ""),
            )
            return str(getattr(message, "content", "") or "")
        finally:
            # 回合终局清理（对称 loop._run reverse-sync ⑥）：闲置提取路径同样不泄漏登记表
            self._trace.end_trace(trace_id)
