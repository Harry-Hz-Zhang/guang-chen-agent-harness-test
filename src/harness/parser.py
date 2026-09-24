"""parser —— LLM 输出解析：决策二分与工具参数显式校验。

parse_response 把 AIMessage 恒定二分为 FinalAnswer（最终答案）或
ToolCallBatch（工具调用批次）；validate_arguments 按工具参数
Schema 对 LLM 返回的参数做显式校验，非法 JSON / 缺必填 / 未知
字段一律返回中文错误列表（空列表 = 通过），由调用方结构化回传
LLM 重试，本模块不抛异常中断主流程。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.llm import AIMessage, ToolCall


@dataclass
class FinalAnswer:
    """最终答案：模型不再发起工具调用时的决策结果。"""

    content: str


@dataclass
class ToolCallBatch:
    """工具调用批次：模型本轮要执行的一组 ToolCall。"""

    calls: list[ToolCall]


AgentDecision = FinalAnswer | ToolCallBatch


class ToolArgumentError(Exception):
    """工具参数校验失败的异常载体（含 tool_call_id 与原因）。

    validate_arguments 本身返回错误列表不抛异常；调用方（loop 层）
    需要以异常形式上抛时使用本类。
    """

    def __init__(self, message: str, tool_call_id: str) -> None:
        """记录错误消息与对应工具调用 id，str(exc) 即错误消息。"""
        super().__init__(message)
        self.tool_call_id = tool_call_id


def parse_response(message: AIMessage) -> AgentDecision:
    """把 LLM 单次返回解析为决策：有工具调用走批次，否则为最终答案。

    恒定二分：tool_calls 非空 → ToolCallBatch（calls 为同一列表）；
    否则一律 FinalAnswer（空 content + 空工具调用也归入此类，
    content 为空串，不抛异常）。
    """
    if message.tool_calls:
        return ToolCallBatch(calls=message.tool_calls)
    return FinalAnswer(content=message.content)


def validate_arguments(call: ToolCall, schema: dict[str, Any]) -> list[str]:
    """按参数 Schema 显式校验工具调用参数，返回中文错误列表（空 = 通过）。

    args 为 None（arguments 是非法 JSON）时返回含「JSON」的错误且
    不再检查其余项；required 中缺失的字段逐个报错；schema 含
    properties 时 args 里的未知键逐个报错；schema 无这两键时不
    约束。所有错误均为可直接回传 LLM 的中文字符串。
    """
    errors: list[str] = []
    if call.args is None:
        errors.append(
            f"工具 {call.name} 的参数不是合法 JSON，无法解析"
            f"（原文：{call.arguments_raw}）"
        )
        return errors
    required = schema.get("required") or []
    for field_name in required:
        if field_name not in call.args:
            errors.append(f"工具 {call.name} 缺少必填字段：{field_name}")
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key in call.args:
            if key not in properties:
                errors.append(f"工具 {call.name} 收到未知字段：{key}")
    return errors
