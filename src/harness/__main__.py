"""harness CLI 入口 —— argparse 参数解析、组件装配与 REPL 交互循环。

REPL 内置命令：/exit 退出、/new 切换新会话、/sessions 列出全部会话、
/history 当前会话概览（命令不进入 LLM）。流式模式下思考与正文分通道
渲染（思考带「思考」前缀）；--no-stream 关闭流式（整段输出）、
--no-thinking 关闭思考模式。LLM 调用失败输出可读提示并保持 REPL
可用；缺少 DEEPSEEK_API_KEY 时启动即给出设置指引并以非 0 状态结束。
"""

from __future__ import annotations

import argparse
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

_REASONING_PREFIX = "思考｜"


def render_event(event: StreamEvent) -> str:
    """把单个流式事件渲染为终端文本（思考带前缀、正文原样，其余为空）。

    思考内容与正文分通道：思考行带「思考」前缀（弱化样式由调用方
    决定），正文行不带前缀——两者视觉可区分。
    """
    from harness.llm import ReasoningDelta, TextDelta

    if isinstance(event, ReasoningDelta):
        return f"{_REASONING_PREFIX}{event.text}"
    if isinstance(event, TextDelta):
        return event.text
    return ""


def _generate_session_id() -> str:
    """生成短随机会话 id（8 位小写 hex）。"""
    return secrets.token_hex(4)


def _default_writer(text: str) -> None:
    """默认输出器：写标准输出（用户可见交互输出，非调试打印）。"""
    sys.stdout.write(text + "\n")


def run_repl(
    loop: Any,
    sessions: Any,
    config: RuntimeConfig,
    lines: Iterable[str],
    writer: Writer,
    session_id: str | None = None,
) -> str:
    """执行 REPL 交互循环，返回最终会话 id。

    命令（/exit /new /sessions /history）不进入 LLM；普通输入路由到
    loop.run（流式配置下挂 on_event 分通道渲染）；LLM 调用失败输出
    可读提示后继续消费输入，不退出。
    """
    if session_id is None:
        session_id = _generate_session_id()
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

    def on_event(event: StreamEvent) -> None:
        """流式事件分通道渲染（空渲染跳过）。"""
        rendered = render_event(event)
        if rendered:
            writer(rendered)

    for line in lines:
        text = line.strip()
        if not text:
            continue
        if text == _EXIT_COMMAND:
            break
        if text == _NEW_COMMAND:
            session_id = _generate_session_id()
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
            rounds = sessions.message_rounds(session_id)
            writer(f"当前会话 {session_id}：累计 {rounds} 轮对话")
            continue
        try:
            result = loop.run(
                text,
                session_id,
                on_event=on_event if config.stream_enabled else None,
            )
        except LLMError as exc:
            writer(f"出错了：{exc}")
            continue
        if not config.stream_enabled or result.truncated:
            writer(result.answer)
        elif config.stream_enabled:
            writer("")
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


def _build_registry(config: RuntimeConfig, session_id: str) -> ToolRegistry:
    """注册四个内置工具（todo 绑定启动会话）。"""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    registry.register(SearchTool())
    registry.register(WeatherTool())
    registry.register(TodoTool(config.data_dir, session_id))
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
    sessions = SessionStore(config.data_dir)
    memory = MemoryStore(config.data_dir, llm)
    builder = ContextBuilder(sessions, memory, config)
    registry = _build_registry(config, session_id)
    trace = TraceCollector(JsonlExporter(config.data_dir / "traces"))
    compressor = ContextCompressor(sessions, llm, trace, config)
    middlewares: list[Middleware] = [CompactionMiddleware(compressor)]
    loop = ReactLoop(llm, registry, sessions, builder, trace, middlewares, config)
    summarizer = MemorySummarizer(sessions, memory, llm, trace, config)
    summarizer.start()
    input_lines: Iterable[str] = lines if lines is not None else sys.stdin
    try:
        run_repl(loop, sessions, config, input_lines, output, session_id=session_id)
    finally:
        summarizer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
