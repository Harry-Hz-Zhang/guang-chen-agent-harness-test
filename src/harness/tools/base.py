"""BaseTool 抽象与 ToolExecutionError —— 工具体系的最小契约定义。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ToolExecutionError(Exception):
    """工具执行失败的统一异常，携带工具名、入参与原始异常，供结构化回传给 LLM。

    属性：tool 为工具名；tool_args 为本次调用入参（缺省空 dict）；
    original 为被包装的原始异常（缺省 None）；str(exc) 为中文人类可读
    错误描述。
    """

    def __init__(
        self,
        message: str,
        tool: str = "",
        tool_args: dict[str, Any] | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.tool: str = tool
        self.tool_args: dict[str, Any] = (
            tool_args if tool_args is not None else {}
        )
        self.original: BaseException | None = original


class BaseTool(ABC):
    """所有工具的抽象基类。

    子类以类属性或实例属性声明 name（工具唯一名）、description
    （中文描述）与 parameters（JSON Schema，形如 {"type": "object", ...}），
    并实现 execute。本基类不提供 __init__，初始化方式由子类自行决定。
    """

    name: str
    description: str
    parameters: dict

    @abstractmethod
    def execute(self, **kwargs: Any) -> str:
        """执行工具逻辑并返回字符串结果；失败时抛 ToolExecutionError。"""
