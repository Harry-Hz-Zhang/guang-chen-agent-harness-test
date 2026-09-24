"""LLMClient 与 AIMessage —— DeepSeek（OpenAI 兼容协议）模型客户端的非流式封装。

reasoning_content 字段在 openai SDK 类型层不存在、仅运行时透传
（pydantic extra="allow"），对其的 getattr 访问统一封装在本模块
单点，其他模块一律读取 AIMessage.reasoning_content。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import openai

from harness.config import RuntimeConfig

_THINKING_DISABLED_EXTRA_BODY: dict[str, Any] = {"thinking": {"type": "disabled"}}


@dataclass
class ToolCall:
    """一次工具调用的结构化载体。

    id 为调用标识、name 为工具名；arguments_raw 为模型返回的原始
    参数字符串；args 为 json.loads 成功后的解析结果（dict），解析
    失败时为 None（原文保留在 arguments_raw，校验交给 parser）。
    """

    id: str
    name: str
    arguments_raw: str
    args: dict[str, Any] | None


@dataclass
class Usage:
    """一次 LLM 调用的 token 用量统计（reasoning_tokens 缺省 0）。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    reasoning_tokens: int = 0


@dataclass
class AIMessage:
    """LLM 单次返回的完整消息（非流式聚合结果）。"""

    role: str = "assistant"
    content: str = ""
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str = ""


class LLMError(Exception):
    """LLM 调用失败的统一封装（网络/超时/鉴权/缺 key），含可读中文消息。"""


@dataclass
class ReasoningDelta:
    """流式思考内容增量（对应 delta.reasoning_content 的单个分片）。"""

    text: str


@dataclass
class TextDelta:
    """流式正文增量（对应 delta.content 的单个分片）。"""

    text: str


@dataclass
class ToolCallDelta:
    """流式工具调用增量（对应 delta.tool_calls 列表的单个分片）。

    index 为同一流内工具调用的序号；首片通常携带 id 与 name，后续
    片只携带 arguments 增量（args_delta），分片拼接与统一解析交给
    collect_stream 聚合器完成。
    """

    index: int
    id: str | None
    name: str | None
    args_delta: str


@dataclass
class UsageEvent:
    """流式 token 用量事件（include_usage 的末 chunk 携带）。"""

    usage: Usage


@dataclass
class DoneEvent:
    """流式结束事件（携带 finish_reason，如 "stop" / "tool_calls"）。"""

    finish_reason: str


StreamEvent = ReasoningDelta | TextDelta | ToolCallDelta | UsageEvent | DoneEvent


class LLMClient:
    """DeepSeek（OpenAI 兼容协议）模型客户端。

    构造时从环境变量读取 DEEPSEEK_API_KEY，并按 RuntimeConfig 建立
    底层 openai 客户端（timeout / max_retries 精确透传）；invoke 为
    非流式调用，把响应聚合为 AIMessage。
    """

    def __init__(self, config: RuntimeConfig) -> None:
        """初始化底层客户端；缺 API key 时抛 LLMError（含设置指引）。"""
        self.config = config
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise LLMError(
                "环境变量 DEEPSEEK_API_KEY 未设置：请先将其设置为你的 "
                "DeepSeek API Key（PowerShell 临时生效："
                '$env:DEEPSEEK_API_KEY = "sk-xxx"）后再启动。'
            )
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
        )

    def _request_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """构造 invoke 与 stream 共用的基础请求参数。

        含 model/messages 直传、tools 条件传入（None 不传该键）与
        thinking 关闭时的 extra_body；流式专用键（stream /
        stream_options）由 stream 调用方追加。
        """
        request_kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if tools is not None:
            request_kwargs["tools"] = tools
        if not self.config.thinking_enabled:
            request_kwargs["extra_body"] = _THINKING_DISABLED_EXTRA_BODY
        return request_kwargs

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AIMessage:
        """非流式调用 chat.completions.create 并聚合响应为 AIMessage。

        tools 为 None 时不传该参数；thinking_enabled=False 时追加
        extra_body 关闭思考模式；底层任何异常统一包装为 LLMError
        （消息含原始异常文本），不裸抛。
        """
        request_kwargs = self._request_kwargs(messages, tools)
        try:
            response = self._client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            raise LLMError(f"LLM 调用失败：{exc}") from exc
        return self._to_ai_message(response)

    def _to_ai_message(self, response: Any) -> AIMessage:
        """把 openai SDK 响应对象聚合为 AIMessage。

        对响应形状做显式校验：choices 为空或 choice.message 为
        None 时抛 LLMError（不裸抛 IndexError/AttributeError）。
        reasoning_content 的 getattr 访问统一收敛在本方法（SDK 类型
        层无该字段、运行时透传）；非法 JSON 的工具参数不抛异常，
        args 置 None 并保留原文，由 parser 层校验回传。
        """
        choices = response.choices
        if not choices:
            raise LLMError("LLM 响应缺少 choices，无法提取消息内容。")
        choice = choices[0]
        message = choice.message
        if message is None:
            raise LLMError("LLM 响应的 choice.message 为空，无法提取消息内容。")
        tool_calls: list[ToolCall] = []
        for raw_call in getattr(message, "tool_calls", None) or []:
            function = raw_call.function
            arguments_raw = function.arguments
            try:
                parsed: Any = json.loads(arguments_raw)
            except json.JSONDecodeError:
                parsed = None
            tool_calls.append(
                ToolCall(
                    id=raw_call.id,
                    name=function.name,
                    arguments_raw=arguments_raw,
                    args=parsed if isinstance(parsed, dict) else None,
                )
            )
        usage: Usage | None = None
        if response.usage is not None:
            usage = Usage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                reasoning_tokens=getattr(response.usage, "reasoning_tokens", 0),
            )
        content = message.content
        return AIMessage(
            content=content if content is not None else "",
            reasoning_content=getattr(message, "reasoning_content", None),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.finish_reason or "",
        )

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[StreamEvent]:
        """流式调用 chat.completions.create，把每个 chunk 翻译为 StreamEvent。

        请求参数与 invoke 完全同构（_request_kwargs 共用：model/
        messages/tools 透传、thinking 关闭时追加 extra_body），另加
        stream=True 与 stream_options={"include_usage": True}；与
        invoke 共享同一 self._client（不重复建连）。create 调用及迭代
        期间的任何异常统一包装为 LLMError，不裸抛。单个 chunk 内事件
        顺序：delta 事件 → UsageEvent → DoneEvent（末 chunk 可同时带
        usage 与 finish_reason；choices 为空的纯 usage 末片只产出
        UsageEvent）。
        """
        request_kwargs = self._request_kwargs(messages, tools)
        request_kwargs["stream"] = True
        request_kwargs["stream_options"] = {"include_usage": True}
        try:
            chunks = self._client.chat.completions.create(**request_kwargs)
            for chunk in chunks:
                yield from self._translate_chunk(chunk)
        except Exception as exc:
            raise LLMError(f"LLM 流式调用失败：{exc}") from exc

    def _translate_chunk(self, chunk: Any) -> list[StreamEvent]:
        """把单个流式 chunk 翻译为事件列表（chunk 内顺序：delta 事件 → UsageEvent → DoneEvent）。"""
        events: list[StreamEvent] = []
        choices = getattr(chunk, "choices", None) or []
        finish_reason: str | None = None
        if choices:
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                events.extend(self._translate_delta(delta))
            finish_reason = getattr(choice, "finish_reason", None)
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            events.append(
                UsageEvent(
                    usage=Usage(
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                        reasoning_tokens=getattr(usage, "reasoning_tokens", 0),
                    )
                )
            )
        if finish_reason:
            events.append(DoneEvent(finish_reason=finish_reason))
        return events

    def _translate_delta(self, delta: Any) -> list[StreamEvent]:
        """把 chunk.choices[0].delta 翻译为思考/正文/工具调用增量事件。

        reasoning_content 的 getattr 访问与 invoke 的 _to_ai_message
        同源（SDK 类型层无该字段、仅运行时透传）；tool_calls 逐项
        翻译为 ToolCallDelta（arguments 允许为 None，统一转空串）。
        """
        events: list[StreamEvent] = []
        reasoning = getattr(delta, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning:
            events.append(ReasoningDelta(text=reasoning))
        content = getattr(delta, "content", None)
        if content:
            events.append(TextDelta(text=content))
        for raw_call in getattr(delta, "tool_calls", None) or []:
            function = raw_call.function
            events.append(
                ToolCallDelta(
                    index=raw_call.index,
                    id=raw_call.id,
                    name=getattr(function, "name", None),
                    args_delta=getattr(function, "arguments", None) or "",
                )
            )
        return events


def collect_stream(events: Iterable[StreamEvent]) -> AIMessage:
    """把 StreamEvent 序列聚合为完整 AIMessage（流式与非流式共用消息结构）。

    思考/正文增量分别拼接（思考为空则 None）；工具调用按 index 聚合，
    首片的 id/name 生效、arguments 分片无损拼接后统一 json.loads
    （成功且为 dict 才填充 args，失败/空则 args=None 且
    arguments_raw 保留拼接原文，校验交给 parser）；UsageEvent 取
    最后一次为 usage；DoneEvent 记录 finish_reason。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_ids: dict[int, str] = {}
    tool_names: dict[int, str] = {}
    argument_parts: dict[int, list[str]] = {}
    usage: Usage | None = None
    finish_reason: str = ""
    for event in events:
        if isinstance(event, ReasoningDelta):
            reasoning_parts.append(event.text)
        elif isinstance(event, TextDelta):
            content_parts.append(event.text)
        elif isinstance(event, ToolCallDelta):
            if event.id is not None:
                tool_ids.setdefault(event.index, event.id)
            if event.name is not None:
                tool_names.setdefault(event.index, event.name)
            argument_parts.setdefault(event.index, []).append(event.args_delta)
        elif isinstance(event, UsageEvent):
            usage = event.usage
        elif isinstance(event, DoneEvent):
            finish_reason = event.finish_reason
    tool_calls: list[ToolCall] = []
    for index in sorted(argument_parts):
        arguments_raw = "".join(argument_parts[index])
        parsed: Any = None
        if arguments_raw:
            try:
                parsed = json.loads(arguments_raw)
            except json.JSONDecodeError:
                parsed = None
        tool_calls.append(
            ToolCall(
                id=tool_ids.get(index, ""),
                name=tool_names.get(index, ""),
                arguments_raw=arguments_raw,
                args=parsed if isinstance(parsed, dict) else None,
            )
        )
    return AIMessage(
        content="".join(content_parts),
        reasoning_content="".join(reasoning_parts) or None,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
    )
