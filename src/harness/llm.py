"""LLMClient 与 AIMessage —— DeepSeek（OpenAI 兼容协议）模型客户端的非流式封装。

reasoning_content 字段在 openai SDK 类型层不存在、仅运行时透传
（pydantic extra="allow"），对其的 getattr 访问统一封装在本模块
单点，其他模块一律读取 AIMessage.reasoning_content。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

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
        request_kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if tools is not None:
            request_kwargs["tools"] = tools
        if not self.config.thinking_enabled:
            request_kwargs["extra_body"] = _THINKING_DISABLED_EXTRA_BODY
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
