"""LLMClient 单元测试：非流式 invoke 的消息聚合、参数透传与错误封装。

openai.OpenAI 全程 monkeypatch 拦截（SimpleNamespace 构造精确属性
的 mock 响应），零真实网络调用。
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import openai
import pytest

from harness.config import RuntimeConfig
from harness.llm import AIMessage, LLMClient, LLMError, ToolCall

_FAKE_API_KEY = "sk-test-fake-key"


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """统一注入假 DEEPSEEK_API_KEY，隔离真实环境缺失导致的连带失败。

    需要验证缺失路径的用例（testApiKeyMissingRaises）在用例内先
    monkeypatch.delenv 删除，同一 monkeypatch 实例保证顺序正确。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", _FAKE_API_KEY)


def _make_usage(
    prompt_tokens: int = 312,
    completion_tokens: int = 47,
    total_tokens: int = 359,
) -> SimpleNamespace:
    """构造 openai SDK 形状的 usage mock（刻意不带 reasoning_tokens，验证默认 0）。"""
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _make_response(
    content: str = "你好",
    reasoning_content: str | None = None,
    tool_calls: list[Any] | None = None,
    usage: SimpleNamespace | None = None,
    finish_reason: str = "stop",
) -> SimpleNamespace:
    """构造 openai SDK 形状的 chat completion 响应 mock。

    reasoning_content 仅在显式给出时挂到 message 上：SDK 类型层无
    此字段、仅运行时透传，默认响应不带该属性以验证 getattr 回退
    None 的路径。
    """
    message_kwargs: dict[str, Any] = {"content": content, "tool_calls": tool_calls}
    if reasoning_content is not None:
        message_kwargs["reasoning_content"] = reasoning_content
    message = SimpleNamespace(**message_kwargs)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


def _install_mock_openai(
    monkeypatch: pytest.MonkeyPatch,
    response: Any = None,
    create_side_effect: BaseException | None = None,
) -> tuple[MagicMock, list[dict[str, Any]]]:
    """把 openai.OpenAI 替换为全 mock，返回（mock create，构造参数记录列表）。"""
    if create_side_effect is not None:
        create = MagicMock(side_effect=create_side_effect)
    else:
        create = MagicMock(return_value=response)
    mock_client = MagicMock()
    mock_client.chat.completions.create = create
    constructor_kwargs: list[dict[str, Any]] = []

    def _fake_openai(*args: Any, **kwargs: Any) -> MagicMock:
        constructor_kwargs.append(kwargs)
        return mock_client

    monkeypatch.setattr(openai, "OpenAI", _fake_openai)
    return create, constructor_kwargs


class TestLLMClient:
    """覆盖 LLMClient 构造校验与 invoke 非流式调用行为。"""

    def testInvokeReturnsAIMessage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """invoke 聚合响应为 AIMessage，content/usage/finish_reason 逐字段一致且入参精确透传。"""
        response = _make_response(
            content="你好", usage=_make_usage(), finish_reason="stop"
        )
        create, _ = _install_mock_openai(monkeypatch, response=response)
        client = LLMClient(RuntimeConfig())
        messages = [{"role": "user", "content": "你好"}]
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
        result = client.invoke(messages, tools)
        assert isinstance(result, AIMessage)
        assert result.role == "assistant"
        assert result.content == "你好"
        assert result.reasoning_content is None
        assert result.tool_calls == []
        assert result.finish_reason == "stop"
        assert result.usage is not None
        assert result.usage.prompt_tokens == 312
        assert result.usage.completion_tokens == 47
        assert result.usage.total_tokens == 359
        assert result.usage.reasoning_tokens == 0
        assert create.call_args.kwargs["model"] == "deepseek-flash"
        assert create.call_args.kwargs["messages"] == messages
        assert create.call_args.kwargs["tools"] == tools

    def testReasoningContentPassthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """message 上的 reasoning_content 额外属性经 getattr 透传进 AIMessage。"""
        response = _make_response(reasoning_content="思考…")
        create, _ = _install_mock_openai(monkeypatch, response=response)
        client = LLMClient(RuntimeConfig())
        result = client.invoke([{"role": "user", "content": "hi"}])
        assert result.reasoning_content == "思考…"
        assert "tools" not in create.call_args.kwargs

    def testToolCallsParsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tool_calls 逐个解析：id/name/arguments_raw 提取且 arguments 解析为 dict。"""
        raw_call = SimpleNamespace(
            id="call_1",
            type="function",
            function=SimpleNamespace(name="calc", arguments='{"expression": "1+1"}'),
        )
        response = _make_response(tool_calls=[raw_call], finish_reason="tool_calls")
        _install_mock_openai(monkeypatch, response=response)
        client = LLMClient(RuntimeConfig())
        result = client.invoke([{"role": "user", "content": "算一下"}])
        assert len(result.tool_calls) == 1
        call = result.tool_calls[0]
        assert isinstance(call, ToolCall)
        assert call.id == "call_1"
        assert call.name == "calc"
        assert call.arguments_raw == '{"expression": "1+1"}'
        assert call.args == {"expression": "1+1"}
        assert result.finish_reason == "tool_calls"

    def testInvalidArgumentsKeptRaw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """arguments 为非法 JSON 时不抛异常：args 置 None、原文保留在 arguments_raw。"""
        raw_call = SimpleNamespace(
            id="call_2",
            type="function",
            function=SimpleNamespace(name="calc", arguments="not-json"),
        )
        response = _make_response(tool_calls=[raw_call])
        _install_mock_openai(monkeypatch, response=response)
        client = LLMClient(RuntimeConfig())
        result = client.invoke([{"role": "user", "content": "算一下"}])
        call = result.tool_calls[0]
        assert call.args is None
        assert call.arguments_raw == "not-json"

    def testThinkingDisabledExtraBody(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """thinking_enabled=False 时 create 收到 extra_body 关闭思考；默认开时不传该参数。"""
        create, _ = _install_mock_openai(monkeypatch, response=_make_response())
        client = LLMClient(RuntimeConfig(thinking_enabled=False))
        client.invoke([{"role": "user", "content": "hi"}])
        assert create.call_args.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}

        create_default, _ = _install_mock_openai(monkeypatch, response=_make_response())
        client_default = LLMClient(RuntimeConfig())
        client_default.invoke([{"role": "user", "content": "hi"}])
        assert "extra_body" not in create_default.call_args.kwargs

    def testTimeoutAndRetriesPassed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """构造参数精确透传：timeout=60.0、max_retries=2、api_key 与 base_url。"""
        _, constructor_kwargs = _install_mock_openai(
            monkeypatch, response=_make_response()
        )
        LLMClient(RuntimeConfig())
        assert len(constructor_kwargs) == 1
        kwargs = constructor_kwargs[0]
        assert kwargs["timeout"] == 60.0
        assert kwargs["max_retries"] == 2
        assert kwargs["api_key"] == _FAKE_API_KEY
        assert kwargs["base_url"] == "https://api.deepseek.com"

    def testApiKeyMissingRaises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEEPSEEK_API_KEY 缺失时构造抛 LLMError，消息含变量名与设置指引。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(LLMError) as exc_info:
            LLMClient(RuntimeConfig())
        message = str(exc_info.value)
        assert "DEEPSEEK_API_KEY" in message
        assert "环境变量" in message

    def testInvokeApiErrorRaisesLLMError(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """create 抛任意异常时 invoke 统一抛 LLMError 且消息含原始异常文本，不裸抛。"""
        _install_mock_openai(monkeypatch, create_side_effect=RuntimeError("boom"))
        client = LLMClient(RuntimeConfig())
        with pytest.raises(LLMError) as exc_info:
            client.invoke([{"role": "user", "content": "hi"}])
        assert "boom" in str(exc_info.value)

    def testInvokeMalformedResponseRaisesLLMError(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """响应形状异常（choices 空 / message 为 None）时统一抛 LLMError，不裸抛 IndexError/AttributeError。"""
        empty_choices = SimpleNamespace(choices=[], usage=None)
        _install_mock_openai(monkeypatch, response=empty_choices)
        client = LLMClient(RuntimeConfig())
        with pytest.raises(LLMError):
            client.invoke([{"role": "user", "content": "hi"}])

        none_message = SimpleNamespace(
            choices=[SimpleNamespace(message=None, finish_reason="stop")],
            usage=None,
        )
        _install_mock_openai(monkeypatch, response=none_message)
        client2 = LLMClient(RuntimeConfig())
        with pytest.raises(LLMError):
            client2.invoke([{"role": "user", "content": "hi"}])
