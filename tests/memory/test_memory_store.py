"""MemoryStore 的单元测试（本地文件存储 + 哈希去重 + LLM 合并）。"""

import json
from pathlib import Path
from typing import Any

from harness.llm import AIMessage
from harness.memory.store import MemoryStore


class _ScriptedLLM:
    """按脚本内容返回 AIMessage 的假 LLM 客户端（记录收到的消息）。"""

    def __init__(self, content: str = "") -> None:
        self.content = content
        self.messages: list[dict[str, Any]] | None = None

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AIMessage:
        """返回预设内容并记录入参（与 LLMClient.invoke 同签名）。"""
        self.messages = messages
        return AIMessage(content=self.content)


def _entries_dir(tmp_path: Path, session_id: str) -> Path:
    """返回指定会话的条目目录路径。"""
    return tmp_path / "memory" / session_id / "entries"


def _index_path(tmp_path: Path, session_id: str) -> Path:
    """返回指定会话的 MEMORY.md 路径。"""
    return tmp_path / "memory" / session_id / "MEMORY.md"


class TestMemoryStore:

    def testWriteCreatesEntryAndIndex(self, tmp_path: Path) -> None:
        """首次写入创建条目文件与 MEMORY.md 索引行。"""
        store = MemoryStore(tmp_path, _ScriptedLLM())
        assert store.write("s1", "记忆A") is True
        entries = list(_entries_dir(tmp_path, "s1").glob("*.md"))
        assert len(entries) == 1
        text = entries[0].read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "hash:" in text
        assert "created:" in text
        assert "tags:" in text
        assert "记忆A" in text
        index = _index_path(tmp_path, "s1").read_text(encoding="utf-8")
        assert "记忆A" in index

    def testDedupSameContent(self, tmp_path: Path) -> None:
        """同内容（strip 规范化后）第二次写入被跳过，条目数不变。"""
        store = MemoryStore(tmp_path, _ScriptedLLM())
        assert store.write("s1", "记忆A") is True
        assert store.write("s1", " 记忆A ") is False
        assert len(list(_entries_dir(tmp_path, "s1").glob("*.md"))) == 1

    def testDifferentContentWrites(self, tmp_path: Path) -> None:
        """不同内容两次写入均成功，生成 2 个条目文件。"""
        store = MemoryStore(tmp_path, _ScriptedLLM())
        assert store.write("s1", "记忆A") is True
        assert store.write("s1", "记忆B") is True
        assert len(list(_entries_dir(tmp_path, "s1").glob("*.md"))) == 2

    def testRenderSummary(self, tmp_path: Path) -> None:
        """render_summary 渲染全部条目；无记忆的会话返回 None。"""
        store = MemoryStore(tmp_path, _ScriptedLLM())
        store.write("s1", "记忆A")
        store.write("s1", "记忆B")
        summary = store.render_summary("s1")
        assert summary is not None
        assert "记忆A" in summary
        assert "记忆B" in summary
        assert store.render_summary("s9") is None

    def testMergeAppliesActions(self, tmp_path: Path) -> None:
        """merge 按 LLM 输出的 DELETE/ADD 动作整理条目并重写索引。"""
        fake = _ScriptedLLM()
        store = MemoryStore(tmp_path, fake)
        store.write("s1", "旧记忆")
        entry_hash = store.list_entries("s1")[0]["hash"]
        fake.content = json.dumps(
            [
                {"action": "DELETE", "hash": entry_hash},
                {"action": "ADD", "content": "新记忆"},
            ],
            ensure_ascii=False,
        )
        assert store.merge("s1") == 2
        entries = store.list_entries("s1")
        assert len(entries) == 1
        assert entries[0]["content"] == "新记忆"
        index = _index_path(tmp_path, "s1").read_text(encoding="utf-8")
        assert "旧记忆" not in index
        assert "新记忆" in index

    def testMergeInvalidLLMOutputKeepsOriginal(self, tmp_path: Path) -> None:
        """LLM 输出非法 JSON 时 merge 不抛异常、返回 -1、内容逐字节不变。"""
        fake = _ScriptedLLM("不是JSON")
        store = MemoryStore(tmp_path, fake)
        store.write("s1", "记忆A")
        before = _index_path(tmp_path, "s1").read_bytes()
        assert store.merge("s1") == -1
        assert _index_path(tmp_path, "s1").read_bytes() == before
        assert len(store.list_entries("s1")) == 1

    def testMergeUpdateDedupCountsApplied(self, tmp_path: Path) -> None:
        """UPDATE 目标内容与现存条目重复时合并生效，不返回 -1。"""
        fake = _ScriptedLLM()
        store = MemoryStore(tmp_path, fake)
        store.write("s1", "旧记忆A")
        store.write("s1", "重复目标B")
        target_hash = next(
            e["hash"] for e in store.list_entries("s1") if e["content"] == "旧记忆A"
        )
        fake.content = json.dumps(
            [{"action": "UPDATE", "hash": target_hash, "content": "重复目标B"}],
            ensure_ascii=False,
        )
        applied = store.merge("s1")
        assert applied >= 1
        entries = store.list_entries("s1")
        assert len(entries) == 1
        assert entries[0]["content"] == "重复目标B"

    def testMergeShortHashRejected(self, tmp_path: Path) -> None:
        """短于 12 位的哈希前缀被拒绝，不误删条目。"""
        fake = _ScriptedLLM()
        store = MemoryStore(tmp_path, fake)
        store.write("s1", "记忆A")
        fake.content = json.dumps(
            [{"action": "DELETE", "hash": "a"}], ensure_ascii=False
        )
        assert store.merge("s1") == 0
        assert len(store.list_entries("s1")) == 1

    def testWriteEmptyContentRejected(self, tmp_path: Path) -> None:
        """空/纯空白内容被拒绝写入且不创建任何文件。"""
        store = MemoryStore(tmp_path, _ScriptedLLM())
        assert store.write("s1", "   ") is False
        assert store.list_entries("s1") == []
        assert not _entries_dir(tmp_path, "s1").exists()
