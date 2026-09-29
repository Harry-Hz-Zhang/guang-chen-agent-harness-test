"""RuntimeState 与会话上下文 —— 进程级公共内存状态：按会话隔离，供工具与运行时组件共享。

CURRENT_SESSION_ID 由 ReactLoop.run 在入口绑定、出口复位：协程（asyncio
Task 各自拷贝上下文）与线程（各自独立上下文）天然隔离，多个 agent
同时执行时，有状态工具据此读到各自会话的数据。状态纯内存、不落盘，
进程退出即丢。
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field

CURRENT_SESSION_ID: ContextVar[str] = ContextVar(
    "harness_current_session_id", default=""
)
"""当前执行上下文绑定的会话 id（默认空串表示未绑定）。"""


@dataclass
class RuntimeState:
    """运行时公共状态（纯内存、按会话隔离、线程安全）。

    todos_by_session 以 session_id 为键存放各会话的待办列表；读写均
    持锁，多个 agent 在不同线程/协程同时执行时互不串扰。由 main()
    创建一次并注入需要它的组件（当前消费者为 write_todos 工具）。
    """

    todos_by_session: dict[str, list[dict]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_todos(self, session_id: str, todos: list[dict]) -> None:
        """全量替换指定会话的待办列表（线程安全）。"""
        with self._lock:
            self.todos_by_session[session_id] = todos

    def todos(self, session_id: str) -> list[dict]:
        """读取指定会话的待办列表副本（线程安全；无记录时为空列表）。"""
        with self._lock:
            return list(self.todos_by_session.get(session_id, []))
