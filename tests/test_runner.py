"""ConcurrentRunner 的单元测试（全 FakeLLM，零网络，tmp 目录存储）。

ThreadSafeFakeLLM 以会话为键分发脚本（经 CURRENT_SESSION_ID 绑定识别
调用线程所属会话），invoke 内短暂停留放大重叠窗口，用于断言批次
真并发与并发上限；共享单实例被多线程并发调用的出队与计数均持锁。
"""

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.config import RuntimeConfig
from harness.context.builder import ContextBuilder
from harness.llm import AIMessage, LLMError, ToolCall
from harness.loop import ReactLoop
from harness.runner import ConcurrentRunner, SessionJob
from harness.session.store import SessionStore
from harness.state import CURRENT_SESSION_ID, RuntimeState
from harness.tools.base import BaseTool
from harness.tools.registry import ToolRegistry
from harness.tools.todo import WriteTodosTool

_HOLD_SECONDS: float = 0.05
_CONCURRENCY_HOLD_SECONDS: float = 0.2


class _EchoTool(BaseTool):
    """原样返回输入参数 JSON 的测试工具（无状态，可并发共享）。"""

    name = "echo"
    description = "回显测试工具"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute(self, **kwargs: Any) -> str:
        """返回 text 参数原文。"""
        return str(kwargs.get("text", ""))


class ThreadSafeFakeLLM:
    """按会话分发脚本的线程安全假 LLM（记录调用与并发峰值）。

    脚本以 session_id 为键（经 CURRENT_SESSION_ID 识别调用线程所属
    会话），每个会话各持独立队列；invoke 全程持锁出队并维护在途
    计数与峰值，停留 hold_seconds 放大线程重叠窗口。脚本元素为
    Exception 实例时原样抛出（LLM 失败路径）；队列耗尽抛
    AssertionError 锁死调用次数上限。
    """

    def __init__(
        self,
        scripts: dict[str, list[AIMessage | Exception]],
        hold_seconds: float = _HOLD_SECONDS,
    ) -> None:
        """按会话脚本构造（列表拷贝防外部改动），并初始化计数与锁。"""
        self._scripts = {
            session_id: list(script) for session_id, script in scripts.items()
        }
        self._hold_seconds = hold_seconds
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    @property
    def total_calls(self) -> int:
        """返回累计 invoke 次数（持锁读）。"""
        with self._lock:
            return len(self.calls)

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AIMessage:
        """记录调用、停留后按当前会话脚本出队返回（或抛出脚本异常）。"""
        session_id = CURRENT_SESSION_ID.get()
        with self._lock:
            self.calls.append((session_id, [dict(m) for m in messages]))
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(self._hold_seconds)
            with self._lock:
                script = self._scripts.get(session_id)
                if not script:
                    raise AssertionError(
                        f"会话 {session_id} 的 LLM 脚本已耗尽：多余的调用"
                    )
                response = script.pop(0)
        finally:
            with self._lock:
                self._active -= 1
        if isinstance(response, Exception):
            raise response
        return response


def _echo_call(session_id: str, text: str) -> ToolCall:
    """构造 echo 工具调用的 ToolCall（参数已解析形态）。"""
    import json

    return ToolCall(
        id=f"call_{session_id}",
        name="echo",
        arguments_raw=json.dumps({"text": text}, ensure_ascii=False),
        args={"text": text},
    )


def _todos_call(session_id: str, marker: str) -> ToolCall:
    """构造 write_todos 工具调用的 ToolCall（单条带标记的待办）。"""
    import json

    todos = [{"content": f"任务{marker}", "status": "in_progress"}]
    return ToolCall(
        id=f"call_{session_id}",
        name="write_todos",
        arguments_raw=json.dumps({"todos": todos}, ensure_ascii=False),
        args={"todos": todos},
    )


def _make_runner(
    tmp_path: Path,
    scripts: dict[str, list[AIMessage | Exception]],
    config: RuntimeConfig | None = None,
    registry: ToolRegistry | None = None,
    hold_seconds: float = _HOLD_SECONDS,
) -> tuple[ConcurrentRunner, ThreadSafeFakeLLM, SessionStore]:
    """组装注入真实 session/builder/registry 与线程安全 FakeLLM 的 runner。"""
    resolved_config = config if config is not None else RuntimeConfig()
    sessions = SessionStore(tmp_path)
    memory = MagicMock()
    memory.render_index.return_value = None
    builder = ContextBuilder(sessions, memory, resolved_config)
    llm = ThreadSafeFakeLLM(scripts, hold_seconds=hold_seconds)
    resolved_registry = registry if registry is not None else ToolRegistry()
    trace = MagicMock()
    trace.start_trace.return_value = "tid-1"
    trace.start_llm_span.return_value = "span-1"
    trace.start_tool_span.return_value = "tool-span-1"
    loop = ReactLoop(
        llm, resolved_registry, sessions, builder, trace, [], resolved_config
    )
    return ConcurrentRunner(loop, resolved_config), llm, sessions


def _message_roles(sessions: SessionStore, session_id: str) -> list[str]:
    """读取会话全部 message 记录的 role 序列（按落库顺序）。"""
    return [
        str(record["message"].get("role"))
        for record in sessions.load_records(session_id)
        if record.get("kind") == "message"
    ]


class TestRunner:
    """ConcurrentRunner.run_batch 的行为与并发安全用例。"""

    def testBatchRunsAllJobsInOrder(self, tmp_path: Path) -> None:
        """双任务并发执行：结果按提交顺序返回，answer/rounds 各归各。"""
        runner, llm, sessions = _make_runner(
            tmp_path,
            {
                "s-B": [AIMessage(content="完成B")],
                "s-A": [AIMessage(content="完成A")],
            },
        )
        jobs = [
            SessionJob(session_id="s-B", user_input="任务B"),
            SessionJob(session_id="s-A", user_input="任务A"),
        ]
        results = runner.run_batch(jobs)
        assert [result.session_id for result in results] == ["s-B", "s-A"]
        assert results[0].answer == "完成B"
        assert results[1].answer == "完成A"
        assert all(
            result.rounds == 1
            and result.tool_call_count == 0
            and result.truncated is False
            and result.error is None
            for result in results
        )
        assert llm.total_calls == 2

    def testBatchSessionsActuallyConcurrent(self, tmp_path: Path) -> None:
        """双任务真并发：LLM 在途峰值 ≥ 2（串行执行必然只有 1）。"""
        runner, llm, _ = _make_runner(
            tmp_path,
            {
                "s-A": [AIMessage(content="完成A")],
                "s-B": [AIMessage(content="完成B")],
            },
            hold_seconds=_CONCURRENCY_HOLD_SECONDS,
        )
        results = runner.run_batch(
            [
                SessionJob(session_id="s-A", user_input="任务A"),
                SessionJob(session_id="s-B", user_input="任务B"),
            ]
        )
        assert llm.max_active >= 2
        assert all(result.error is None for result in results)

    def testBatchRejectsEmptyJobs(self, tmp_path: Path) -> None:
        """空任务列表直接拒绝。"""
        runner, _, _ = _make_runner(tmp_path, {})
        with pytest.raises(ValueError, match="不能为空"):
            runner.run_batch([])

    def testBatchRejectsBlankSessionId(self, tmp_path: Path) -> None:
        """session_id 空白时拒绝并标注第几个任务。"""
        runner, _, _ = _make_runner(tmp_path, {"s-A": [AIMessage(content="完成A")]})
        with pytest.raises(ValueError, match="session_id"):
            runner.run_batch(
                [
                    SessionJob(session_id="s-A", user_input="任务A"),
                    SessionJob(session_id="  ", user_input="任务B"),
                ]
            )

    def testBatchRejectsBlankInput(self, tmp_path: Path) -> None:
        """user_input 空白时拒绝并点名会话。"""
        runner, _, _ = _make_runner(tmp_path, {"s-A": [AIMessage(content="完成A")]})
        with pytest.raises(ValueError, match="输入"):
            runner.run_batch([SessionJob(session_id="s-A", user_input="  ")])

    def testBatchRejectsDuplicateSessions(self, tmp_path: Path) -> None:
        """批内重复 session_id 拒绝（同一会话不支持并发执行）。"""
        runner, _, _ = _make_runner(
            tmp_path, {"s-A": [AIMessage(content="完成A")]}
        )
        with pytest.raises(ValueError, match="重复"):
            runner.run_batch(
                [
                    SessionJob(session_id="s-A", user_input="任务一"),
                    SessionJob(session_id="s-A", user_input="任务二"),
                ]
            )

    def testLlmErrorCapturedPerJob(self, tmp_path: Path) -> None:
        """单任务 LLM 失败只进该任务 error，不影响其余任务。"""
        runner, _, _ = _make_runner(
            tmp_path,
            {
                "s-bad": [LLMError("LLM 调用失败：网络超时")],
                "s-ok": [AIMessage(content="完成B")],
            },
        )
        results = runner.run_batch(
            [
                SessionJob(session_id="s-bad", user_input="会失败"),
                SessionJob(session_id="s-ok", user_input="任务B"),
            ]
        )
        assert results[0].error is not None
        assert "网络超时" in results[0].error
        assert results[0].answer == ""
        assert results[1].error is None
        assert results[1].answer == "完成B"

    def testBatchSessionFilesIsolated(self, tmp_path: Path) -> None:
        """并发批次各会话文件独立完整：user/assistant/tool/assistant 各归各。"""
        registry = ToolRegistry()
        registry.register(_EchoTool())
        runner, _, sessions = _make_runner(
            tmp_path,
            {
                "s-A": [
                    AIMessage(
                        content="", tool_calls=[_echo_call("s-A", "回显A")]
                    ),
                    AIMessage(content="完成A"),
                ],
                "s-B": [
                    AIMessage(
                        content="", tool_calls=[_echo_call("s-B", "回显B")]
                    ),
                    AIMessage(content="完成B"),
                ],
            },
            registry=registry,
        )
        results = runner.run_batch(
            [
                SessionJob(session_id="s-A", user_input="任务A"),
                SessionJob(session_id="s-B", user_input="任务B"),
            ]
        )
        assert all(result.error is None for result in results)
        assert _message_roles(sessions, "s-A") == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert _message_roles(sessions, "s-B") == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        contents = {
            session_id: [
                str(record["message"].get("content"))
                for record in sessions.load_records(session_id)
                if record.get("kind") == "message"
            ]
            for session_id in ("s-A", "s-B")
        }
        assert any("回显A" in content for content in contents["s-A"])
        assert any("完成A" in content for content in contents["s-A"])
        assert any("回显B" in content for content in contents["s-B"])
        assert any("完成B" in content for content in contents["s-B"])

    def testTodosIsolatedAcrossConcurrentBatch(self, tmp_path: Path) -> None:
        """端到端：并发批次内 write_todos 按会话写入各自 todos。"""
        state = RuntimeState()
        registry = ToolRegistry()
        registry.register(WriteTodosTool(state))
        runner, _, _ = _make_runner(
            tmp_path,
            {
                "s-A": [
                    AIMessage(content="", tool_calls=[_todos_call("s-A", "A")]),
                    AIMessage(content="完成A"),
                ],
                "s-B": [
                    AIMessage(content="", tool_calls=[_todos_call("s-B", "B")]),
                    AIMessage(content="完成B"),
                ],
            },
            registry=registry,
        )
        results = runner.run_batch(
            [
                SessionJob(session_id="s-A", user_input="记录待办A"),
                SessionJob(session_id="s-B", user_input="记录待办B"),
            ]
        )
        assert all(result.error is None for result in results)
        todos_a = state.todos("s-A")
        todos_b = state.todos("s-B")
        assert len(todos_a) == 1 and todos_a[0]["content"] == "任务A"
        assert len(todos_b) == 1 and todos_b[0]["content"] == "任务B"

    def testMaxWorkersRespectsConfig(self, tmp_path: Path) -> None:
        """并发上限受配置约束：max_concurrent_sessions=2 时峰值 ≤ 2。"""
        config = RuntimeConfig()
        config.max_concurrent_sessions = 2
        scripts = {
            f"s-{index}": [AIMessage(content=f"完成{index}")]
            for index in range(6)
        }
        runner, llm, _ = _make_runner(
            tmp_path, scripts, config=config, hold_seconds=_HOLD_SECONDS
        )
        jobs = [
            SessionJob(session_id=f"s-{index}", user_input=f"任务{index}")
            for index in range(6)
        ]
        results = runner.run_batch(jobs)
        assert llm.max_active <= 2
        assert [result.answer for result in results] == [
            f"完成{index}" for index in range(6)
        ]
