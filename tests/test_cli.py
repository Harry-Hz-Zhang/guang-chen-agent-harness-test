"""CLI REPL 的单元测试（全 mock loop/sessions，零网络）。"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.config import RuntimeConfig
from harness.llm import LLMError, ReasoningDelta, TextDelta
from harness.loop import LoopResult


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
        {"ordinal": i, "kind": "message",
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
        loop = MagicMock()
        monkeypatch.setattr(cli, "ReactLoop", lambda *a, **k: loop)
        monkeypatch.setattr(cli, "MemorySummarizer", MagicMock())
        writer = _Writer()
        code = cli.main(["--session", "s1"], ["/exit"], writer)
        assert code != 0
        assert "DEEPSEEK_API_KEY" in writer.text
        assert loop.run.call_count == 0


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
            "ordinal": 6, "kind": "compaction",
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

    def testOnSessionChangeCallbackInvoked(self) -> None:
        """/new 切会话时通知 on_session_change 回调（todo 换绑依据）。"""
        from harness.__main__ import run_repl

        loop = MagicMock()
        loop.run.return_value = _ok_result()
        callback = MagicMock()
        writer = _Writer()
        run_repl(
            loop, _mock_sessions(), RuntimeConfig(), ["/new", "你好", "/exit"],
            writer, session_id="s1", on_session_change=callback,
        )
        callback.assert_called_once()
        new_id = callback.call_args.args[0]
        assert new_id != "s1"
        assert loop.run.call_args.args[1] == new_id

    def testRebindableTodoToolSwitchesSession(self, tmp_path: Any) -> None:
        """RebindableTodoTool 按 ref 当前会话换绑存储文件。"""
        from pathlib import Path

        from harness.__main__ import RebindableTodoTool

        ref: dict[str, str] = {"session_id": "s1"}
        tool = RebindableTodoTool(Path(tmp_path), ref)
        added = tool.execute(action="add", todo="写周报")
        assert "1" in added
        assert (tmp_path / "todos" / "s1.json").exists()
        ref["session_id"] = "s2"
        listing = tool.execute(action="list")
        assert "暂无" in listing
        assert not (tmp_path / "todos" / "s2.json").exists()
