"""harness CLI 入口 —— argparse 参数解析、组件装配与 REPL 交互循环。

REPL 内置命令：/exit 退出、/new 新会话、/switch <id> 切换已有会话、
/sessions 列出全部会话、/history 当前会话概览（命令不进入 LLM）。流式
模式下思考与正文分通道渲染（思考带「思考」前缀）；--no-stream 关闭
流式（整段输出）、--no-thinking 关闭思考模式。LLM 调用失败输出可读
提示并保持 REPL 可用；缺少 DEEPSEEK_API_KEY 时启动即给出设置指引
并以非 0 状态结束。

主线程只读输入分拣：普通输入经 SessionRouter 提交到该会话的常驻
worker 线程执行（_run_turn），输出全部过 OutputMux（全局锁 + 前台
会话原样 / 后台会话带 [xxxx] 胸牌）；/exit 硬退不 join 不等待（C2），
lines 自然耗尽时等待已入队消息跑完后返回（flush 语义）。
"""

from __future__ import annotations

import argparse
import secrets
import sys
from functools import partial
from typing import Any, Callable, Iterable

from dotenv import load_dotenv

from harness.config import RuntimeConfig
from harness.context.builder import ContextBuilder
from harness.context.compressor import CompactionMiddleware, ContextCompressor
from harness.llm import LLMClient, LLMError
from harness.loop import ReactLoop
from harness.memory.store import MemoryStore
from harness.memory.summarizer import MemorySummarizer
from harness.middleware import Middleware
from harness.outputmux import OutputMux
from harness.renderer import DEFAULT_REASONING_PREFIX, StreamRenderer
from harness.session.store import SessionStore
from harness.sessionworker import SessionRouter
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


def _build_router(
    loop: ReactLoop,
    config: RuntimeConfig,
    writer: Writer,
    raw_writer: Writer,
) -> SessionRouter:
    """装配默认链路：OutputMux + partial(_run_turn) + SessionRouter。"""
    mux = OutputMux(writer=writer, raw_writer=raw_writer)
    runner = partial(_run_turn, loop=loop, config=config)
    return SessionRouter(runner=runner, config=config, mux=mux)


def _run_turn(
    text: str,
    session_id: str,
    writer: Writer,
    raw_writer: Writer,
    loop: ReactLoop,
    config: RuntimeConfig,
) -> None:
    """在 worker 线程里跑一整轮对话（渲染器每回合独立，禁共享）。

    由 SessionRouter 经 partial 绑定 loop/config 后作为 runner 注入；
    LLMError 沿用「出错了：」文案（经 _run_turn 捕获）；其余异常不在本层拦
    （由 SessionWorker 兜底「该会话处理失败」）。
    """
    renderer = StreamRenderer(raw_writer=raw_writer, reasoning_prefix=_REASONING_PREFIX)
    try:
        result = loop.run(
            text,
            session_id,
            on_event=renderer.render if config.stream_enabled else None,
        )
    except LLMError as exc:
        writer(f"出错了：{exc}")
        return
    if not config.stream_enabled or result.truncated:
        writer(result.answer)
    else:
        renderer.finalize()


def run_repl(
    loop: ReactLoop,
    sessions: Any,
    config: RuntimeConfig,
    lines: Iterable[str],
    writer: Writer,
    session_id: str | None = None,
    raw_writer: Writer | None = None,
    router: SessionRouter | None = None,
) -> str:
    """执行 REPL 交互循环，返回最终会话 id。

    命令（/exit /new /switch /sessions /history）不进入 LLM；普通输入经
    router.submit 路由到该会话的 worker 线程执行 _run_turn（流式配置下
    每回合独立渲染器）。LLM 调用失败输出可读提示后继续消费输入，不退出。

    退出路径二分：/exit 硬退（不 flush、不 join，C2）；lines 自然耗尽
    （EOF / 测试注入完毕）时等待所有已入队消息跑完后返回（flush 语义，
    供测试断言最终一致性）。
    """
    if session_id is None:
        session_id = _generate_session_id()
        writer(
            f"已创建新会话 {session_id}（命令：/new 新会话、/switch <id> 切换会话、/sessions 全部会话、/history 概览、/exit 退出）"
        )
    else:
        message_count = sum(
            1
            for record in sessions.load_records(session_id)
            if record.get("kind") == "message"
        )
        if message_count > 0:
            writer(f"已续接会话 {session_id}（{message_count} 条消息）")
        else:
            writer(
                f"已创建新会话 {session_id}（命令：/new 新会话、/switch <id> 切换会话、/sessions 全部会话、/history 概览、/exit 退出）"
            )

    target_raw: Writer = raw_writer if raw_writer is not None else _default_raw_writer

    if router is None:
        # 自建默认链路，供无输入口路径（旧用例/非交互复用）使用
        router = _build_router(loop, config, writer, target_raw)

    line_out = router.mux_line()
    writer = line_out  # 命令提示统一过 mux 锁（session_id=None 恒前台）

    router.switch(session_id)
    hard_exit = False

    for line in lines:
        text = line.strip()
        if not text:
            continue

        if text == _EXIT_COMMAND:
            hard_exit = True
            break

        if text == _NEW_COMMAND:
            session_id = _generate_session_id()
            router.switch(session_id)
            writer(f"已创建新会话 {session_id}")
            continue

        if text == _SESSIONS_COMMAND:
            ids = sessions.session_ids()
            if ids:
                # 在跑会话加「跑着中」标记（design §2.1 busy 用途兑现）
                busy_ids = router.busy_sessions()
                marked = [f"{sid}（跑着中）" if sid in busy_ids else sid for sid in ids]
                writer("全部会话：" + "、".join(marked))
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
                writer(
                    f"当前会话 {session_id}：{msg_count} 条消息，累计 {rounds} 轮对话"
                )
            continue

        parts = text.split()
        if parts[0] == _SWITCH_COMMAND:
            if len(parts) != 2:
                writer(
                    f"用法：{_SWITCH_COMMAND} <会话 id>（可用 /sessions 查看全部会话）"
                )
                continue
            target = parts[1]
            if target not in sessions.session_ids():
                writer(f"会话 {target} 不存在，可用 /sessions 查看全部会话")
                continue
            session_id = target
            router.switch(target)
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

        outcome = router.submit(text, session_id)
        if not outcome.accepted:
            writer(outcome.reason or "该会话暂不可用，请稍后再试")

    if hard_exit:
        # C2：硬退——不 join、不等待任何 worker，随进程终止
        return session_id

    # flush 语义：lines 自然耗尽时，收尾等待已入队消息全部跑完再返回
    router.close_all()
    return session_id


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析 CLI 参数：--session、--no-stream、--no-thinking。"""
    parser = argparse.ArgumentParser(
        prog="harness", description="最小可用 Agent Runtime（DeepSeek + ReAct）"
    )
    parser.add_argument(
        "--session", default=None, help="以指定会话 id 启动（已存在则续接历史）"
    )
    parser.add_argument(
        "--no-stream", action="store_true", help="关闭流式输出（整段输出）"
    )
    parser.add_argument("--no-thinking", action="store_true", help="关闭思考模式")
    return parser.parse_args(argv)


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
        input_lines: Iterable[str] = lines if lines is not None else sys.stdin
        raw_output: Writer = _default_raw_writer
        router = _build_router(loop, config, output, raw_output)
        run_repl(
            loop,
            sessions,
            config,
            input_lines,
            output,
            session_id=session_id,
            raw_writer=raw_output,
            router=router,
        )
    finally:
        summarizer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
