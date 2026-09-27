"""harness CLI 入口 —— argparse 参数解析、组件装配与 REPL 交互循环。

REPL 内置命令：/exit 退出、/new 切换新会话、/sessions 列出全部会话、
/history 当前会话概览（命令不进入 LLM）。流式模式下思考与正文分通道
渲染（思考带「思考」前缀）；--no-stream 关闭流式（整段输出）、
--no-thinking 关闭思考模式。LLM 调用失败输出可读提示并保持 REPL
可用；缺少 DEEPSEEK_API_KEY 时启动即给出设置指引并以非 0 状态结束。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import secrets
import sys
from typing import Any, Callable, Iterable

from harness.config import RuntimeConfig
from harness.context.builder import ContextBuilder
from harness.context.compressor import CompactionMiddleware, ContextCompressor
from harness.llm import LLMClient, LLMError, StreamEvent
from harness.loop import ReactLoop
from harness.memory.store import MemoryStore
from harness.memory.summarizer import MemorySummarizer
from harness.middleware import Middleware
from harness.session.store import SessionStore
from harness.tools.base import BaseTool
from harness.tools.calculator import CalculatorTool
from harness.tools.registry import ToolRegistry
from harness.tools.search import SearchTool
from harness.tools.todo import TodoTool
from harness.tools.weather import WeatherTool
from harness.trace import JsonlExporter, TraceCollector

Writer = Callable[[str], None]

_EXIT_COMMAND = "/exit"
_NEW_COMMAND = "/new"
_SESSIONS_COMMAND = "/sessions"
_HISTORY_COMMAND = "/history"

from harness.renderer import DEFAULT_REASONING_PREFIX, StreamRenderer, render_event

_REASONING_PREFIX = DEFAULT_REASONING_PREFIX


class RebindableTodoTool(BaseTool):
    """会话可重绑定的待办管理工具包装器。

    持有一个包含当前 session_id 的字典引用（{"session_id": "..."}），
    当用户通过 /new 等命令切换会话时，动态委托到对应会话的 TodoTool，
    实现多会话隔离存储的无缝切换。
    """

    name: str = TodoTool.name
    description: str = TodoTool.description
    parameters: dict = TodoTool.parameters

    def __init__(self, data_dir: Path, session_ref: dict[str, str]) -> None:
        """保存数据根目录与当前会话引用字典。"""
        self._data_dir: Path = data_dir
        self._session_ref: dict[str, str] = session_ref

    @property
    def session_id(self) -> str:
        """返回当前绑定的会话 id。"""
        return self._session_ref["session_id"]

    def execute(self, **kwargs: Any) -> str:
        """按当前 session_id 构造 TodoTool 并执行。"""
        tool = TodoTool(self._data_dir, self.session_id)
        return tool.execute(**kwargs)


def _generate_session_id() -> str:
    """生成短随机会话 id（8 位小写 hex）。"""
    return secrets.token_hex(4)


def _default_writer(text: str) -> None:
    """默认输出器：写标准输出（用户可见交互输出，非调试打印）。"""
    sys.stdout.write(text + "\n")


def _default_raw_writer(text: str) -> None:
    """默认流式分片输出器：写标准输出并立即刷新。"""
    sys.stdout.write(text)
    sys.stdout.flush()


def run_repl(
    loop: Any,
    sessions: Any,
    config: RuntimeConfig,
    lines: Iterable[str],
    writer: Writer,
    session_id: str | None = None,
    raw_writer: Writer | None = None,
    on_session_change: Callable[[str], None] | None = None,
) -> str:
    """执行 REPL 交互循环，返回最终会话 id。

    命令（/exit /new /sessions /history）不进入 LLM；普通输入路由到
    loop.run（流式配置下挂 on_event 分通道渲染）；LLM 调用失败输出
    可读提示后继续消费输入，不退出。
    """
    if session_id is None:
        session_id = _generate_session_id()
        if on_session_change is not None:
            on_session_change(session_id)
        writer(f"已创建新会话 {session_id}（命令：/new 新会话、/sessions 全部会话、/history 概览、/exit 退出）")
    else:
        message_count = sum(
            1
            for record in sessions.load_records(session_id)
            if record.get("kind") == "message"
        )
        if message_count > 0:
            writer(f"已续接会话 {session_id}（{message_count} 条消息）")
        else:
            writer(f"已创建新会话 {session_id}（命令：/new 新会话、/sessions 全部会话、/history 概览、/exit 退出）")

    target_raw: Writer = raw_writer if raw_writer is not None else _default_raw_writer

    for line in lines:
        text = line.strip()
        if not text:
            continue
        if text == _EXIT_COMMAND:
            break
        if text == _NEW_COMMAND:
            session_id = _generate_session_id()
            if on_session_change is not None:
                on_session_change(session_id)
            writer(f"已创建新会话 {session_id}")
            continue
        if text == _SESSIONS_COMMAND:
            ids = sessions.session_ids()
            if ids:
                writer("全部会话：" + "、".join(ids))
            else:
                writer("暂无其他会话")
            continue
        if text == _HISTORY_COMMAND:
            records = sessions.load_records(session_id)
            msg_count = sum(1 for r in records if r.get("kind") == "message")
            rounds = sessions.message_rounds(session_id)
            compactions = [r for r in records if r.get("kind") == "compaction"]
            if compactions:
                last_comp = compactions[-1]
                writer(
                    f"当前会话 {session_id}：{msg_count} 条消息，累计 {rounds} 轮对话"
                    f"，最近压缩至序号 {last_comp.get('compressed_up_to', 0)}"
                )
            else:
                writer(f"当前会话 {session_id}：{msg_count} 条消息，累计 {rounds} 轮对话")
            continue

        renderer = StreamRenderer(
            raw_writer=target_raw, reasoning_prefix=_REASONING_PREFIX
        )

        try:
            result = loop.run(
                text,
                session_id,
                on_event=renderer.render if config.stream_enabled else None,
            )
        except LLMError as exc:
            writer(f"出错了：{exc}")
            continue
        if not config.stream_enabled or result.truncated:
            writer(result.answer)
        elif config.stream_enabled:
            renderer.finalize()
    return session_id


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析 CLI 参数：--session、--no-stream、--no-thinking。"""
    parser = argparse.ArgumentParser(
        prog="harness", description="最小可用 Agent Runtime（DeepSeek + ReAct）"
    )
    parser.add_argument("--session", default=None, help="以指定会话 id 启动（已存在则续接历史）")
    parser.add_argument("--no-stream", action="store_true", help="关闭流式输出（整段输出）")
    parser.add_argument("--no-thinking", action="store_true", help="关闭思考模式")
    return parser.parse_args(argv)


def _build_registry(
    config: RuntimeConfig, session_ref: dict[str, str] | str
) -> ToolRegistry:
    """注册四个内置工具（todo 绑定 session_ref 指向的会话）。"""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    registry.register(SearchTool())
    registry.register(WeatherTool())
    if isinstance(session_ref, str):
        ref = {"session_id": session_ref}
    else:
        ref = session_ref
    registry.register(RebindableTodoTool(config.data_dir, ref))
    return registry


def main(
    argv: list[str] | None = None,
    lines: Iterable[str] | None = None,
    writer: Writer | None = None,
) -> int:
    """CLI 主入口：装配全部组件并进入 REPL，返回进程退出码。

    缺少 DEEPSEEK_API_KEY 时输出设置指引并返回 1（不发起任何 API
    调用）；正常路径返回 0。
    """
    args = _parse_args(argv)
    output: Writer = writer if writer is not None else _default_writer
    config = RuntimeConfig.from_env()
    if args.no_stream:
        config.stream_enabled = False
    if args.no_thinking:
        config.thinking_enabled = False

    try:
        llm = LLMClient(config)
    except LLMError as exc:
        output(str(exc))
        return 1

    session_id = args.session if args.session else _generate_session_id()
    session_ref: dict[str, str] = {"session_id": session_id}

    def on_session_change(new_session_id: str) -> None:
        session_ref["session_id"] = new_session_id

    sessions = SessionStore(config.data_dir)
    memory = MemoryStore(config.data_dir, llm)
    builder = ContextBuilder(sessions, memory, config)
    registry = _build_registry(config, session_ref)
    trace = TraceCollector(JsonlExporter(config.data_dir / "traces"))
    compressor = ContextCompressor(sessions, llm, trace, config)
    middlewares: list[Middleware] = [CompactionMiddleware(compressor)]
    loop = ReactLoop(llm, registry, sessions, builder, trace, middlewares, config)
    summarizer = MemorySummarizer(sessions, memory, llm, trace, config)
    summarizer.start()
    input_lines: Iterable[str] = lines if lines is not None else sys.stdin
    try:
        run_repl(
            loop,
            sessions,
            config,
            input_lines,
            output,
            session_id=session_id,
            raw_writer=_default_raw_writer,
            on_session_change=on_session_change,
        )
    finally:
        summarizer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
