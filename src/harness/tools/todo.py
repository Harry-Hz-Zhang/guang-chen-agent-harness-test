"""WriteTodosTool —— 待办清单写入工具（无状态：校验 + 渲染）。"""

from __future__ import annotations

from typing import Any

from harness.tools.base import BaseTool, ToolExecutionError

_STATUS_LABELS: dict[str, str] = {
    "pending": "待办",
    "in_progress": "进行中",
    "completed": "已完成",
}
_MAX_TODOS: int = 100
_MAX_CONTENT_CHARS: int = 1000


class WriteTodosTool(BaseTool):
    """待办写入工具：接收完整列表，校验后渲染为文本回传。

    工具自身无状态：不落内存也不落盘，渲染文本以 role=tool 消息
    落入会话文件历史，LLM 在后续轮次经上下文读到待办。todos 为
    整张计划清单：执行多步任务前先写入计划，过程中随进度更新
    各条 status。
    """

    name: str = "write_todos"
    description: str = (
        "写入待办清单（全量替换）：传入完整列表，每项为 {content, status}，"
        "status 取 pending（待办）/ in_progress（进行中）/ completed（已完成）。"
        "执行多步任务前先写计划，过程中随进度更新各条状态。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "完整待办列表（每次调用替换既有全部待办）",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "待办内容"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "状态",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def execute(self, **kwargs: Any) -> str:
        """校验待办列表并渲染为带编号与状态的文本。"""
        todos = self._validate(kwargs.get("todos"))
        return self._render(todos)

    def _validate(self, todos: Any) -> list[dict]:
        """显式校验 LLM 输入：数组、限长，各项含非空 content 与合法 status。"""
        if not isinstance(todos, list):
            raise ToolExecutionError("todos 必须为数组", tool=self.name)
        if len(todos) > _MAX_TODOS:
            raise ToolExecutionError(
                f"待办数量超上限（最多 {_MAX_TODOS} 条）", tool=self.name
            )
        checked: list[dict] = []
        for index, item in enumerate(todos, start=1):
            if not isinstance(item, dict):
                raise ToolExecutionError(
                    f"第 {index} 项必须是对象", tool=self.name
                )
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ToolExecutionError(
                    f"第 {index} 项 content 必须为非空字符串", tool=self.name
                )
            if len(content) > _MAX_CONTENT_CHARS:
                raise ToolExecutionError(
                    f"第 {index} 项 content 超长（最多 {_MAX_CONTENT_CHARS} 字符）",
                    tool=self.name,
                )
            status = item.get("status")
            if status not in _STATUS_LABELS:
                raise ToolExecutionError(
                    f"第 {index} 项 status 非法（仅支持 pending / in_progress / completed）",
                    tool=self.name,
                )
            checked.append({"content": content, "status": status})
        return checked

    def _render(self, todos: list[dict]) -> str:
        """把待办列表渲染为带编号与中文状态的多行文本。"""
        if not todos:
            return "已写入 0 条待办（清空）"
        lines = [f"已写入 {len(todos)} 条待办："]
        for index, item in enumerate(todos, start=1):
            label = _STATUS_LABELS[item["status"]]
            lines.append(f"{index}. [{label}] {item['content']}")
        return "\n".join(lines)
