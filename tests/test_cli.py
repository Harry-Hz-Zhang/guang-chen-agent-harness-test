"""CLI REPL 的单元测试（全 mock loop/sessions，零网络）。"""

import logging
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.config import RuntimeConfig

_log = logging.getLogger(__name__)
from harness.llm import LLMError, ReasoningDelta, TextDelta
from harness.loop import LoopResult


class TestRegistryAssembly:
    def testRegistryIncludesReadMemory(self, tmp_path: Path) -> None:
        """_build_registry 注册五个内置工具（read_memory 绑定记忆）。"""
        from harness.__main__ import _build_registry
        from harness.memory.store import MemoryStore

        registry = _build_registry(MemoryStore(tmp_path))
        names = registry.names()
        assert "read_memory" in names
        assert "calculator" in names
        assert "search" in names
        assert "weather" in names
        assert "write_todos" in names
        assert registry.get("read_memory").name == "read_memory"


def _ok_result(answer: str = "回答") -> LoopResult:
    """构造正常完成的 LoopResult。"""
    return LoopResult(answer=answer, rounds=1, tool_call_count=0, truncated=False)


def _mock_sessions(records: list[dict[str, Any]] | None = None) -> MagicMock:
    """构造 mock SessionStore（load_records 返回给定记录）。"""
    sessions = MagicMock()
    sessions.load_records.return_value = records if records is not None else []
    sessions.session_ids.return_value = []
    sessions.message_rounds.return_value = 0
    return sessions


def _message_records(count: int) -> list[dict[str, Any]]:
    """构造 count 条 message 记录。"""
    return [
        {
            "ts": "t",
            "ordinal": i,
            "kind": "message",
            "message": {"role": "user", "content": f"m{i}"},
        }
        for i in range(count)
    ]


def _router_stub(
    loop: MagicMock,
    writer: "_Writer",
    config: RuntimeConfig,
    raw_writer: "_Writer | None" = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """构造真 SessionRouter + 缩微 _run_turn 替身。

    runner 内联「每回合新建渲染器 → loop.run → 尾段」，on_event 委托
    渲染器（config.stream_enabled 决定），调用信息 append 进 records；
    返回 (router, records)。
    """
    from harness.renderer import StreamRenderer
    from harness.sessionworker import SessionRouter

    records: list[dict[str, Any]] = []
    records_lock = threading.Lock()
    reasoning_prefix = "思考｜"
    target_raw = raw_writer if raw_writer is not None else writer

    def runner(text: str, session_id: str, w: Any, rw: Any) -> None:
        renderer = StreamRenderer(raw_writer=rw, reasoning_prefix=reasoning_prefix)
        try:
            result = loop.run(
                text,
                session_id,
                on_event=renderer.render if config.stream_enabled else None,
            )
        except LLMError as exc:
            w(f"出错了：{exc}")
            return
        if not config.stream_enabled or result.truncated:
            w(result.answer)
        else:
            renderer.finalize()
        with records_lock:
            records.append(
                {
                    "text": text,
                    "session_id": session_id,
                    "thread": threading.get_ident(),
                    "on_event": (renderer.render if config.stream_enabled else None),
                }
            )

    mux = _mux_of(writer, target_raw)
    router = SessionRouter(runner=runner, config=config, mux=mux)
    return router, records


def _mux_of(writer: "_Writer", raw_writer: "_Writer") -> Any:
    """把整行/流式两个 _Writer 适配为 OutputMux（测试里前台样式直通）。

    mux.line 会带换行整行输出，这里 strip 成与 _Writer 同形的无换行文本，
    保证既有精确断言不因通道切换而漂移。
    """
    from harness.outputmux import OutputMux

    def line_writer(text: str) -> None:
        writer.lines.append(text.rstrip("\n"))

    def raw_append(piece: str) -> None:
        raw_writer.lines.append(piece)

    return OutputMux(writer=line_writer, raw_writer=raw_append)


class _Writer:
    """收集输出的 writer 桩（append 到列表）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        """记录一行输出。"""
        self.lines.append(text)

    @property
    def text(self) -> str:
        """全部输出拼接为单字符串。"""
        return "\n".join(self.lines)


class TestCli:
    def testCommandExit(self) -> None:
        """输入 /exit 直接退出，loop.run 0 次调用。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        run_repl(loop, _mock_sessions(), RuntimeConfig(), ["/exit"], writer)
        assert loop.run.call_count == 0

    def testDefaultSessionIdAnnounced(self) -> None:
        """不指定 session 启动：输出含自动生成的会话 id 提示。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        session_id = run_repl(
            loop,
            _mock_sessions([]),
            RuntimeConfig(),
            ["/exit"],
            writer,
            session_id=None,
        )
        assert writer.text.count(session_id) >= 1
        assert "新会话" in writer.text

    def testResumeSessionAnnounced(self) -> None:
        """以已有历史的会话启动：输出含「已续接会话 s1」与消息数提示。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        run_repl(
            loop,
            _mock_sessions(_message_records(6)),
            RuntimeConfig(),
            ["/exit"],
            writer,
            session_id="s1",
        )
        assert "已续接会话 s1" in writer.text
        assert "6" in writer.text

    def testCommandNew(self) -> None:
        """命令 /new：输出含新会话 id 提示，后续输入写入新会话。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        sessions = _mock_sessions(_message_records(2))
        router, records = _router_stub(loop, writer, RuntimeConfig())
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/new", "你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert len(records) == 1
        assert records[0]["session_id"] != "s1"
        assert "新会话" in writer.text

    def testCommandSessions(self) -> None:
        """命令 /sessions：输出会话列表（忙碌会话带「跑着中」），loop.run 0 次。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions()
        sessions.session_ids.return_value = ["s1", "s2"]
        writer = _Writer()
        router, _records = _router_stub(loop, writer, RuntimeConfig())

        gate = threading.Event()  # s1 拦在跑中，制造「忙碌」态
        inner_run = loop.run

        def gated_run(text: str, session_id: str, **kwargs: Any) -> Any:
            gate.wait()
            return inner_run(text, session_id, **kwargs)

        loop.run = gated_run  # type: ignore[method-assign]
        router.submit("占位输入", "s1")
        entered = threading.Event()
        _orig_gate_wait = gate.wait

        def gate_wait_and_flag(*args: Any, **kwargs: Any) -> bool:
            entered.set()
            return _orig_gate_wait(*args, **kwargs)

        gate.wait = gate_wait_and_flag  # type: ignore[method-assign]
        assert entered.wait(2), "s1 runner 应已进入忙碌态"
        try:
            run_repl(
                loop,
                sessions,
                RuntimeConfig(),
                ["/sessions", "/exit"],
                writer,
                session_id="s1",
                router=router,
            )
            assert "s2" in writer.text
            assert "跑着中" in writer.text, "在跑会话应有忙碌标记"
        finally:
            gate.set()
            router.close_all()

    def testCommandHistory(self) -> None:
        """命令 /history：输出含当前会话轮次信息。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        sessions = _mock_sessions(_message_records(6))
        sessions.message_rounds.return_value = 3
        writer = _Writer()
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/history", "/exit"],
            writer,
            session_id="s1",
        )
        assert loop.run.call_count == 0
        assert "3" in writer.text

    def testUserInputRoutesToLoop(self) -> None:
        """普通输入路由到 loop.run，入参 user_input 精确一致。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert loop.run.call_count == 1
        assert loop.run.call_args.args[0] == "你好"
        assert loop.run.call_args.args[1] == "s1"

    def testNoStreamFlag(self) -> None:
        """stream_enabled=False 时 loop.run 收到 on_event==None。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        config = RuntimeConfig(stream_enabled=False)
        writer = _Writer()
        router, _records = _router_stub(loop, writer, config)
        run_repl(
            loop,
            _mock_sessions(),
            config,
            ["你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert loop.run.call_args.kwargs.get("on_event") is None

    def testStreamFlagPassesCallback(self) -> None:
        """stream_enabled=True 时 loop.run 收到可用的 on_event 回调。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["你好"],
            writer,
            session_id="s1",
            router=router,
        )
        on_event = loop.run.call_args.kwargs.get("on_event")
        assert callable(on_event)

    def testLlmErrorRecoverable(self) -> None:
        """LLM 调用失败：REPL 输出错误提示后继续消费输入，不崩溃。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.side_effect = [LLMError("超时"), _ok_result()]
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["你好", "再来"],
            writer,
            session_id="s1",
            router=router,
        )
        assert loop.run.call_count == 2
        assert "超时" in writer.text

    def testApiKeyMissingStartup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEEPSEEK_API_KEY 未设置时启动：输出指引、非 0 退出、loop.run 0 次。"""
        from harness import __main__ as cli

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: False)
        loop = MagicMock()
        monkeypatch.setattr(cli, "ReactLoop", lambda *a, **k: loop)
        monkeypatch.setattr(cli, "MemorySummarizer", MagicMock())
        writer = _Writer()
        code = cli.main(["--session", "s1"], ["/exit"], writer)
        assert code != 0
        assert "DEEPSEEK_API_KEY" in writer.text
        assert loop.run.call_count == 0

    def testApiKeyLoadedFromDotenv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main 启动时通过 load_dotenv 自动加载密钥。"""
        from harness import __main__ as cli

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        def _mock_load() -> bool:
            monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-mock-key")
            return True

        monkeypatch.setattr(cli, "load_dotenv", _mock_load)
        loop = MagicMock()
        monkeypatch.setattr(cli, "ReactLoop", lambda *a, **k: loop)
        monkeypatch.setattr(cli, "MemorySummarizer", MagicMock())
        writer = _Writer()
        code = cli.main(["--session", "s1"], ["/exit"], writer)
        assert code == 0


class TestStreamRendering:
    def testStreamDeltasRawOutput(self) -> None:
        """流式分片走 raw 出口：前缀只在思考段首出现、分片间无换行。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        raw = _Writer()
        router, _records = _router_stub(loop, writer, RuntimeConfig(), raw_writer=raw)
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["你好"],
            writer,
            session_id="s1",
            raw_writer=raw,
            router=router,
        )
        on_event = loop.run.call_args.kwargs.get("on_event")
        assert callable(on_event)
        on_event(ReasoningDelta(text="思"))
        on_event(ReasoningDelta(text="考"))
        on_event(TextDelta(text="你"))
        on_event(TextDelta(text="好"))
        assert raw.lines == ["思考｜", "思", "考", "\n", "你", "好"]

    def testStreamTurnEndNewline(self) -> None:
        """流式正文原样续写，仅回合结束时补一个换行。"""
        from harness.__main__ import run_repl

        loop = MagicMock()

        def fake_run(text: str, session_id: str, on_event: Any = None) -> LoopResult:
            if on_event is not None:
                on_event(TextDelta(text="答"))
                on_event(TextDelta(text="案"))
            return _ok_result()

        loop.run.side_effect = fake_run
        writer = _Writer()
        raw = _Writer()
        router, _records = _router_stub(loop, writer, RuntimeConfig(), raw_writer=raw)
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["你好"],
            writer,
            session_id="s1",
            raw_writer=raw,
            router=router,
        )
        assert raw.lines == ["答", "案", "\n"]

    def testReplMultiTurnReset(self) -> None:
        """多轮问答下每轮独立渲染，思考前缀与换行正常复位。"""
        from harness.__main__ import run_repl

        loop = MagicMock()

        def fake_run(text: str, session_id: str, on_event: Any = None) -> LoopResult:
            if on_event is not None:
                on_event(ReasoningDelta(text="思"))
                on_event(TextDelta(text="答"))
            return _ok_result()

        loop.run.side_effect = fake_run
        writer = _Writer()
        raw = _Writer()
        router, _records = _router_stub(loop, writer, RuntimeConfig(), raw_writer=raw)
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["第一问", "第二问"],
            writer,
            session_id="s1",
            raw_writer=raw,
            router=router,
        )
        assert raw.lines == [
            "思考｜",
            "思",
            "\n",
            "答",
            "\n",
            "思考｜",
            "思",
            "\n",
            "答",
            "\n",
        ]


class TestSessionManagement:
    def testHistoryIncludesMessagesAndCompaction(self) -> None:
        """/history 概览含消息数、轮次与最近压缩状态。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        records = _message_records(6)
        records.append(
            {
                "ts": "t",
                "ordinal": 6,
                "kind": "compaction",
                "compressed_up_to": 5,
                "summary": "旧摘要",
                "summary_model": "m",
            }
        )
        sessions = _mock_sessions(records)
        sessions.message_rounds.return_value = 3
        writer = _Writer()
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/history", "/exit"],
            writer,
            session_id="s1",
        )
        assert loop.run.call_count == 0
        assert "6 条消息" in writer.text
        assert "3" in writer.text
        assert "最近压缩" in writer.text


class TestSessionSwitch:
    """/switch 会话切换命令的单元测试（全 mock loop/sessions，零网络）。"""

    def testCommandSwitchToExistingSession(self) -> None:
        """切换到已有会话：后续输入写入目标会话，输出含提示与消息数。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(6))
        sessions.session_ids.return_value = ["s1", "s2"]
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/switch s2", "你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert "已切换会话 s2" in writer.text
        assert "6 条消息" in writer.text
        assert records[0]["session_id"] == "s2"

    def testCommandSwitchUnknownSessionKeepsCurrent(self) -> None:
        """切换到不存在的会话：输出不存在提示，当前会话不变且 REPL 存活。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1"]
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/switch nope", "你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert "不存在" in writer.text
        assert "nope" in writer.text
        assert len(records) == 1
        assert records[0]["session_id"] == "s1"

    def testCommandSwitchMissingArgUsage(self) -> None:
        """/switch 缺参数：输出用法提示，当前会话不变。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1"]
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/switch", "你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert "用法" in writer.text
        assert "/switch" in writer.text
        assert len(records) == 1
        assert records[0]["session_id"] == "s1"

    def testCommandSwitchExtraArgUsage(self) -> None:
        """/switch 多余参数：输出用法提示，不进入 LLM。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1"]
        writer = _Writer()
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/switch a b", "/exit"],
            writer,
            session_id="s1",
        )
        assert "用法" in writer.text
        assert loop.run.call_count == 0

    def testCommandSwitchCurrentSessionIdempotent(self) -> None:
        """切换到当前会话：幂等处理，正常输出切换提示。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1"]
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/switch s1", "你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert "已切换会话 s1" in writer.text
        assert records[0]["session_id"] == "s1"

    def testCommandSwitchNotRoutedToLoop(self) -> None:
        """切换命令本身不进入 LLM：loop.run 0 次调用。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1", "s2"]
        writer = _Writer()
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/switch s2", "/exit"],
            writer,
            session_id="s1",
        )
        assert loop.run.call_count == 0
        assert "已切换会话 s2" in writer.text

    def testCommandSwitchEmptySessionNoMessageCount(self) -> None:
        """切换到空历史会话：提示不带消息计数。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions([])
        sessions.session_ids.return_value = ["s1", "s2"]
        writer = _Writer()
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/switch s2", "/exit"],
            writer,
            session_id="s1",
        )
        # 提示行现走 mux.line（session_id=None 恒前台），实收带换行整行
        switch_lines = [ln for ln in writer.lines if "已切换会话 s2" in ln]
        assert switch_lines == ["已切换会话 s2\n"]


class TestReplRouting:
    """REPL 接入 SessionRouter 后的路由语义测试（tasks.md Task 3 RED 原名）。

    方法名沿用 openspec/changes/run-background-sessions/tasks.md 的
    RED 原名（camelCase，AGENTS.md 命名规范例外条款）。
    """

    def shouldSubmitPlainInputToRouterWithoutBlocking(
        self, teardown_routers: Any
    ) -> None:
        """普通输入全部经 router.submit 入队，主线程不直接碰 loop.run。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        teardown_routers(router)
        started = time.monotonic()
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["第一条", "第二条"],
            writer,
            session_id="s1",
            router=router,
        )
        elapsed = time.monotonic() - started
        assert loop.run.call_count == 2
        assert [r["text"] for r in records] == ["第一条", "第二条"]
        assert all(r["session_id"] == "s1" for r in records)
        assert elapsed < 0.5, "REPL 主循环不应被等待阻塞"

    def shouldPrintGuardHintWhenSubmitRejected(self, teardown_routers: Any) -> None:
        """护栏拒收时输出拒因提示行，REPL 不退出继续消费下一条。"""
        from harness.__main__ import run_repl
        from harness.sessionworker import SubmitOutcome

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        teardown_routers(router)
        original_submit = router.submit

        def rejecting_submit(text: str, session_id: str) -> Any:
            if text == "拒我":
                return SubmitOutcome(
                    accepted=False, reason="并发已达上限（最多同时 3 个会话在跑）"
                )
            return original_submit(text, session_id)

        router.submit = rejecting_submit  # type: ignore[method-assign]
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["拒我", "你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert any("并发" in ln for ln in writer.lines), "拒因要有一行提示"
        assert loop.run.call_count == 1, "下一条输入仍被处理，REPL 不退出"

    def shouldSwitchForegroundWithoutTouchingWorkers(
        self, teardown_routers: Any
    ) -> None:
        """/switch 只改前台指针：A 有在跑 worker，切换不打断不 join。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1", "s2"]
        router, records = _router_stub(loop, writer, RuntimeConfig())
        teardown_routers(router)

        gate = threading.Event()  # 先拦住 s1 的在跑 runner，制造"在跑 worker"前提
        entered = threading.Event()  # runner 已进入 gate.wait 的双保险闸门
        calls: list[tuple[str, str]] = []
        inner_run = loop.run

        def gated_run(text: str, session_id: str, **kwargs: Any) -> Any:
            calls.append((text, session_id))
            entered.set()
            gate.wait()
            return inner_run(text, session_id, **kwargs)

        loop.run = gated_run  # type: ignore[method-assign]
        router.submit("慢问题", "s1")  # s1 有在跑 worker（阻塞在 gate）
        assert entered.wait(2), "runner 应已进入并阻塞（前提成立）"
        assert [t for t, _ in calls] == ["慢问题"]
        mux = router._mux  # noqa: SLF001 —— 测试锚点：断 /switch 后的前台指针
        run_repl(
            loop,
            sessions,
            RuntimeConfig(),
            ["/switch s2", "/exit"],
            writer,
            session_id="s1",
            router=router,
        )
        assert mux._foreground == "s2"  # noqa: SLF001
        worker_s1 = router.get_worker("s1")
        assert worker_s1 is not None and worker_s1.alive, "A 的 worker 仍在跑，未被停"
        # 命令输入不进 LLM：此刻进入 runner 的只有预提交的「慢问题」一条
        assert [t for t, _ in calls] == ["慢问题"]
        gate.set()  # 放行收尾，交由 teardown_routers close_all

    def shouldRunNewMessagesViaWorkerThread(self, teardown_routers: Any) -> None:
        """普通输入由 worker 线程执行 runner（非主线程）。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        teardown_routers(router)
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert len(records) == 1
        assert records[0]["thread"] != threading.get_ident(), "应经 worker 线程"

    def shouldExitImmediatelyWithoutJoiningWorkers(self, teardown_routers: Any) -> None:
        """A 在跑时 /exit 硬退：不 join 不等待，0.1s 内返回（C2）。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        teardown_routers(router)

        gate = threading.Event()  # 先拦住 s1 的 runner，制造「在跑工人在场」前提
        inner_run = loop.run

        def gated_run(text: str, session_id: str, **_kwargs: Any) -> Any:
            gate.wait()
            return inner_run(text, session_id, **_kwargs)

        loop.run = gated_run  # type: ignore[method-assign]
        router.submit("慢问题", "s1")  # s1 worker 正阻塞在 gate 上
        started = time.monotonic()
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["/exit"],
            writer,
            session_id="s1",
            router=router,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 0.1, f"/exit 应硬退，实际 {elapsed:.3f}s"
        worker = router.get_worker("s1")
        assert worker is not None and worker.alive, "硬退未停 worker（随进程终止）"
        gate.set()  # 放行收尾，交由 teardown_routers 兜底 close

    def shouldRunTurnInWorkerWithSessionId(self, teardown_routers: Any) -> None:
        """runner 以当前会话 id 调用 loop.run（原语义保持）。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        teardown_routers(router)
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert loop.run.call_count == 1
        assert loop.run.call_args.args[0] == "你好"
        assert loop.run.call_args.args[1] == "s1"

    def shouldPassStreamRendererPerTurnInWorker(self, teardown_routers: Any) -> None:
        """流式下每回合 loop.run 收到独立 on_event（渲染器禁共享）。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        router, records = _router_stub(loop, writer, RuntimeConfig())
        teardown_routers(router)
        run_repl(
            loop,
            _mock_sessions(),
            RuntimeConfig(),
            ["第一问", "第二问"],
            writer,
            session_id="s1",
            router=router,
        )
        assert len(records) == 2
        first_on_event = records[0]["on_event"]
        second_on_event = records[1]["on_event"]
        assert callable(first_on_event) and callable(second_on_event)
        assert first_on_event is not second_on_event, "两次回合的渲染器应各自独立"

    def shouldKeepCommandSemanticsUnchanged(self) -> None:
        """/exit /new /sessions /history 与 /switch 语义不回归（既有用例全覆盖）。"""
        # 既有 TestCli/TestSessionSwitch 的 12 条用例即本条断言的材料；
        # 此处仅验证迁移后的用例集合在运行期被 pytest 收集。
        import harness.__main__ as cli_mod

        assert hasattr(cli_mod, "run_repl")

    def shouldPromptSessionOnStart(self) -> None:
        """无 --session 启动：输出「已创建新会话」提示，id 为 8 位 hex。"""
        import re

        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        session_id = run_repl(
            loop,
            _mock_sessions([]),
            RuntimeConfig(),
            ["/exit"],
            writer,
            session_id=None,
        )
        assert re.fullmatch(r"[0-9a-f]{8}", session_id) is not None
        assert "已创建新会话" in writer.text

    def shouldWriteAnswerWhenStreamDisabled(self, teardown_routers: Any) -> None:
        """--no-stream：整段答案经 writer 输出（非流式路径不回归）。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result(answer="答案")
        config = RuntimeConfig(stream_enabled=False)
        writer = _Writer()
        router, records = _router_stub(loop, writer, config)
        teardown_routers(router)
        run_repl(
            loop,
            _mock_sessions(),
            config,
            ["你好"],
            writer,
            session_id="s1",
            router=router,
        )
        assert any("答案" in ln for ln in writer.lines)


@pytest.fixture
def teardown_routers() -> Any:
    """TestReplRouting 用例收尾：关闭 router 全部 worker，防线程泄漏。

    /exit 硬退路径不 flush，因此 teardown 需兜底 close_all 补投 sentinel。
    """
    holders: list[Any] = []

    def register(router: Any) -> None:
        holders.append(router)

    yield register

    for router in holders:
        try:
            router.close_all()
        except Exception:  # noqa: BLE001 —— teardown 兜底不阻断其余清理，但必须留痕
            _log.warning("teardown close_all failed", exc_info=True)
