"""闲置会话的后台记忆总结器（daemon 线程周期扫描）。

判定口径：会话文件 last_modified 距今超过 config.idle_seconds 视为
闲置；memory.render_summary 非 None 视为已总结过（跳过）。总结的
LLM 调用挂在独立 trace 上（kind="idle_summary"）；单个会话总结失败
只告警、留给下轮扫描重试，scan_once 永不抛异常。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from harness.config import RuntimeConfig
from harness.llm import Usage
from harness.prompts import MEMORY_SUMMARY_PROMPT

logger = logging.getLogger(__name__)

_THREAD_NAME: str = "harness-memory-summarizer"
_JOIN_TIMEOUT_SECONDS: float = 2.0


def _render_messages(messages: list[dict[str, Any]]) -> str:
    """把消息列表渲染为「角色: 内容」逐行文本（供总结提示词拼接）。"""
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role", "unknown"))
        content = str(message.get("content", ""))
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


class MemorySummarizer:
    """周期扫描闲置会话并调用 LLM 总结为长期记忆。"""

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
        """扫描全部会话，总结「闲置且未总结」的，返回本次总结的 session_id 列表。"""
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
                if self._memory.render_summary(session_id) is not None:
                    continue
                content = self._summarize_session(session_id)
                if self._memory.write(session_id, content):
                    summarized.append(session_id)
            except Exception as exc:
                logger.warning("会话 %s 总结失败，等待下轮扫描重试：%s", session_id, exc)
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

    def _summarize_session(self, session_id: str) -> str:
        """对单个会话生成记忆总结文本（LLM 调用挂独立 trace 与 span）。

        LLM 调用失败时先以 error 结束 span（失败在 trace 中可观测、
        span 元数据不悬空），再把异常上抛给 scan_once 统一降级。
        """
        messages = self._sessions.read_context_messages(session_id)
        prompt = MEMORY_SUMMARY_PROMPT + _render_messages(messages)
        request: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        trace_id = self._trace.start_trace(session_id)
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
