"""ReadMemoryTool —— 按文件名读取全局长期记忆的详细内容。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.memory.store import MemoryStore
from harness.tools.base import BaseTool, ToolExecutionError


class ReadMemoryTool(BaseTool):
    """记忆读取工具：按记忆索引中的文件名读取记忆全文。

    文件名非法（非字符串 / 空 / 非 .md / 含路径分隔符或路径穿越形态）
    抛 ToolExecutionError 结构化回传；文件不存在返回友好提示（与
    search 工具的「未找到」风格一致，可恢复场景不抛异常）。
    """

    name: str = "read_memory"
    description: str = "读取长期记忆详细内容：按记忆索引中的文件名读取对应记忆全文"
    parameters: dict = {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": (
                    "记忆文件名（来自系统提示词中的记忆索引），"
                    "如 20260928-143005.md"
                ),
            },
        },
        "required": ["file"],
    }

    def __init__(self, memory: MemoryStore) -> None:
        """注入全局记忆存储（MEMORY 目录布局单一真源）。"""
        self._memory = memory

    def execute(self, **kwargs: Any) -> str:
        """校验文件名后读取记忆全文；不存在返回未找到提示。"""
        file = kwargs.get("file")
        if (
            not isinstance(file, str)
            or not file
            or not file.endswith(".md")
            or "/" in file
            or "\\" in file
            or Path(file).name != file
        ):
            raise ToolExecutionError(
                "参数 file 必须为记忆索引中的 md 文件名（不含路径）",
                tool=self.name,
                tool_args=kwargs,
            )
        content = self._memory.read(file)
        if content is None:
            return f"未找到记忆文件 {file}，请确认文件名来自记忆索引"
        return content
