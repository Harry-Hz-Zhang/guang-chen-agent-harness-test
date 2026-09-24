"""ToolRegistry —— 工具注册表：按名注册、查询与导出 OpenAI 工具格式。"""

from __future__ import annotations

from harness.tools.base import BaseTool


class DuplicateToolError(Exception):
    """向注册表重复注册同名工具时抛出，消息含重名工具名。"""


class ToolNotFoundError(Exception):
    """从注册表查询未注册的工具名时抛出，消息含被查名称。"""


class ToolRegistry:
    """维护工具名到 BaseTool 实例的映射，供主循环注册、查找与导出工具表。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册一个工具实例。

        同名工具已存在时抛 DuplicateToolError，且已注册的原工具
        不被覆盖。
        """
        if tool.name in self._tools:
            raise DuplicateToolError(f"工具名重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        """按名查询已注册的工具实例；未注册时抛 ToolNotFoundError。"""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"未注册的工具：{name}") from None

    def names(self) -> list[str]:
        """返回全部已注册工具名，按字母序排序。"""
        return sorted(self._tools)

    def to_openai_tools(self) -> list[dict]:
        """导出为 OpenAI function calling 的 tools 参数格式。

        每项形如 {"type": "function", "function": {"name",
        "description", "parameters"}}，全部为纯 JSON 类型，
        可直接 json.dumps，按工具名排序输出。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for name, tool in sorted(self._tools.items())
        ]
