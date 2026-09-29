"""CLI REPL 的单元测试（全 mock loop/sessions，零网络）。"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.config import RuntimeConfig
from harness.llm import LLMError, ReasoningDelta, TextDelta
from harness.loop import LoopResult


class TestRegistryAssembly:

    def testRegistryIncludesReadMemory(self, tmp_path: Path) -> None:
        """_build_registry 注册 read_memory（绑定全局记忆存储）及 calculator / search / weather。"""
        from harness.__main__ import _build_registry
        from harness.memory.store import MemoryStore

        registry = _build_registry(MemoryStore(tmp_path))
        names = registry.names()
        assert "read_memory" in names
        assert "calculator" in names
        assert "search" in names
        assert "weather" in names
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
        {"ts": "t", "ordinal": i, "kind": "message",
         "message": {"role": "user", "content": f"m{i}"}}
        for i in range(count)
    ]


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
            loop, _mock_sessions([]), RuntimeConfig(), ["/exit"], writer, session_id=None
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
        run_repl(
            loop, sessions, RuntimeConfig(), ["/new", "你好", "/exit"], writer,
            session_id="s1",
        )
        assert loop.run.call_count == 1
        assert loop.run.call_args.args[1] != "s1"
        assert "新会话" in writer.text

    def testCommandSessions(self) -> None:
        """命令 /sessions：输出会话列表，loop.run 0 次调用。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        sessions = _mock_sessions()
        sessions.session_ids.return_value = ["s1", "s2"]
        writer = _Writer()
        run_repl(loop, sessions, RuntimeConfig(), ["/sessions", "/exit"], writer,
                 session_id="s1")
        assert loop.run.call_count == 0
        assert "s1" in writer.text
        assert "s2" in writer.text

    def testCommandHistory(self) -> None:
        """命令 /history：输出含当前会话轮次信息。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        sessions = _mock_sessions(_message_records(6))
        sessions.message_rounds.return_value = 3
        writer = _Writer()
        run_repl(loop, sessions, RuntimeConfig(), ["/history", "/exit"], writer,
                 session_id="s1")
        assert loop.run.call_count == 0
        assert "3" in writer.text

    def testUserInputRoutesToLoop(self) -> None:
        """普通输入路由到 loop.run，入参 user_input 精确一致。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        run_repl(loop, _mock_sessions(), RuntimeConfig(), ["你好", "/exit"], writer,
                 session_id="s1")
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
        run_repl(loop, _mock_sessions(), config, ["你好", "/exit"], writer,
                 session_id="s1")
        assert loop.run.call_args.kwargs.get("on_event") is None

    def testStreamFlagPassesCallback(self) -> None:
        """stream_enabled=True 时 loop.run 收到可用的 on_event 回调。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        run_repl(loop, _mock_sessions(), RuntimeConfig(), ["你好", "/exit"], writer,
                 session_id="s1")
        on_event = loop.run.call_args.kwargs.get("on_event")
        assert callable(on_event)

    def testLlmErrorRecoverable(self) -> None:
        """LLM 调用失败：REPL 输出错误提示后继续消费输入，不崩溃。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.side_effect = [LLMError("超时"), _ok_result()]
        writer = _Writer()
        run_repl(
            loop, _mock_sessions(), RuntimeConfig(), ["你好", "再来", "/exit"],
            writer, session_id="s1",
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


class TestRender:

    def testStreamEventRendering(self) -> None:
        """思考与正文分通道渲染：思考行含前缀，正文行不含。"""
        from harness.__main__ import render_event

        reasoning = render_event(ReasoningDelta(text="思"))
        assert "思" in reasoning
        assert "思考" in reasoning
        text = render_event(TextDelta(text="答"))
        assert "答" in text
        assert "思考" not in text


class TestStreamRendering:

    def testStreamDeltasRawOutput(self) -> None:
        """流式分片走 raw 出口：前缀只在思考段首出现、分片间无换行。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        writer = _Writer()
        raw = _Writer()
        run_repl(
            loop, _mock_sessions(), RuntimeConfig(), ["你好", "/exit"], writer,
            session_id="s1", raw_writer=raw,
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
        run_repl(
            loop, _mock_sessions(), RuntimeConfig(), ["你好", "/exit"], writer,
            session_id="s1", raw_writer=raw,
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
        run_repl(
            loop, _mock_sessions(), RuntimeConfig(), ["第一问", "第二问", "/exit"],
            writer, session_id="s1", raw_writer=raw,
        )
        assert raw.lines == [
            "思考｜", "思", "\n", "答", "\n",
            "思考｜", "思", "\n", "答", "\n",
        ]


class TestSessionManagement:

    def testHistoryIncludesMessagesAndCompaction(self) -> None:
        """/history 概览含消息数、轮次与最近压缩状态。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        records = _message_records(6)
        records.append({
            "ts": "t", "ordinal": 6, "kind": "compaction",
            "compressed_up_to": 5, "summary": "旧摘要", "summary_model": "m",
        })
        sessions = _mock_sessions(records)
        sessions.message_rounds.return_value = 3
        writer = _Writer()
        run_repl(loop, sessions, RuntimeConfig(), ["/history", "/exit"], writer,
                 session_id="s1")
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
        run_repl(
            loop, sessions, RuntimeConfig(), ["/switch s2", "你好", "/exit"],
            writer, session_id="s1",
        )
        assert "已切换会话 s2" in writer.text
        assert "6 条消息" in writer.text
        assert loop.run.call_args.args[1] == "s2"

    def testCommandSwitchUnknownSessionKeepsCurrent(self) -> None:
        """切换到不存在的会话：输出不存在提示，当前会话不变且 REPL 存活。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1"]
        writer = _Writer()
        run_repl(
            loop, sessions, RuntimeConfig(), ["/switch nope", "你好", "/exit"],
            writer, session_id="s1",
        )
        assert "不存在" in writer.text
        assert "nope" in writer.text
        assert loop.run.call_count == 1
        assert loop.run.call_args.args[1] == "s1"

    def testCommandSwitchMissingArgUsage(self) -> None:
        """/switch 缺参数：输出用法提示，当前会话不变。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1"]
        writer = _Writer()
        run_repl(
            loop, sessions, RuntimeConfig(), ["/switch", "你好", "/exit"],
            writer, session_id="s1",
        )
        assert "用法" in writer.text
        assert "/switch" in writer.text
        assert loop.run.call_count == 1
        assert loop.run.call_args.args[1] == "s1"

    def testCommandSwitchExtraArgUsage(self) -> None:
        """/switch 多余参数：输出用法提示，不进入 LLM。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1"]
        writer = _Writer()
        run_repl(
            loop, sessions, RuntimeConfig(), ["/switch a b", "/exit"],
            writer, session_id="s1",
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
        run_repl(
            loop, sessions, RuntimeConfig(), ["/switch s1", "你好", "/exit"],
            writer, session_id="s1",
        )
        assert "已切换会话 s1" in writer.text
        assert loop.run.call_args.args[1] == "s1"

    def testCommandSwitchNotRoutedToLoop(self) -> None:
        """切换命令本身不进入 LLM：loop.run 0 次调用。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        sessions = _mock_sessions(_message_records(2))
        sessions.session_ids.return_value = ["s1", "s2"]
        writer = _Writer()
        run_repl(
            loop, sessions, RuntimeConfig(), ["/switch s2", "/exit"],
            writer, session_id="s1",
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
            loop, sessions, RuntimeConfig(), ["/switch s2", "/exit"],
            writer, session_id="s1",
        )
        switch_lines = [ln for ln in writer.lines if "已切换会话 s2" in ln]
        assert switch_lines == ["已切换会话 s2"]
