"""parser 单元测试：parse_response 的恒定二分与工具参数显式校验。

直接构造 AIMessage / ToolCall（来自 harness.llm），不涉及任何
网络调用；校验失败路径断言「返回错误列表而非抛异常」，与
spec agent-loop「工具参数显式校验」逐条对应。
"""

from harness.llm import AIMessage, ToolCall
from harness.parser import (
    FinalAnswer,
    ToolArgumentError,
    ToolCallBatch,
    parse_response,
    validate_arguments,
)


class TestParser:
    """parse_response 与 validate_arguments 的行为契约。"""

    def testFinalAnswer(self) -> None:
        """无工具调用时返回 FinalAnswer，content 原样保留。"""
        message = AIMessage(content="答案", tool_calls=[])
        decision = parse_response(message)
        assert isinstance(decision, FinalAnswer)
        assert decision.content == "答案"

    def testToolCallBatch(self) -> None:
        """带工具调用时返回 ToolCallBatch，calls 为同一列表。"""
        call = ToolCall(
            id="call_1",
            name="calc",
            arguments_raw='{"a": 1}',
            args={"a": 1},
        )
        message = AIMessage(content="", tool_calls=[call])
        decision = parse_response(message)
        assert isinstance(decision, ToolCallBatch)
        assert decision.calls[0].args == {"a": 1}
        assert decision.calls[0].id == "call_1"

    def testEmptyContentNoToolsIsFinal(self) -> None:
        """空 content + 空工具调用仍为 FinalAnswer（空串），不抛异常。"""
        message = AIMessage(content="", tool_calls=[])
        decision = parse_response(message)
        assert isinstance(decision, FinalAnswer)
        assert decision.content == ""

    def testValidateMissingRequired(self) -> None:
        """required 字段缺失时错误列表含该字段名。"""
        call = ToolCall(
            id="call_2",
            name="weather",
            arguments_raw="{}",
            args={},
        )
        schema = {"required": ["city"]}
        errors = validate_arguments(call, schema)
        assert any("city" in e for e in errors)

    def testValidateInvalidJSONRaw(self) -> None:
        """args 为 None（非法 JSON）时返回含「JSON」的错误，不抛异常。"""
        call = ToolCall(
            id="call_3",
            name="weather",
            arguments_raw="oops",
            args=None,
        )
        errors = validate_arguments(call, {"required": ["city"]})
        assert any("JSON" in e for e in errors)

    def testValidateUnknownArgReported(self) -> None:
        """schema 有 properties 时未知字段逐个报错，消息含键名。"""
        call = ToolCall(
            id="call_4",
            name="weather",
            arguments_raw='{"city": "北京", "foo": 1}',
            args={"city": "北京", "foo": 1},
        )
        schema = {
            "required": ["city"],
            "properties": {"city": {"type": "string"}},
        }
        errors = validate_arguments(call, schema)
        assert any("foo" in e for e in errors)

    def testValidatePassReturnsEmpty(self) -> None:
        """合法参数返回空列表（零错误）。"""
        call = ToolCall(
            id="call_5",
            name="weather",
            arguments_raw='{"city": "北京"}',
            args={"city": "北京"},
        )
        schema = {
            "required": ["city"],
            "properties": {"city": {"type": "string"}},
        }
        assert validate_arguments(call, schema) == []

    def testToolArgumentErrorCarriesContext(self) -> None:
        """ToolArgumentError 携带 tool_call_id，str() 为原始消息。"""
        exc = ToolArgumentError("缺少必填字段 city", "call_9")
        assert exc.tool_call_id == "call_9"
        assert str(exc) == "缺少必填字段 city"
