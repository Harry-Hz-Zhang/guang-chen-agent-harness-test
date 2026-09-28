"""ReactLoop 的单元测试（全 FakeLLM，零网络，tmp 目录存储）。"""

import json
import time
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

from harness.config import RuntimeConfig
from harness.context.builder import ContextBuilder
from harness.llm import AIMessage, ToolCall
from harness.loop import ReactLoop
from harness.middleware import LoopState, Middleware
from harness.session.store import SessionStore
from harness.tools.base import BaseTool
from harness.tools.calculator import CalculatorTool
from harness.tools.registry import ToolRegistry
from harness.tools.weather import WeatherTool


class FakeLLM:
    """按脚本依序返回 AIMessage 的假 LLM 客户端（记录每次请求消息）。

    脚本耗尽时抛 AssertionError——天然锁死 LLM 调用次数上限（truncated
    路径不得发起超额调用的 verify 手段）。stream_mode 为 True 时走
    stream/collect_stream 路径（供流式分支用例）。
    """

    def __init__(self, responses: list[AIMessage], stream_mode: bool = False) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.stream_mode = stream_mode
        self.stream_calls: list[list[dict[str, Any]]] = []

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AIMessage:
        """记录请求消息并依序返回脚本响应。"""
        self.calls.append([dict(message) for message in messages])
        if not self.responses:
            raise AssertionError("多余的 LLM 调用：脚本已耗尽")
        return self.responses.pop(0)

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """流式路径：记录请求后按事件序列产出（文本一次性分片）。"""
        self.stream_calls.append([dict(message) for message in messages])
        if not self.responses:
            raise AssertionError("多余的 LLM 调用：脚本已耗尽")
        message = self.responses.pop(0)
        from harness.llm import TextDelta

        events: list[Any] = [TextDelta(text=message.content)]
        yield from events


class RecordingMiddleware(Middleware):
    """记录各钩子调用次数与 state 快照的中间件。"""

    def __init__(self) -> None:
        self.before_model_count: int = 0
        self.after_model_count: int = 0
        self.wrap_tool_call_count: int = 0
        self.states: list[LoopState] = []

    def before_model(self, state: LoopState) -> None:
        """计数并快照 state。"""
        self.before_model_count += 1
        self.states.append(state)

    def after_model(self, state: LoopState) -> None:
        """计数 after_model。"""
        self.after_model_count += 1

    def wrap_tool_call(self, call: ToolCall, execute: Callable[[ToolCall], str]) -> str:
        """计数 wrap_tool_call 并透传执行。"""
        self.wrap_tool_call_count += 1
        return execute(call)


class _BoomTool(BaseTool):
    """总是抛 RuntimeError 的工具（错误回传路径）。"""

    name = "boom"
    description = "总是抛异常的测试工具"
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs: Any) -> str:
        """抛出 RuntimeError('boom')。"""
        raise RuntimeError("boom")


class _SlowTool(BaseTool):
    """执行耗时超过测试超时阈值的慢工具。"""

    name = "slow"
    description = "慢速测试工具"
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs: Any) -> str:
        """睡 50ms 后返回。"""
        time.sleep(0.05)
        return "slow done"


class _CountingTool(BaseTool):
    """计数 execute 次数的工具（非法参数路径 0 次执行断言）。"""

    name = "counter"
    description = "计数测试工具"
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }

    def __init__(self) -> None:
        self.execute_count: int = 0

    def execute(self, **kwargs: Any) -> str:
        """计数并返回。"""
        self.execute_count += 1
        return "ok"


def _mock_trace() -> MagicMock:
    """构造返回固定 ID 的 mock TraceCollector。"""
    trace = MagicMock()
    trace.start_trace.return_value = "tid-1"
    trace.start_llm_span.side_effect = ["span-a", "span-b", "span-c"]
    trace.start_tool_span.side_effect = ["tool-span-1", "tool-span-2"]
    return trace


def _make_loop(
    tmp_path: Path,
    responses: list[AIMessage],
    registry: ToolRegistry | None = None,
    middlewares: list[Middleware] | None = None,
    config: RuntimeConfig | None = None,
    trace: Any = None,
    memory: Any = None,
    fake_stream: bool = False,
) -> tuple[Any, FakeLLM, SessionStore]:
    """组装注入全部依赖的 ReactLoop（真实 session/builder/registry，LLM 为 Fake）。"""
    resolved_config = config if config is not None else RuntimeConfig()
    sessions = SessionStore(tmp_path)
    if memory is None:
        memory = MagicMock()
        memory.render_index.return_value = None
    builder = ContextBuilder(sessions, memory, resolved_config)
    llm = FakeLLM(responses, stream_mode=fake_stream)
    resolved_registry = registry if registry is not None else ToolRegistry()
    resolved_trace = trace if trace is not None else _mock_trace()
    loop = ReactLoop(
        llm,
        resolved_registry,
        sessions,
        builder,
        resolved_trace,
        middlewares if middlewares is not None else [],
        resolved_config,
    )
    return loop, llm, sessions


def _messages(sessions: SessionStore, session_id: str) -> list[dict[str, Any]]:
    """读取会话全部 message 记录的消息体（按落库顺序）。"""
    return [
        record["message"]
        for record in sessions.load_records(session_id)
        if record.get("kind") == "message"
    ]


class TestReactLoop:

    def testDirectAnswer(self, tmp_path: Path) -> None:
        """无工具调用：一轮 LLM 即返回最终答案。"""
        loop, llm, _ = _make_loop(tmp_path, [AIMessage(content="你好")])
        result = loop.run("在吗", "s1")
        assert result.answer == "你好"
        assert result.rounds == 1
        assert result.tool_call_count == 0
        assert result.truncated is False
        assert len(llm.calls) == 1

    def testSingleToolRound(self, tmp_path: Path) -> None:
        """单工具调用：calculator 真注册，结果回传后第二轮给出答案。"""
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        call = ToolCall(
            id="call_1",
            name="calculator",
            arguments_raw='{"expression": "2+3*4"}',
            args={"expression": "2+3*4"},
        )
        responses = [
            AIMessage(content="", tool_calls=[call]),
            AIMessage(content="14"),
        ]
        loop, llm, sessions = _make_loop(tmp_path, responses, registry=registry)
        result = loop.run("计算 2+3*4", "s1")
        assert result.answer == "14"
        assert result.rounds == 2
        assert result.tool_call_count == 1
        assert len(llm.calls) == 2
        tool_messages = [m for m in _messages(sessions, "s1") if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0]["content"] == "14"

    def testSequentialToolCalls(self, tmp_path: Path) -> None:
        """连续两轮工具调用：先查天气再计算，两结果均落库。"""
        registry = ToolRegistry()
        registry.register(WeatherTool())
        registry.register(CalculatorTool())
        weather_call = ToolCall(
            id="call_w",
            name="weather",
            arguments_raw='{"city": "北京"}',
            args={"city": "北京"},
        )
        calc_call = ToolCall(
            id="call_c",
            name="calculator",
            arguments_raw='{"expression": "16*2"}',
            args={"expression": "16*2"},
        )
        responses = [
            AIMessage(content="", tool_calls=[weather_call]),
            AIMessage(content="", tool_calls=[calc_call]),
            AIMessage(content="最终答案"),
        ]
        loop, llm, sessions = _make_loop(tmp_path, responses, registry=registry)
        result = loop.run("北京天气如何？再帮我算个数", "s1")
        assert result.answer == "最终答案"
        assert result.rounds == 3
        assert result.tool_call_count == 2
        assert len(llm.calls) == 3
        tool_messages = [m for m in _messages(sessions, "s1") if m.get("role") == "tool"]
        assert len(tool_messages) == 2
        assert tool_messages[0]["tool_call_id"] == "call_w"
        assert "北京" in tool_messages[0]["content"]
        assert tool_messages[1]["tool_call_id"] == "call_c"
        assert tool_messages[1]["content"] == "32"

    def testMaxRoundsTruncation(self, tmp_path: Path) -> None:
        """达到最大轮次即截断：不发起超额 LLM 调用。"""
        config = RuntimeConfig(max_rounds=2)
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        responses = [
            AIMessage(content="", tool_calls=[ToolCall(
                id="call_1", name="calculator",
                arguments_raw='{"expression": "1+1"}', args={"expression": "1+1"},
            )]),
            AIMessage(content="", tool_calls=[ToolCall(
                id="call_2", name="calculator",
                arguments_raw='{"expression": "2+2"}', args={"expression": "2+2"},
            )]),
        ]
        loop, llm, _ = _make_loop(tmp_path, responses, registry=registry, config=config)
        result = loop.run("一直调工具", "s1")
        assert result.truncated is True
        assert "最大轮次" in result.answer
        assert result.rounds == 2
        assert len(llm.calls) == 2

    def testToolErrorStructuredReturn(self, tmp_path: Path) -> None:
        """工具抛异常：错误结构化回传，第二轮 LLM 收到 error JSON。"""
        registry = ToolRegistry()
        registry.register(_BoomTool())
        call = ToolCall(id="call_1", name="boom", arguments_raw="{}", args={})
        responses = [
            AIMessage(content="", tool_calls=[call]),
            AIMessage(content="工具炸了，我来解释"),
        ]
        loop, llm, _ = _make_loop(tmp_path, responses, registry=registry)
        result = loop.run("调一个会炸的工具", "s1")
        assert result.answer == "工具炸了，我来解释"
        round2_messages = llm.calls[1]
        tool_messages = [m for m in round2_messages if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        payload = json.loads(tool_messages[0]["content"])
        assert payload["error"]["type"] == "ToolExecutionError"
        assert "boom" in payload["error"]["message"]
        assert payload["error"]["tool"] == "boom"
        assert payload["error"]["tool_call_id"] == "call_1"

    def testToolErrorNoSideEffects(self, tmp_path: Path) -> None:
        """工具错误不写长期记忆、不派生额外记录、LoopResult 正常返回。"""
        memory = MagicMock()
        memory.render_index.return_value = None
        registry = ToolRegistry()
        registry.register(_BoomTool())
        call = ToolCall(id="call_1", name="boom", arguments_raw="{}", args={})
        responses = [
            AIMessage(content="", tool_calls=[call]),
            AIMessage(content="恢复"),
        ]
        loop, llm, sessions = _make_loop(
            tmp_path, responses, registry=registry, memory=memory
        )
        result = loop.run("再炸一次", "s1")
        assert result.answer == "恢复"
        assert result.truncated is False
        assert memory.append.call_count == 0
        roles = [m.get("role") for m in _messages(sessions, "s1")]
        assert roles == ["user", "assistant", "tool", "assistant"]

    def testToolTimeoutStructuredReturn(self, tmp_path: Path) -> None:
        """工具超时：结构化 ToolTimeoutError 回传，主循环不中断。"""
        config = RuntimeConfig(tool_timeout_seconds=0.01)
        registry = ToolRegistry()
        registry.register(_SlowTool())
        call = ToolCall(id="call_1", name="slow", arguments_raw="{}", args={})
        responses = [
            AIMessage(content="", tool_calls=[call]),
            AIMessage(content="超时也能继续"),
        ]
        loop, llm, _ = _make_loop(tmp_path, responses, registry=registry, config=config)
        result = loop.run("调慢工具", "s1")
        assert result.answer == "超时也能继续"
        tool_messages = [m for m in llm.calls[1] if m.get("role") == "tool"]
        payload = json.loads(tool_messages[0]["content"])
        assert payload["error"]["type"] == "ToolTimeoutError"

    def testInvalidArgumentsNotExecuted(self, tmp_path: Path) -> None:
        """非法 JSON 参数：工具不执行，LLM 收到 InvalidToolArguments 错误。"""
        registry = ToolRegistry()
        counting = _CountingTool()
        registry.register(counting)
        call = ToolCall(id="call_1", name="counter", arguments_raw="bad-json", args=None)
        responses = [
            AIMessage(content="", tool_calls=[call]),
            AIMessage(content="参数坏了"),
        ]
        loop, llm, _ = _make_loop(tmp_path, responses, registry=registry)
        result = loop.run("坏参数", "s1")
        assert result.answer == "参数坏了"
        assert counting.execute_count == 0
        tool_messages = [m for m in llm.calls[1] if m.get("role") == "tool"]
        payload = json.loads(tool_messages[0]["content"])
        assert payload["error"]["type"] == "InvalidToolArguments"
        assert "JSON" in payload["error"]["message"]

    def testMiddlewareHooksInvoked(self, tmp_path: Path) -> None:
        """钩子调用次数：无工具轮 before/after 各 1；带工具 2 轮 before 2、wrap 1。"""
        recorder = RecordingMiddleware()
        loop, _, _ = _make_loop(
            tmp_path, [AIMessage(content="答")], middlewares=[recorder]
        )
        loop.run("问", "s1")
        assert recorder.before_model_count == 1
        assert recorder.after_model_count == 1
        assert recorder.wrap_tool_call_count == 0

        registry = ToolRegistry()
        registry.register(CalculatorTool())
        call = ToolCall(
            id="call_1",
            name="calculator",
            arguments_raw='{"expression": "1+1"}',
            args={"expression": "1+1"},
        )
        recorder2 = RecordingMiddleware()
        loop2, _, _ = _make_loop(
            tmp_path,
            [AIMessage(content="", tool_calls=[call]), AIMessage(content="完成")],
            registry=registry,
            middlewares=[recorder2],
        )
        loop2.run("算", "s1")
        assert recorder2.before_model_count == 2
        assert recorder2.wrap_tool_call_count == 1
        assert recorder2.after_model_count == 1

    def testTraceSpansEmitted(self, tmp_path: Path) -> None:
        """两轮循环：llm span 恰 2 次、tool span 恰 1 次且父为第 1 个 llm span。"""
        trace = _mock_trace()
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        call = ToolCall(
            id="call_1",
            name="calculator",
            arguments_raw='{"expression": "1+1"}',
            args={"expression": "1+1"},
        )
        loop, _, _ = _make_loop(
            tmp_path,
            [AIMessage(content="", tool_calls=[call]), AIMessage(content="好")],
            registry=registry,
            trace=trace,
        )
        loop.run("算", "s1")
        assert trace.start_trace.call_count == 1
        assert trace.start_llm_span.call_count == 2
        assert trace.start_tool_span.call_count == 1
        tool_span_args = trace.start_tool_span.call_args.args
        assert tool_span_args[0] == "tid-1"
        assert tool_span_args[1] == "span-a"

    def testTraceIdInjectedIntoLoopState(self, tmp_path: Path) -> None:
        """trace_id 注入 LoopState：首个 before_model 收到 start_trace 返回值。"""
        recorder = RecordingMiddleware()
        loop, _, _ = _make_loop(
            tmp_path, [AIMessage(content="答")], middlewares=[recorder]
        )
        loop.run("问", "s1")
        assert recorder.states[0].trace_id == "tid-1"
        assert recorder.states[0].session_id == "s1"

    def testReasoningContentPersisted(self, tmp_path: Path) -> None:
        """思考内容随 assistant 消息持久化（下轮请求回传前提）。"""
        loop, _, sessions = _make_loop(
            tmp_path, [AIMessage(content="答案", reasoning_content="思考")]
        )
        loop.run("问", "s1")
        assistant = next(
            m for m in _messages(sessions, "s1") if m.get("role") == "assistant"
        )
        assert assistant["reasoning_content"] == "思考"
        assert assistant["content"] == "答案"

    def testAssistantToolCallsStoredWithPair(self, tmp_path: Path) -> None:
        """assistant 保留 tool_calls 结构且紧跟配对 tool 消息（id 一致）。"""
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        call = ToolCall(
            id="call_1",
            name="calculator",
            arguments_raw='{"expression": "2+3*4"}',
            args={"expression": "2+3*4"},
        )
        loop, _, sessions = _make_loop(
            tmp_path,
            [AIMessage(content="", tool_calls=[call]), AIMessage(content="14")],
            registry=registry,
        )
        loop.run("算", "s1")
        messages = _messages(sessions, "s1")
        assistant_index = next(
            i
            for i, m in enumerate(messages)
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        assistant = messages[assistant_index]
        assert assistant["tool_calls"][0]["id"] == "call_1"
        assert assistant["tool_calls"][0]["type"] == "function"
        assert assistant["tool_calls"][0]["function"]["name"] == "calculator"
        assert (
            assistant["tool_calls"][0]["function"]["arguments"]
            == '{"expression": "2+3*4"}'
        )
        paired = messages[assistant_index + 1]
        assert paired["role"] == "tool"
        assert paired["tool_call_id"] == "call_1"
        assert paired["content"] == "14"

    def testUserInputAppearsOncePerRound(self, tmp_path: Path) -> None:
        """当前输入每轮恰出现一次：首轮由 build 追加，后续轮已含于历史。"""
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        call = ToolCall(
            id="call_1",
            name="calculator",
            arguments_raw='{"expression": "2+3*4"}',
            args={"expression": "2+3*4"},
        )
        loop, llm, sessions = _make_loop(
            tmp_path,
            [AIMessage(content="", tool_calls=[call]), AIMessage(content="14")],
            registry=registry,
        )
        loop.run("计算 2+3*4", "s1")
        round1 = llm.calls[0]
        user_messages_1 = [m for m in round1 if m.get("role") == "user"]
        assert len(user_messages_1) == 1
        assert user_messages_1[0]["content"] == "计算 2+3*4"
        round2 = llm.calls[1]
        user_messages_2 = [m for m in round2 if m.get("role") == "user"]
        assert len(user_messages_2) == 1
        assert user_messages_2[0]["content"] == "计算 2+3*4"
        assert round2[-1]["role"] == "tool"
        # 会话落库的 user 消息也恰一条
        stored_user = [m for m in _messages(sessions, "s1") if m.get("role") == "user"]
        assert len(stored_user) == 1

    def testToolNotFoundStructuredReturn(self, tmp_path: Path) -> None:
        """调用未注册工具：ToolNotFoundError 结构化回传，无 span、无执行。"""
        trace = _mock_trace()
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        call = ToolCall(
            id="call_x",
            name="不存在的工具",
            arguments_raw="{}",
            args={},
        )
        responses = [
            AIMessage(content="", tool_calls=[call]),
            AIMessage(content="没有这个工具"),
        ]
        loop, llm, _ = _make_loop(
            tmp_path, responses, registry=registry, trace=trace
        )
        result = loop.run("调用不存在的工具", "s1")
        assert result.answer == "没有这个工具"
        assert trace.start_tool_span.call_count == 0
        tool_messages = [m for m in llm.calls[1] if m.get("role") == "tool"]
        payload = json.loads(tool_messages[0]["content"])
        assert payload["error"]["type"] == "ToolNotFoundError"
        assert "不存在的工具" in payload["error"]["message"]

    def testStreamingPathForwardsEvents(self, tmp_path: Path) -> None:
        """on_event 非空时走 stream 路径：事件逐个转发并聚合为 AIMessage。"""
        received: list[Any] = []
        loop, llm, sessions = _make_loop(
            tmp_path,
            [AIMessage(content="流式回答")],
            fake_stream=True,
        )
        result = loop.run("你好", "s1", on_event=received.append)
        assert result.answer == "流式回答"
        assert result.rounds == 1
        assert len(llm.calls) == 0
        assert len(llm.stream_calls) == 1
        assert len(received) == 1
        assert received[0].text == "流式回答"
