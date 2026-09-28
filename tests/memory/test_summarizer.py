"""MemorySummarizer（闲置增量提取）的单元测试 —— 对应 refactor-global-memory tasks.md Task 2 RED 条目。"""

import json
import time
from typing import Any
from unittest.mock import MagicMock

from harness.config import RuntimeConfig
from harness.llm import AIMessage
from harness.memory.summarizer import MemorySummarizer
from harness.prompts import MEMORY_EXTRACT_PROMPT

_THREAD_NAME = "harness-memory-summarizer"

_VALID_EXTRACTION = json.dumps(
    {"memories": ["用户养猫名叫小花"], "tags": ["个人", "宠物"]},
    ensure_ascii=False,
)


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


def _message_records(*contents: str) -> list[dict[str, Any]]:
    """构造带 ordinal 的 user 消息记录列表（ordinal 从 0 递增）。"""
    return [
        {
            "ordinal": ordinal,
            "kind": "message",
            "message": {"role": "user", "content": content},
        }
        for ordinal, content in enumerate(contents)
    ]


def _idle_sessions(records: list[dict[str, Any]]) -> MagicMock:
    """构造只有一个闲置会话 s1 的 mock SessionStore。"""
    sessions = MagicMock()
    sessions.session_ids.return_value = ["s1"]
    sessions.last_modified.return_value = time.time() - 100_000
    sessions.load_records.return_value = records
    return sessions


def _fresh_memory(summarized_ordinal: int = -1) -> MagicMock:
    """构造提取进度为 summarized_ordinal 的 mock MemoryStore。"""
    memory = MagicMock()
    memory.summarized_ordinal.return_value = summarized_ordinal
    return memory


def _extraction_llm(content: str) -> MagicMock:
    """构造返回固定文本的 mock LLM 客户端。"""
    llm = MagicMock()
    llm.invoke.return_value = AIMessage(content=content)
    return llm


def _mock_trace() -> MagicMock:
    """构造返回固定 trace_id / span_id 的 mock TraceCollector。"""
    trace = MagicMock()
    trace.start_trace.return_value = "tid-1"
    trace.start_llm_span.return_value = "span-1"
    return trace


class TestMemorySummarizer:

    def testScanOnceExtractsIdleSessionNewMessages(self) -> None:
        """闲置且有新消息：提取一次，追加 memories/tags 并推进进度到最大 ordinal。"""
        sessions = _idle_sessions(_message_records("我的猫叫小花", "记一下"))
        memory = _fresh_memory()
        llm = _extraction_llm(_VALID_EXTRACTION)
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace())
        assert summarizer.scan_once() == ["s1"]
        memory.append.assert_called_once_with(
            ["用户养猫名叫小花"], ["个人", "宠物"]
        )
        memory.mark_summarized.assert_called_once_with("s1", 1)
        prompt_text = llm.invoke.call_args.args[0][0]["content"]
        assert prompt_text.startswith(MEMORY_EXTRACT_PROMPT)
        assert "我的猫叫小花" in prompt_text
        assert "记一下" in prompt_text

    def testScanOnceOnlySendsMessagesAfterOrdinal(self) -> None:
        """增量提取：进度之后的消息才进入提取 prompt。"""
        sessions = _idle_sessions(_message_records("旧消息A", "旧消息B", "新消息C"))
        memory = _fresh_memory(summarized_ordinal=1)
        llm = _extraction_llm(_VALID_EXTRACTION)
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace())
        assert summarizer.scan_once() == ["s1"]
        prompt_text = llm.invoke.call_args.args[0][0]["content"]
        assert "新消息C" in prompt_text
        assert "旧消息A" not in prompt_text
        assert "旧消息B" not in prompt_text
        memory.mark_summarized.assert_called_once_with("s1", 2)

    def testScanOnceSkipsWhenNoNewMessages(self) -> None:
        """无新消息：不调 LLM、不写入，返回空。"""
        sessions = _idle_sessions(_message_records("已有消息"))
        memory = _fresh_memory(summarized_ordinal=0)
        llm = _extraction_llm(_VALID_EXTRACTION)
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace())
        assert summarizer.scan_once() == []
        llm.invoke.assert_not_called()
        memory.append.assert_not_called()

    def testScanOnceSkipsActiveSession(self) -> None:
        """活跃会话：跳过，不调 LLM。"""
        sessions = MagicMock()
        sessions.session_ids.return_value = ["s1"]
        sessions.last_modified.return_value = time.time()
        llm = _extraction_llm(_VALID_EXTRACTION)
        summarizer = _make_summarizer(sessions, _fresh_memory(), llm, _mock_trace())
        assert summarizer.scan_once() == []
        llm.invoke.assert_not_called()

    def testScanOnceInvalidJsonSkipsAndKeepsProgress(self) -> None:
        """LLM 输出非 JSON：本轮跳过、进度不推进（下轮重试）。"""
        sessions = _idle_sessions(_message_records("我的猫叫小花"))
        memory = _fresh_memory()
        llm = _extraction_llm("这不是 JSON")
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace())
        assert summarizer.scan_once() == []
        memory.append.assert_not_called()
        memory.mark_summarized.assert_not_called()

    def testScanOnceNonObjectJsonSkips(self) -> None:
        """LLM 输出非对象 JSON（数组）：跳过且进度不推进。"""
        sessions = _idle_sessions(_message_records("我的猫叫小花"))
        memory = _fresh_memory()
        llm = _extraction_llm("[1, 2]")
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace())
        assert summarizer.scan_once() == []
        memory.mark_summarized.assert_not_called()

    def testScanOnceEmptyMemoriesAdvancesProgressOnly(self) -> None:
        """空 memories 合法：不写记忆但推进进度。"""
        sessions = _idle_sessions(_message_records("闲聊", "再见"))
        memory = _fresh_memory()
        llm = _extraction_llm('{"memories": [], "tags": []}')
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace())
        assert summarizer.scan_once() == ["s1"]
        memory.append.assert_not_called()
        memory.mark_summarized.assert_called_once_with("s1", 1)

    def testScanOnceMissingTagsToleratedAsEmpty(self) -> None:
        """缺少 tags 字段容忍为空列表。"""
        sessions = _idle_sessions(_message_records("我的猫叫小花"))
        memory = _fresh_memory()
        llm = _extraction_llm('{"memories": ["用户养猫名叫小花"]}')
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace())
        assert summarizer.scan_once() == ["s1"]
        memory.append.assert_called_once_with(["用户养猫名叫小花"], [])

    def testScanOnceLlmFailureLoggedAndSkipped(self) -> None:
        """LLM 调用失败：返回空、进度不推进、span 以 error 结束。"""
        sessions = _idle_sessions(_message_records("我的猫叫小花"))
        memory = _fresh_memory()
        llm = MagicMock()
        llm.invoke.side_effect = RuntimeError("api down")
        trace = _mock_trace()
        summarizer = _make_summarizer(sessions, memory, llm, trace)
        assert summarizer.scan_once() == []
        memory.mark_summarized.assert_not_called()
        assert trace.end_llm_span.call_args.kwargs.get("error") is not None

    def testScanOnceMultipleSessionsIndependent(self) -> None:
        """多会话：仅提取有新消息的会话，另一个零 LLM 调用。"""
        sessions = MagicMock()
        sessions.session_ids.return_value = ["s1", "s2"]
        sessions.last_modified.return_value = time.time() - 100_000
        sessions.load_records.side_effect = [
            _message_records("s1 的新消息"),
            _message_records("s2 的旧消息"),
        ]
        memory = MagicMock()
        memory.summarized_ordinal.side_effect = [-1, 0]
        llm = _extraction_llm(_VALID_EXTRACTION)
        summarizer = _make_summarizer(sessions, memory, llm, _mock_trace())
        assert summarizer.scan_once() == ["s1"]
        assert llm.invoke.call_count == 1
        memory.append.assert_called_once()

    def testStartStopThreadLifecycle(self) -> None:
        """start 幂等、stop 安全（线程按命名退出）。"""
        sessions = MagicMock()
        sessions.session_ids.return_value = []
        summarizer = _make_summarizer(
            sessions,
            MagicMock(),
            MagicMock(),
            _mock_trace(),
            scan_interval_seconds=0.05,
        )
        summarizer.start()
        summarizer.start()
        thread = summarizer._thread
        assert thread is not None
        assert thread.is_alive()
        assert thread.name == _THREAD_NAME
        summarizer.stop()
        thread.join(timeout=1.0)
        assert not thread.is_alive()
