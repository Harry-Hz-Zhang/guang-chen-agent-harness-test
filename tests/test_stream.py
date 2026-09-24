"""LLMClient.stream 流式管道与 collect_stream 事件聚合的单元测试。

openai.OpenAI 全程 monkeypatch 拦截，流式 chunk 用 SimpleNamespace
按 SDK 形状手工构造（勿用 MagicMock 自动属性，避免假阳性），零真实
网络调用。
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import openai
import pytest

from harness.config import RuntimeConfig
from harness.llm import (
    AIMessage,
    DoneEvent,
    LLMClient,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    Usage,
    UsageEvent,
    collect_stream,
)

_FAKE_API_KEY = "sk-test-fake-key"


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """统一注入假 DEEPSEEK_API_KEY，隔离真实环境缺失导致的连带失败。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", _FAKE_API_KEY)


def _usage(
    prompt_tokens: int = 312,
    completion_tokens: int = 47,
    total_tokens: int = 359,
) -> SimpleNamespace:
    """构造 openai SDK 形状的 usage mock（不带 reasoning_tokens，验证默认 0）。"""
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _chunk(
    reasoning: str | None = None,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
    usage: SimpleNamespace | None = None,
    choices: bool = True,
) -> SimpleNamespace:
    """构造 openai SDK 形状的流式 chunk mock。

    choices=True 时生成带 delta 的 choice（finish_reason 可同时挂在
    choice 上）；choices=False 表示 include_usage 的独立纯 usage 末片
    （choices 为空列表）。
    """
    delta = SimpleNamespace(
        reasoning_content=reasoning,
        content=content,
        tool_calls=tool_calls,
    )
    if choices:
        choice_list = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    else:
        choice_list = []
    return SimpleNamespace(choices=choice_list, usage=usage)


def _tool_call_piece(
    index: int = 0,
    id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> SimpleNamespace:
    """构造 SDK 形状的 delta.tool_calls 单项 mock（首片带 id/name，后续片只带 arguments）。"""
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _invoke_response() -> SimpleNamespace:
    """构造 invoke 所需的最小非流式响应 mock（供共享 client 断言复用）。"""
    message = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=None,
    )


def _install_mock_openai_stream(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[Any],
) -> tuple[MagicMock, MagicMock]:
    """把 openai.OpenAI 替换为全 mock，create 返回给定 chunk 序列的迭代器。

    返回（mock create，mock client）；client 供需要复用同一实例的
    用例重设 create.return_value。
    """
    create = MagicMock(return_value=iter(chunks))
    mock_client = MagicMock()
    mock_client.chat.completions.create = create

    def _fake_openai(*args: Any, **kwargs: Any) -> MagicMock:
        return mock_client

    monkeypatch.setattr(openai, "OpenAI", _fake_openai)
    return create, mock_client


class TestLLMClientStream:
    """覆盖 LLMClient.stream 的 chunk 翻译与 collect_stream 事件聚合。"""

    def testReasoningThenTextDeltas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """思考增量先于正文增量产出，4 个 chunk 翻译为 4 个事件且顺序保持。"""
        chunks = [
            _chunk(reasoning="思"),
            _chunk(reasoning="考"),
            _chunk(content="你"),
            _chunk(content="好"),
        ]
        _install_mock_openai_stream(monkeypatch, chunks)
        client = LLMClient(RuntimeConfig())
        events = list(client.stream([{"role": "user", "content": "hi"}]))
        assert events == [
            ReasoningDelta("思"),
            ReasoningDelta("考"),
            TextDelta("你"),
            TextDelta("好"),
        ]

    def testToolCallDeltaAggregation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """工具调用分片逐片产出 ToolCallDelta，聚合后 id/name/args 完整还原。"""
        chunks = [
            _chunk(
                tool_calls=[
                    _tool_call_piece(
                        index=0, id="call_1", name="calc", arguments='{"ex'
                    )
                ]
            ),
            _chunk(
                tool_calls=[
                    _tool_call_piece(
                        index=0, id=None, name=None, arguments='pression":"1+1"}'
                    )
                ]
            ),
        ]
        _install_mock_openai_stream(monkeypatch, chunks)
        client = LLMClient(RuntimeConfig())
        events = list(client.stream([{"role": "user", "content": "算一下"}]))
        deltas = [e for e in events if isinstance(e, ToolCallDelta)]
        assert deltas == [
            ToolCallDelta(index=0, id="call_1", name="calc", args_delta='{"ex'),
            ToolCallDelta(index=0, id=None, name=None, args_delta='pression":"1+1"}'),
        ]
        message = collect_stream(events)
        assert len(message.tool_calls) == 1
        call = message.tool_calls[0]
        assert isinstance(call, ToolCall)
        assert call.id == "call_1"
        assert call.name == "calc"
        assert call.arguments_raw == '{"expression":"1+1"}'
        assert call.args == {"expression": "1+1"}

    def testMultiToolAggregation(self) -> None:
        """多工具分片交错聚合：index 1 首片先到时仍按 index 升序输出，各自分片无损拼接。

        分片到达顺序刻意使 dict 插入序（[1, 0]）≠ index 升序
        （[0, 1]）：若聚合改为按插入序输出，本用例在顺序断言处
        失败（排序敏感证明）。
        """
        events = [
            ToolCallDelta(
                index=1, id="call_2", name="weather", args_delta='{"ci'
            ),
            ToolCallDelta(
                index=0, id="call_1", name="calc", args_delta='{"ex'
            ),
            ToolCallDelta(
                index=1, id=None, name=None, args_delta='ty":"北京"}'
            ),
            ToolCallDelta(
                index=0, id=None, name=None, args_delta='pression":"2+3*4"}'
            ),
            UsageEvent(
                usage=Usage(
                    prompt_tokens=100, completion_tokens=20, total_tokens=120
                )
            ),
            DoneEvent(finish_reason="tool_calls"),
        ]
        message = collect_stream(events)
        assert [c.id for c in message.tool_calls] == ["call_1", "call_2"]
        assert [c.name for c in message.tool_calls] == ["calc", "weather"]
        first, second = message.tool_calls
        assert first.arguments_raw == '{"expression":"2+3*4"}'
        assert first.args == {"expression": "2+3*4"}
        assert second.arguments_raw == '{"city":"北京"}'
        assert second.args == {"city": "北京"}
        assert message.usage is not None
        assert message.finish_reason == "tool_calls"

    def testUsageAndDoneEvents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """末 chunk 同时带 usage 与 finish_reason 时，UsageEvent 先于 DoneEvent 产出。"""
        chunks = [
            _chunk(content="中"),
            _chunk(usage=_usage(), finish_reason="tool_calls"),
        ]
        _install_mock_openai_stream(monkeypatch, chunks)
        client = LLMClient(RuntimeConfig())
        events = list(client.stream([{"role": "user", "content": "hi"}]))
        assert events == [
            TextDelta("中"),
            UsageEvent(usage=Usage(prompt_tokens=312, completion_tokens=47, total_tokens=359)),
            DoneEvent(finish_reason="tool_calls"),
        ]

        _install_mock_openai_stream(
            monkeypatch,
            [
                _chunk(content="答"),
                _chunk(
                    usage=_usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                    choices=False,
                ),
            ],
        )
        client2 = LLMClient(RuntimeConfig())
        events2 = list(client2.stream([{"role": "user", "content": "hi"}]))
        assert events2 == [
            TextDelta("答"),
            UsageEvent(usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
        ]

    def testStreamOptionsSent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """create 收到 stream=True 与 stream_options={"include_usage": True}；thinking 默认开不传 extra_body。"""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "calc",
                    "description": "计算器",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        messages = [{"role": "user", "content": "hi"}]
        create, _ = _install_mock_openai_stream(monkeypatch, [_chunk()])
        client = LLMClient(RuntimeConfig())
        list(client.stream(messages))
        kwargs = create.call_args.kwargs
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        assert kwargs["model"] == "deepseek-flash"
        assert kwargs["messages"] == messages
        assert "tools" not in kwargs
        assert "extra_body" not in kwargs

        create.return_value = _invoke_response()
        client.invoke(messages)
        create.return_value = iter([_chunk(content="好")])
        list(client.stream(messages, tools))
        assert create.call_args.kwargs["tools"] == tools

    def testStreamSharesClientWithInvoke(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stream 与 invoke 共享同一底层 client：连续调用 openai.OpenAI 构造恰 1 次。"""
        create = MagicMock(return_value=iter([_chunk(content="好")]))
        mock_client = MagicMock()
        mock_client.chat.completions.create = create
        constructor_calls: list[dict[str, Any]] = []

        def _fake_openai(*args: Any, **kwargs: Any) -> MagicMock:
            constructor_calls.append(kwargs)
            return mock_client

        monkeypatch.setattr(openai, "OpenAI", _fake_openai)
        client = LLMClient(RuntimeConfig())
        list(client.stream([{"role": "user", "content": "hi"}]))
        create.return_value = _invoke_response()
        client.invoke([{"role": "user", "content": "hi"}])
        create.return_value = iter([_chunk(content="好")])
        list(client.stream([{"role": "user", "content": "hi"}]))
        assert len(constructor_calls) == 1

    def testCollectFullAIMessage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """完整 chunk 序列聚合为 AIMessage：思考/正文拼接、工具参数解析、usage 与 finish_reason 填充。"""
        chunks = [
            _chunk(reasoning="思"),
            _chunk(reasoning="考"),
            _chunk(content="你"),
            _chunk(content="好"),
            _chunk(
                tool_calls=[
                    _tool_call_piece(
                        index=0, id="call_1", name="calc", arguments='{"ex'
                    )
                ]
            ),
            _chunk(
                tool_calls=[
                    _tool_call_piece(
                        index=0, id=None, name=None, arguments='pression":"1+1"}'
                    )
                ]
            ),
            _chunk(usage=_usage(), finish_reason="tool_calls"),
        ]
        _install_mock_openai_stream(monkeypatch, chunks)
        client = LLMClient(RuntimeConfig())
        message = collect_stream(
            client.stream([{"role": "user", "content": "算一下"}])
        )
        assert isinstance(message, AIMessage)
        assert message.role == "assistant"
        assert message.content == "你好"
        assert message.reasoning_content == "思考"
        assert len(message.tool_calls) == 1
        call = message.tool_calls[0]
        assert call.id == "call_1"
        assert call.name == "calc"
        assert call.arguments_raw == '{"expression":"1+1"}'
        assert call.args == {"expression": "1+1"}
        assert message.usage is not None
        assert message.usage.prompt_tokens == 312
        assert message.usage.completion_tokens == 47
        assert message.usage.total_tokens == 359
        assert message.usage.reasoning_tokens == 0
        assert message.finish_reason == "tool_calls"

    def testEmptyStream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """create 返回空迭代时流式路径 0 异常：事件为空列表，聚合返回空 AIMessage。"""
        _install_mock_openai_stream(monkeypatch, [])
        client = LLMClient(RuntimeConfig())
        events = list(client.stream([{"role": "user", "content": "hi"}]))
        assert events == []
        message = collect_stream(events)
        assert isinstance(message, AIMessage)
        assert message.content == ""
        assert message.reasoning_content is None
        assert message.tool_calls == []
        assert message.usage is None
        assert message.finish_reason == ""
