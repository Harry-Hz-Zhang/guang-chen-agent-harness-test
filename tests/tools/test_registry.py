"""ToolRegistry 与 BaseTool 单元测试：注册查询、重复保护、OpenAI 格式导出与执行契约。"""

import json
from typing import Any

import pytest

from harness.tools.base import BaseTool
from harness.tools.registry import (
    DuplicateToolError,
    ToolNotFoundError,
    ToolRegistry,
)

_CALC_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "expression": {"type": "string", "description": "要计算的算式"},
    },
    "required": ["expression"],
}


class FakeTool(BaseTool):
    """测试用假工具：按注入的元数据注册，execute 透传固定结果。"""

    def __init__(
        self,
        name: str,
        description: str = "测试工具",
        parameters: dict | None = None,
        result: str = "2",
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = (
            parameters if parameters is not None else _CALC_PARAMETERS
        )
        self._result = result

    def execute(self, **kwargs: Any) -> str:
        """返回构造时注入的固定结果字符串。"""
        return self._result


class TestToolRegistry:
    """覆盖 ToolRegistry 的注册、查询、重复保护与 OpenAI 工具格式导出。"""

    def testRegisterAndGet(self) -> None:
        """注册 fake 工具后 get 按名取回同一实例，元数据与注册时一致。"""
        registry = ToolRegistry()
        tool = FakeTool(name="calc", description="计算器")
        registry.register(tool)
        got = registry.get("calc")
        assert got is tool
        assert got.name == "calc"
        assert got.description == "计算器"
        assert got.parameters == _CALC_PARAMETERS

    def testGetUnknownRaises(self) -> None:
        """get 查询未注册名称时抛 ToolNotFoundError，消息含被查名称。"""
        registry = ToolRegistry()
        with pytest.raises(ToolNotFoundError) as excinfo:
            registry.get("nope")
        assert "nope" in str(excinfo.value)

    def testDuplicateRegisterRaises(self) -> None:
        """重复注册同名工具抛 DuplicateToolError，原工具不被覆盖。"""
        registry = ToolRegistry()
        first = FakeTool(name="calc", description="计算器")
        registry.register(first)
        second = FakeTool(name="calc", description="另一个计算器")
        with pytest.raises(DuplicateToolError):
            registry.register(second)
        assert registry.get("calc") is first
        assert len(registry.names()) == 1

    def testToOpenaiToolsFormat(self) -> None:
        """to_openai_tools 输出官方 function calling 三键格式且可 json.dumps。"""
        registry = ToolRegistry()
        registry.register(FakeTool(name="calc", description="计算器"))
        registry.register(FakeTool(name="search", description="搜索"))
        tools = registry.to_openai_tools()
        assert isinstance(tools, list)
        assert len(tools) == 2
        assert {item["function"]["name"] for item in tools} == {
            "calc",
            "search",
        }
        for item in tools:
            assert item["type"] == "function"
            function = item["function"]
            assert set(function.keys()) == {"name", "description", "parameters"}
            assert function["parameters"] == _CALC_PARAMETERS
        json.dumps(tools)

    def testNamesSorted(self) -> None:
        """names() 按字母序返回全部已注册工具名。"""
        registry = ToolRegistry()
        for name in ("b", "a", "c"):
            registry.register(FakeTool(name=name))
        assert registry.names() == ["a", "b", "c"]


class TestBaseTool:
    """覆盖 BaseTool 抽象契约的执行行为。"""

    def testExecuteReturnsString(self) -> None:
        """fake 工具 execute(expression="1+1") 返回 "2" 且类型为 str。"""
        tool = FakeTool(name="calc", description="计算器", result="2")
        result = tool.execute(expression="1+1")
        assert result == "2"
        assert isinstance(result, str)
