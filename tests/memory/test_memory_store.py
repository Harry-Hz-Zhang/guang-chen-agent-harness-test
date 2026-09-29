"""MemoryStore（全局 MEMORY 目录存储）的单元测试 —— 对应 refactor-global-memory tasks.md Task 1 RED 条目。"""

import logging
from datetime import datetime
from pathlib import Path

import pytest

from harness.memory import store as store_module
from harness.memory.store import MemoryStore


class _FixedDatetime(datetime):
    """now() 固定返回 2026-09-28 14:30:05，用于确定文件名与日期断言。"""

    @classmethod
    def now(cls) -> datetime:
        return cls(2026, 9, 28, 14, 30, 5)


class TestMemoryStore:

    def testAppendWritesEntryFileAndIndexLine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一次提取写入记忆文件（日期 + 条目）并在 MEMORY.md 追加一行索引。"""
        monkeypatch.setattr(store_module, "datetime", _FixedDatetime)
        store = MemoryStore(tmp_path)
        filename = store.append(["用户偏好简洁回复", "正在开发 demo"], ["偏好", "项目"])
        assert filename == "20260928-143005.md"
        entry = (tmp_path / "MEMORY" / filename).read_text(encoding="utf-8")
        assert entry.startswith("日期：2026-09-28\n\n")
        assert "- 用户偏好简洁回复" in entry
        assert "- 正在开发 demo" in entry
        index = (tmp_path / "MEMORY" / "MEMORY.md").read_text(encoding="utf-8")
        assert index == (
            "- 20260928-143005.md｜用户偏好简洁回复（等 2 条）（tags: 偏好, 项目）\n"
        )

    def testAppendBriefTruncatedAndCountSuffix(self, tmp_path: Path) -> None:
        """索引行简述截断 60 字，多条记忆追加「等 N 条」。"""
        store = MemoryStore(tmp_path)
        store.append(["长" * 100, "第二条"], [])
        index = (tmp_path / "MEMORY" / "MEMORY.md").read_text(encoding="utf-8")
        assert ("长" * 60) in index
        assert ("长" * 61) not in index
        assert "（等 2 条）" in index

    def testAppendEmptyTagsOmitTagSection(self, tmp_path: Path) -> None:
        """tags 为空时索引行不含 tags 段。"""
        store = MemoryStore(tmp_path)
        store.append(["用户养了一只猫叫团子"], [])
        index = (tmp_path / "MEMORY" / "MEMORY.md").read_text(encoding="utf-8")
        assert "tags:" not in index
        assert "用户养了一只猫叫团子" in index

    def testAppendEmptyMemoriesNoop(self, tmp_path: Path) -> None:
        """空记忆列表不产生任何文件。"""
        store = MemoryStore(tmp_path)
        assert store.append([], []) is None
        assert not (tmp_path / "MEMORY").exists()

    def testAppendFilenameCollisionSuffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同秒文件名冲突时追加 -2 后缀，不覆盖既有文件。"""
        monkeypatch.setattr(store_module, "datetime", _FixedDatetime)
        memory_dir = tmp_path / "MEMORY"
        memory_dir.mkdir()
        (memory_dir / "20260928-143005.md").write_text("旧内容", encoding="utf-8")
        store = MemoryStore(tmp_path)
        filename = store.append(["新记忆"], [])
        assert filename == "20260928-143005-2.md"
        assert (memory_dir / "20260928-143005.md").read_text(encoding="utf-8") == "旧内容"
        assert "- 新记忆" in (memory_dir / filename).read_text(encoding="utf-8")

    def testRenderIndexNoneWhenAbsent(self, tmp_path: Path) -> None:
        """无 MEMORY 目录时索引为 None。"""
        assert MemoryStore(tmp_path).render_index() is None

    def testRenderIndexReturnsIndexLines(self, tmp_path: Path) -> None:
        """索引按追加序返回全部索引行。"""
        store = MemoryStore(tmp_path)
        first = store.append(["第一条记忆"], [])
        second = store.append(["第二条记忆"], [])
        index = store.render_index()
        assert index is not None
        assert index.splitlines() == [
            f"- {first}｜第一条记忆",
            f"- {second}｜第二条记忆",
        ]

    def testReadEntryContent(self, tmp_path: Path) -> None:
        """read 返回记忆文件全文（含日期行）。"""
        store = MemoryStore(tmp_path)
        filename = store.append(["用户偏好简洁回复"], [])
        content = store.read(filename)
        assert content is not None
        assert content.startswith("日期：")
        assert "- 用户偏好简洁回复" in content

    def testReadMissingReturnsNone(self, tmp_path: Path) -> None:
        """read 不存在的文件返回 None。"""
        assert MemoryStore(tmp_path).read("nonexistent.md") is None

    def testSummarizedOrdinalDefaultsAndPersists(self, tmp_path: Path) -> None:
        """提取进度缺省 -1，写入后新实例可读回（持久化）。"""
        store = MemoryStore(tmp_path)
        assert store.summarized_ordinal("s1") == -1
        store.mark_summarized("s1", 5)
        assert MemoryStore(tmp_path).summarized_ordinal("s1") == 5

    def testCorruptStateTreatedAsEmpty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """state.json 损坏（非法 JSON / 非对象）时告警并按空进度处理，不抛异常。"""
        memory_dir = tmp_path / "MEMORY"
        memory_dir.mkdir()
        (memory_dir / "state.json").write_text("不是 JSON", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert MemoryStore(tmp_path).summarized_ordinal("s1") == -1
        assert "state.json" in caplog.text
        (memory_dir / "state.json").write_text("[1, 2]", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert MemoryStore(tmp_path).summarized_ordinal("s2") == -1
        assert caplog.text.count("state.json") >= 2

    def testRenderIndexEmptyFileReturnsNone(self, tmp_path: Path) -> None:
        """空白 MEMORY.md 视为无记忆（返回 None）。"""
        memory_dir = tmp_path / "MEMORY"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("  \n", encoding="utf-8")
        assert MemoryStore(tmp_path).render_index() is None
