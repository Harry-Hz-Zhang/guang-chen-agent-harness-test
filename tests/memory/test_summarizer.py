"""MemorySummarizer 的单元测试（闲置判定 + 后台线程 + 独立 trace）。"""

import threading
import time
from typing import Any
from unittest.mock import MagicMock

from harness.config import RuntimeConfig
from harness.llm import AIMessage
from harness.memory.summarizer import MemorySummarizer
from harness.prompts import MEMORY_SUMMARY_PROMPT

_THREAD_NAME = "harness-memory-summarizer"


def _make_summarizer(
    sessions: Any,
    memory: Any,
    llm: Any,
    trace: Any,
    idle_seconds: int = 0,
    scan_interval_seconds: float = 0.1,
) -> MemorySummarizer:
    """构造注入全部 mock 依赖的 MemorySummarizer。"""
    config = RuntimeConfig(
        idle_seconds=idle_seconds,
        scan_interval_seconds=scan_interval_seconds,
        model="m",
    )
    return MemorySummarizer(sessions, memory, llm, trace, config)


def _idle_sessions() -> MagicMock:
    """构造只有一个闲置会话 s1 的 mock SessionStore。"""
    sessions = MagicMock()
    sessions.session_ids.return_value = ["s1"]
    sessions.last_modified.return_value = time.time() - 100_000
    sessions.read_context_messages.return_value = [
        {"role": "user", "content": "我的猫叫小花"},
        {"role": "assistant", "content": "好的，记住了。"},
    ]
    return sessions


def _fresh_memory() -> MagicMock:
    """构造无记忆（render_summary 为 None）的 mock MemoryStore。"""
    memory = MagicMock()
    memory.render_summary.return_value = None
    return memory


def _summary_llm() -> MagicMock:
    """构造返回固定总结的 mock LLM 客户端。"""
    llm = MagicMock()
    llm.invoke.return_value = AIMessage(content="总结：用户养猫名叫小花")
    return llm


def _mock_trace() -> MagicMock:
    """构造返回固定 trace_id / span_id 的 mock TraceCollector。"""
    trace = MagicMock()
    trace.start_trace.return_value = "tid-1"
    trace.start_llm_span.return_value = "span-1"
    return trace


class TestMemorySummarizer:

    def testScanOnceSummarizesIdle(self) -> None:
        """闲置且未总结的会话被总结一次，prompt 含模板与会话内容。"""
        sessions = _idle_sessions()
        memory = _fresh_memory()
        llm = _summary_llm()
        trace = _mock_trace()
        summarizer = _make_summarizer(sessions, memory, llm, trace, idle_seconds=0)
        assert summarizer.scan_once() == ["s1"]
        memory.write.assert_called_once_with("s1", "总结：用户养猫名叫小花")
        request = llm.invoke.call_args.args[0]
        prompt_text = request[0]["content"]
        assert prompt_text.startswith(MEMORY_SUMMARY_PROMPT)
        assert "我的猫叫小花" in prompt_text

    def testScanSkipsActive(self) -> None:
        """活跃会话（mtime 距今小于闲置阈值）不被总结。"""
        sessions = MagicMock()
        sessions.session_ids.return_value = ["s2"]
        sessions.last_modified.return_value = time.time()
        memory = _fresh_memory()
        llm = _summary_llm()
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace(), idle_seconds=7200)
        assert summarizer.scan_once() == []
        assert memory.write.call_count == 0
        assert llm.invoke.call_count == 0

    def testScanSkipsAlreadySummarized(self) -> None:
        """已有记忆（render_summary 非 None）的会话不重复总结。"""
        sessions = _idle_sessions()
        memory = _fresh_memory()
        memory.render_summary.return_value = "已有记忆"
        llm = _summary_llm()
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace(), idle_seconds=0)
        assert summarizer.scan_once() == []
        assert memory.write.call_count == 0
        assert llm.invoke.call_count == 0

    def testScanFailureRetriesNextRound(self) -> None:
        """LLM 失败时本轮返回空列表不抛异常；恢复后下一轮总结成功。"""
        sessions = _idle_sessions()
        memory = _fresh_memory()
        llm = _summary_llm()
        llm.invoke.side_effect = RuntimeError("boom")
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace(), idle_seconds=0)
        assert summarizer.scan_once() == []
        llm.invoke.side_effect = None
        assert summarizer.scan_once() == ["s1"]
        memory.write.assert_called_once_with("s1", "总结：用户养猫名叫小花")

    def testScanFailureEndsSpanWithError(self) -> None:
        """LLM 失败时 span 以 error 结束（失败在 trace 中可观测）。"""
        sessions = _idle_sessions()
        memory = _fresh_memory()
        llm = _summary_llm()
        llm.invoke.side_effect = RuntimeError("boom")
        trace = _mock_trace()
        summarizer = _make_summarizer(sessions, memory, llm, trace, idle_seconds=0)
        assert summarizer.scan_once() == []
        assert trace.end_llm_span.call_count == 1
        error = trace.end_llm_span.call_args.kwargs.get("error")
        assert error is not None
        assert "boom" in error.get("message", "")

    def testScanSkipsWhenWriteRejected(self) -> None:
        """总结结果被拒写（write 返回 False）时不计入返回列表。"""
        sessions = _idle_sessions()
        memory = _fresh_memory()
        memory.write.return_value = False
        llm = _summary_llm()
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace(), idle_seconds=0)
        assert summarizer.scan_once() == []

    def testStartStopThread(self) -> None:
        """start 启动 daemon 线程，stop 后 2 秒内退出。"""
        sessions = _idle_sessions()
        memory = _fresh_memory()
        llm = _summary_llm()
        summarizer = _make_summarizer(
            sessions, memory, llm, _mock_trace(), idle_seconds=0, scan_interval_seconds=0.05
        )
        summarizer.start()
        try:
            thread = next(t for t in threading.enumerate() if t.name == _THREAD_NAME)
            assert thread.is_alive()
            assert thread.daemon
        finally:
            summarizer.stop()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not any(t.name == _THREAD_NAME and t.is_alive() for t in threading.enumerate()):
                break
            time.sleep(0.02)
        assert not any(t.name == _THREAD_NAME and t.is_alive() for t in threading.enumerate())

    def testScanEmitsIndependentTrace(self) -> None:
        """总结的 LLM 调用挂独立 trace 且 span kind 为 idle_summary。"""
        sessions = _idle_sessions()
        memory = _fresh_memory()
        llm = _summary_llm()
        trace = _mock_trace()
        summarizer = _make_summarizer(sessions, memory, llm, trace, idle_seconds=0)
        assert summarizer.scan_once() == ["s1"]
        trace.start_trace.assert_called_once_with("s1")
        span_call = trace.start_llm_span.call_args
        assert span_call.args[0] == "tid-1"
        assert span_call.kwargs.get("kind") == "idle_summary"
        assert trace.end_llm_span.call_count == 1
