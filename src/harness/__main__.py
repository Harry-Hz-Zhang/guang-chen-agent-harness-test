"""harness CLI 入口 —— argparse 参数解析、组件装配与 REPL 交互循环。

REPL 内置命令：/exit 退出、/new 新会话、/switch <id> 切换已有会话、
/sessions 列出全部会话、/history 当前会话概览（命令不进入 LLM）。流式
模式下思考与正文分通道
渲染（思考带「思考」前缀）；--no-stream 关闭流式（整段输出）、
--no-thinking 关闭思考模式。--concurrent SESSION:输入 [...] 以线程池
并发执行多个会话任务后输出结果退出（不进 REPL、不流式）。LLM 调用
失败输出可读提示并保持 REPL 可用；缺少 DEEPSEEK_API_KEY 时启动即给出
设置指引并以非 0 状态结束。
"""

from __future__ import annotations

import argparse
import secrets
import sys
from typing import Any, Callable, Iterable

from harness.config import RuntimeConfig, load_dotenv
from harness.context.builder import ContextBuilder
from harness.context.compressor import CompactionMiddleware, ContextCompressor
from harness.llm import LLMClient, LLMError, StreamEvent
from harness.loop import ReactLoop
from harness.memory.store import MemoryStore
from harness.memory.summarizer import MemorySummarizer
from harness.middleware import Middleware
from harness.runner import ConcurrentRunner, SessionJob
from harness.session.store import SessionStore
from harness.state import RuntimeState
from harness.tools.calculator import CalculatorTool
from harness.tools.read_memory import ReadMemoryTool
from harness.tools.registry import ToolRegistry
from harness.tools.search import SearchTool
from harness.tools.todo import WriteTodosTool
from harness.tools.weather import WeatherTool
from harness.trace import JsonlExporter, TraceCollector

Writer = Callable[[str], None]

_EXIT_COMMAND = "/exit"
_NEW_COMMAND = "/new"
_SWITCH_COMMAND = "/switch"
_SESSIONS_COMMAND = "/sessions"
_HISTORY_COMMAND = "/history"

from harness.renderer import DEFAULT_REASONING_PREFIX, StreamRenderer, render_event

_REASONING_PREFIX = DEFAULT_REASONING_PREFIX


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
) -> str:
    """执行 REPL 交互循环，返回最终会话 id。

    命令（/exit /new /switch /sessions /history）不进入 LLM；普通输入路由到
    loop.run（流式配置下挂 on_event 分通道渲染）；LLM 调用失败输出
    可读提示后继续消费输入，不退出。
    """
    if session_id is None:
        session_id = _generate_session_id()
        writer(f"已创建新会话 {session_id}（命令：/new 新会话、/switch <id> 切换会话、/sessions 全部会话、/history 概览、/exit 退出）")
    else:
        message_count = sum(
            1
            for record in sessions.load_records(session_id)
            if record.get("kind") == "message"
        )
        if message_count > 0:
            writer(f"已续接会话 {session_id}（{message_count} 条消息）")
        else:
            writer(f"已创建新会话 {session_id}（命令：/new 新会话、/switch <id> 切换会话、/sessions 全部会话、/history 概览、/exit 退出）")

    target_raw: Writer = raw_writer if raw_writer is not None else _default_raw_writer

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

        parts = text.split()
        if parts[0] == _SWITCH_COMMAND:
            if len(parts) != 2:
                writer(f"用法：{_SWITCH_COMMAND} <会话 id>（可用 /sessions 查看全部会话）")
                continue
            target = parts[1]
            if target not in sessions.session_ids():
                writer(f"会话 {target} 不存在，可用 /sessions 查看全部会话")
                continue
            session_id = target
            message_count = sum(
                1
                for record in sessions.load_records(target)
                if record.get("kind") == "message"
            )
            if message_count > 0:
                writer(f"已切换会话 {target}（{message_count} 条消息）")
            else:
                writer(f"已切换会话 {target}")
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
    """解析 CLI 参数：--session、--concurrent、--no-stream、--no-thinking。"""
    parser = argparse.ArgumentParser(
        prog="harness", description="最小可用 Agent Runtime（DeepSeek + ReAct）"
    )
    parser.add_argument("--session", default=None, help="以指定会话 id 启动（已存在则续接历史）")
    parser.add_argument(
        "--concurrent",
        nargs="+",
        metavar="SESSION:输入",
        default=None,
        help="并发执行多个会话任务后退出（例：--concurrent s1:查天气 s2:写周报）",
    )
    parser.add_argument("--no-stream", action="store_true", help="关闭流式输出（整段输出）")
    parser.add_argument("--no-thinking", action="store_true", help="关闭思考模式")
    return parser.parse_args(argv)


def _parse_concurrent_specs(specs: list[str]) -> list[SessionJob]:
    """解析 --concurrent 的 SESSION:输入 参数列表为 SessionJob 列表。

    只按首个冒号切分（输入可含冒号）；缺少分隔符、会话 id 或输入为
    空白均抛 ValueError（中文消息指明违规参数）。
    """
    jobs: list[SessionJob] = []
    for spec in specs:
        session_id, separator, user_input = spec.partition(":")
        if not separator:
            raise ValueError(f"参数 {spec!r} 缺少 ':' 分隔符（格式：SESSION:输入）")
        session_id = session_id.strip()
        user_input = user_input.strip()
        if not session_id:
            raise ValueError(f"参数 {spec!r} 的会话 id 不能为空")
        if not user_input:
            raise ValueError(f"参数 {spec!r} 的输入不能为空")
        jobs.append(SessionJob(session_id=session_id, user_input=user_input))
    return jobs


def run_concurrent(
    runner: Any,
    jobs: list[SessionJob],
    writer: Writer,
) -> None:
    """执行并发批次并按提交顺序渲染每个会话结果与成败汇总行。

    成功段带轮次与工具调用数；失败段输出出错提示与错误文本；末尾
    汇总成功/失败数（error 非 None 计为失败）。
    """
    results = runner.run_batch(jobs)
    for result in results:
        if result.error is not None:
            writer(f"── 会话 {result.session_id}（出错）──")
            writer(f"出错了：{result.error}")
        else:
            writer(
                f"── 会话 {result.session_id}（{result.rounds} 轮决策，"
                f"{result.tool_call_count} 次工具调用）──"
            )
            writer(result.answer)
    failed = sum(1 for result in results if result.error is not None)
    writer(
        f"并发批次完成：{len(results)} 个会话"
        f"（{len(results) - failed} 成功 / {failed} 失败）"
    )


def _build_registry(memory: MemoryStore, state: RuntimeState) -> ToolRegistry:
    """注册五个内置工具（read_memory 绑定全局记忆、write_todos 绑定会话隔离的公共状态）。"""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    registry.register(SearchTool())
    registry.register(WeatherTool())
    registry.register(ReadMemoryTool(memory))
    registry.register(WriteTodosTool(state))
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
    load_dotenv()
    args = _parse_args(argv)
    output: Writer = writer if writer is not None else _default_writer
    config = RuntimeConfig.from_env()
    if args.no_stream:
        config.stream_enabled = False
    if args.no_thinking:
        config.thinking_enabled = False

    jobs: list[SessionJob] | None = None
    if args.concurrent is not None:
        if args.session is not None:
            output("--concurrent 与 --session 不能同时使用：请仅指定其一。")
            return 2
        try:
            jobs = _parse_concurrent_specs(args.concurrent)
        except ValueError as exc:
            output(f"--concurrent 参数格式错误：{exc}")
            return 2

    try:
        llm = LLMClient(config)
    except LLMError as exc:
        output(str(exc))
        return 1

    session_id = args.session if args.session else _generate_session_id()

    sessions = SessionStore(config.data_dir)
    memory = MemoryStore(config.data_dir)
    builder = ContextBuilder(sessions, memory, config)
    state = RuntimeState()
    registry = _build_registry(memory, state)
    trace = TraceCollector(JsonlExporter(config.data_dir / "traces"))
    compressor = ContextCompressor(sessions, llm, trace, config)
    middlewares: list[Middleware] = [CompactionMiddleware(compressor)]
    loop = ReactLoop(llm, registry, sessions, builder, trace, middlewares, config)
    summarizer = MemorySummarizer(sessions, memory, llm, trace, config)
    summarizer.start()
    try:
        if jobs is not None:
            try:
                run_concurrent(ConcurrentRunner(loop, config), jobs, output)
            except ValueError as exc:
                output(f"并发批次校验失败：{exc}")
                return 2
        else:
            input_lines: Iterable[str] = (
                lines if lines is not None else sys.stdin
            )
            run_repl(
                loop,
                sessions,
                config,
                input_lines,
                output,
                session_id=session_id,
                raw_writer=_default_raw_writer,
            )
    finally:
        summarizer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
