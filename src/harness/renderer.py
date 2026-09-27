"""流式事件终端渲染器与状态管理。

负责把 LLM 流式分片事件（StreamEvent）转换为终端可读的文本输出。
维护思考通道（带前缀）与正文通道（无前缀）的状态切换，并在回合结束时补齐换行。
"""

from __future__ import annotations

from typing import Callable

from harness.llm import ReasoningDelta, StreamEvent, TextDelta

DEFAULT_REASONING_PREFIX = "思考｜"


class StreamRenderer:
    """流式事件终端渲染器（维护分通道状态）。"""

    def __init__(
        self,
        raw_writer: Callable[[str], None] | None = None,
        reasoning_prefix: str = DEFAULT_REASONING_PREFIX,
    ) -> None:
        """初始化渲染器状态与配置。"""
        self._raw_writer: Callable[[str], None] | None = raw_writer
        self._reasoning_prefix: str = reasoning_prefix
        self.in_reasoning: bool = False
        self.streamed_any: bool = False

    def render(self, event: StreamEvent) -> str:
        """把单个流式事件渲染为文本片段，并推进内部状态。

        若配置了 raw_writer，则逐个向其写入产生的文本片段。
        - ReasoningDelta：首次进入思考段输出思考前缀（若此前已有输出则先换行），后续片原样输出；
        - TextDelta：若由思考段切换至正文段，先换行，后续片原样输出；
        - 其他事件（如 UsageEvent、DoneEvent）：返回空字符串且无写入副作用。
        """
        pieces: list[str] = []
        if isinstance(event, ReasoningDelta):
            if not self.in_reasoning:
                if self.streamed_any:
                    pieces.append("\n")
                pieces.append(self._reasoning_prefix)
                self.in_reasoning = True
            pieces.append(event.text)
            self.streamed_any = True
        elif isinstance(event, TextDelta):
            if self.in_reasoning:
                pieces.append("\n")
                self.in_reasoning = False
            pieces.append(event.text)
            self.streamed_any = True

        if self._raw_writer is not None:
            for piece in pieces:
                self._raw_writer(piece)
        return "".join(pieces)

    def finalize(self) -> str:
        """回合结束时补齐换行（若曾输出过内容）。

        若配置了 raw_writer，则同步写入换行符。
        """
        if self.streamed_any:
            if self._raw_writer is not None:
                self._raw_writer("\n")
            return "\n"
        return ""

    def reset(self) -> None:
        """重置状态机，供下一轮交互使用。"""
        self.in_reasoning = False
        self.streamed_any = False


def render_event(
    event: StreamEvent,
    prefix: str = DEFAULT_REASONING_PREFIX,
) -> str:
    """把单个流式事件渲染为终端文本（纯函数向后兼容接口）。

    思考内容与正文分通道：思考行带前缀，正文行不带前缀。
    """
    if isinstance(event, ReasoningDelta):
        return f"{prefix}{event.text}"
    if isinstance(event, TextDelta):
        return event.text
    return ""
