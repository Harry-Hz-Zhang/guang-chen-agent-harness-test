"""TodoTool —— 按会话隔离并持久化到本地 JSON 文件的待办管理工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.tools.base import BaseTool, ToolExecutionError

_TODOS_DIR_NAME: str = "todos"
_EMPTY_LIST_MESSAGE: str = "暂无待办"
_INVALID_SESSION_ID_CHARS: frozenset[str] = frozenset('\\/:*?"<>|')


class TodoTool(BaseTool):
    """待办管理工具：add 添加待办（编号自 1 递增）、list 列出当前会话待办。

    存储位置为 data_dir/todos/<session_id>.json，内容为待办文本的
    JSON 数组，每次变更整文件重写（todo 不受会话 JSONL 追加式
    约束）；不同 session_id 互不可见，新实例同目录同会话可读回
    全部待办。注意：本工具按读-改-写整文件方式更新，非并发
    安全（多进程/多线程同时操作同一会话文件可能互相覆盖）。
    """

    name: str = "todo"
    description: str = (
        "待办管理：action=add 添加一条待办（参数 todo 为待办内容），"
        "action=list 列出当前会话的全部待办"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list"],
                "description": "动作：add 添加待办，list 列出待办",
            },
            "todo": {
                "type": "string",
                "description": "待办内容（action 为 add 时必填）",
            },
        },
        "required": ["action"],
    }

    def __init__(self, data_dir: Path, session_id: str) -> None:
        """按数据根目录与会话 id 构造工具，存储文件为 todos/<session_id>.json。

        session_id 为空、为 "."/".."，或含路径分隔符与 Windows
        保留字符（\\ / : * ? " < > |）时抛 ToolExecutionError。
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ToolExecutionError(
                "session_id 必须为非空字符串",
                tool=self.name,
                tool_args={"session_id": session_id},
            )
        if session_id in {".", ".."} or any(
            ch in _INVALID_SESSION_ID_CHARS for ch in session_id
        ):
            raise ToolExecutionError(
                f"session_id 含非法路径字符：{session_id!r}",
                tool=self.name,
                tool_args={"session_id": session_id},
            )
        self.session_id: str = session_id
        self._file: Path = data_dir / _TODOS_DIR_NAME / f"{session_id}.json"

    def add(self, todo: str) -> str:
        """追加一条待办并整文件重写，返回含自 1 递增编号的中文提示。"""
        if not isinstance(todo, str) or not todo.strip():
            raise ToolExecutionError(
                "待办内容 todo 必须为非空字符串",
                tool=self.name,
                tool_args={"action": "add", "todo": todo},
            )
        todos = self._load()
        todos.append(todo)
        self._save(todos)
        return f"已添加待办 {len(todos)}：{todo}"

    def list_todos(self) -> list[str]:
        """返回当前会话全部待办文本；无待办时返回空列表。"""
        return self._load()

    def execute(self, **kwargs: Any) -> str:
        """按 action 分发：add 走添加并返回编号提示，list 渲染多行待办文本。"""
        action = kwargs.get("action")
        if action == "add":
            todo = kwargs.get("todo")
            if not isinstance(todo, str) or not todo.strip():
                raise ToolExecutionError(
                    "action=add 时参数 todo 必须为非空字符串",
                    tool=self.name,
                    tool_args=kwargs,
                )
            return self.add(todo)
        if action == "list":
            todos = self.list_todos()
            if not todos:
                return _EMPTY_LIST_MESSAGE
            return "\n".join(
                f"{index}. {text}"
                for index, text in enumerate(todos, start=1)
            )
        raise ToolExecutionError(
            f"未知的 action：{action!r}（仅支持 add / list）",
            tool=self.name,
            tool_args=kwargs,
        )

    def _load(self) -> list[str]:
        """从存储文件读回待办列表；文件不存在视为空，格式非法抛结构化错误。"""
        if not self._file.exists():
            return []
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolExecutionError(
                f"待办文件读取失败：{self._file}",
                tool=self.name,
                original=exc,
            ) from exc
        if not isinstance(data, list) or not all(
            isinstance(item, str) for item in data
        ):
            raise ToolExecutionError(
                f"待办文件格式非法（期望字符串数组）：{self._file}",
                tool=self.name,
            )
        return data

    def _save(self, todos: list[str]) -> None:
        """把待办列表整文件重写到 JSON 文件（非追加式写入）。"""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                json.dumps(todos, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            raise ToolExecutionError(
                f"待办文件写入失败：{self._file}",
                tool=self.name,
                original=exc,
            ) from exc
