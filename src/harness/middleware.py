"""中间件基类与 LoopState —— 主循环各阶段的可插拔扩展点。

ReactLoop 在关键节点（模型调用前/后）调用中间件钩子；基类两个
钩子均提供默认空实现，子类按需覆盖其一即可。
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any


@dataclass
class LoopState:
    """单次用户请求在主循环中的共享状态。

    trace_id 由 TraceCollector.start_trace 注入（缺省空串），压缩
    中间件据此把压缩用的 LLM 调用挂到同一 trace 下（见设计决策 5）。
    """

    session_id: str
    round_no: int
    messages: list[dict[str, Any]]
    user_input: str
    trace_id: str = ""


class Middleware(ABC):
    """主循环中间件基类：两钩子默认全部空实现，可直接实例化使用。"""

    def before_model(self, state: LoopState) -> None:
        """每轮调用 LLM 前触发；默认不做任何事。"""
        return None

    def after_model(self, state: LoopState) -> None:
        """每轮 LLM 返回后触发；默认不做任何事。"""
        return None
