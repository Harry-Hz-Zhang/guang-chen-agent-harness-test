"""SessionStore 的 TDD 测试 —— 对应 tasks.md Task 4 的 7 条 RED 与 review 修复 4 条。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from harness.session.store import SessionStore

COMPACTION_SUMMARY_NAME = "__compaction_summary__"


def _msg(role: str, content: str) -> dict:
    """构造最小 OpenAI 消息 dict。"""
    return {"role": role, "content": content}


class TestSessionStore:
    def testAppendAndLoad(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        for i in range(3):
            store.append_message("s1", _msg("user" if i % 2 == 0 else "assistant", f"m{i}"))
        records = store.load_records("s1")
        assert len(records) == 3
        assert [r["ordinal"] for r in records] == [0, 1, 2]
        lines = (tmp_path / "sessions" / "s1.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        for line in lines:
            assert isinstance(json.loads(line), dict)

    def testIsolation(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        store.append_message("s1", _msg("user", "消息A"))
        store.append_message("s2", _msg("user", "消息B"))
        s1_text = (tmp_path / "sessions" / "s1.jsonl").read_text(encoding="utf-8")
        s2_text = (tmp_path / "sessions" / "s2.jsonl").read_text(encoding="utf-8")
        assert "消息A" in s1_text and "消息B" not in s1_text
        assert "消息B" in s2_text and "消息A" not in s2_text
        s1_contents = [
            r["message"]["content"]
            for r in store.load_records("s1")
            if r["kind"] == "message"
        ]
        s2_contents = [
            r["message"]["content"]
            for r in store.load_records("s2")
            if r["kind"] == "message"
        ]
        assert s1_contents == ["消息A"]
        assert s2_contents == ["消息B"]

    def testPersistAcrossInstances(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        store.append_message("s1", _msg("user", "第一条"))
        store.append_message("s1", _msg("assistant", "第二条"))
        restarted = SessionStore(tmp_path)
        records = restarted.load_records("s1")
        assert len(records) == 2
        contents = [
            r["message"]["content"] for r in records if r["kind"] == "message"
        ]
        assert contents == ["第一条", "第二条"]

    def testAppendCompactionAndReadContext(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        for i in range(5):
            store.append_message("s1", _msg("user" if i % 2 == 0 else "assistant", f"m{i}"))
        message_records = [r for r in store.load_records("s1") if r["kind"] == "message"]
        third_ordinal = message_records[2]["ordinal"]
        assert third_ordinal == 2
        store.append_compaction(
            "s1", compressed_up_to=third_ordinal, summary="S", model="deepseek-flash"
        )
        ctx = store.read_context_messages("s1")
        assert len(ctx) == 3
        assert ctx[0]["role"] == "user"
        assert ctx[0]["name"] == COMPACTION_SUMMARY_NAME
        assert ctx[0]["content"] == "S"
        assert [m["content"] for m in ctx[1:]] == ["m3", "m4"]
        reloaded = store.load_records("s1")
        assert len([r for r in reloaded if r["kind"] == "message"]) == 5
        assert len([r for r in reloaded if r["kind"] == "compaction"]) == 1

    def testCorruptLineSkipped(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        for i in range(3):
            store.append_message("s1", _msg("user", f"m{i}"))
        with (tmp_path / "sessions" / "s1.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("{{{ 这不是合法JSON\n")
        records = store.load_records("s1")
        assert len(records) == 3
        assert len([r for r in records if r["kind"] == "message"]) == 3
        assert len(store.read_context_messages("s1")) == 3

    def testLastModifiedAndIds(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        store.append_message("s1", _msg("user", "a"))
        store.append_message("s2", _msg("user", "b"))
        assert store.session_ids() == ["s1", "s2"]
        mtime = store.last_modified("s1")
        assert isinstance(mtime, float)
        assert mtime > 0
        assert store.last_modified("nope") is None

    def testMessageRounds(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        for i in range(3):
            store.append_message("s1", _msg("user", f"q{i}"))
            store.append_message("s1", _msg("assistant", f"a{i}"))
        assert store.message_rounds("s1") == 3

    def testNewSessionNoSpuriousWarning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = SessionStore(tmp_path)
        with caplog.at_level(logging.WARNING, logger="harness.session.store"):
            store.append_message("fresh", _msg("user", "hi"))
            assert store.read_context_messages("fresh") == [_msg("user", "hi")]
            assert store.read_context_messages("never-written") == []
        assert [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ] == []

    def testMessageRoundsCountsAllHistory(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        for i in range(3):
            store.append_message("s1", _msg("user", f"q{i}"))
            store.append_message("s1", _msg("assistant", f"a{i}"))
        store.append_compaction(
            "s1", compressed_up_to=5, summary="S", model="deepseek-flash"
        )
        assert store.message_rounds("s1") == 3

    def testMultipleCompactionsLastWins(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        for i in range(5):
            store.append_message("s1", _msg("user" if i % 2 == 0 else "assistant", f"m{i}"))
        store.append_compaction("s1", compressed_up_to=1, summary="S1", model="m")
        store.append_compaction("s1", compressed_up_to=3, summary="S2", model="m")
        ctx = store.read_context_messages("s1")
        assert len(ctx) == 2
        assert ctx[0]["name"] == COMPACTION_SUMMARY_NAME
        assert ctx[0]["content"] == "S2"
        assert ctx[1]["content"] == "m4"

    def testCorruptLineLogsWarning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = SessionStore(tmp_path)
        store.append_message("s1", _msg("user", "ok"))
        with (tmp_path / "sessions" / "s1.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("not-json\n")
        with caplog.at_level(logging.WARNING, logger="harness.session.store"):
            records = store.load_records("s1")
        assert len(records) == 1
        assert any(
            r.levelno >= logging.WARNING and "非法 JSON" in r.getMessage()
            for r in caplog.records
        )
