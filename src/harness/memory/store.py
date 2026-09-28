"""全局长期记忆存储 —— data/MEMORY/ 目录的唯一管理者。

目录结构：MEMORY.md（索引，一行一条：文件名 + 简述 + tags，只追加）、
若干时间戳命名的记忆文件（正文只有日期与记忆条目，同秒冲突加后缀）、
state.json（各会话的提取进度）。只追加、不去重、不合并（demo 决策）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BRIEF_MAX_CHARS: int = 60
_TAG_MAX_COUNT: int = 5
_TAG_MAX_CHARS: int = 20
_INDEX_FILENAME: str = "MEMORY.md"
_STATE_FILENAME: str = "state.json"


class MemoryStore:
    """全局 MEMORY 目录管理者：追加记忆、渲染索引、读取单条、记录提取进度。

    写路径无锁（与 session/store.py 的既有限制一致）；索引与记忆文件
    均只追加，永不改写既有内容。
    """

    def __init__(self, data_dir: Path) -> None:
        """以数据根目录构造实例，记忆落在 <data_dir>/MEMORY/ 下。"""
        self._root = Path(data_dir) / "MEMORY"

    def append(self, memories: list[str], tags: list[str]) -> str | None:
        """写入一次提取的记忆：新 md 文件 + 索引追加一行；空列表不写。

        返回记忆文件名；memories 为空时返回 None 且不产生任何文件。
        """
        if not memories:
            return None
        self._root.mkdir(parents=True, exist_ok=True)
        filename = self._new_filename()
        lines = [f"日期：{datetime.now().strftime('%Y-%m-%d')}", ""]
        lines += [f"- {memory}" for memory in memories]
        (self._root / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")
        with (self._root / _INDEX_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(self._index_line(filename, memories, tags) + "\n")
        return filename

    def render_index(self) -> str | None:
        """返回 MEMORY.md 索引全文（无文件或为空时返回 None）。"""
        try:
            text = (self._root / _INDEX_FILENAME).read_text(encoding="utf-8")
        except OSError:
            return None
        text = text.strip()
        return text or None

    def read(self, filename: str) -> str | None:
        """按文件名读取单个记忆文件全文；不存在返回 None。"""
        try:
            return (self._root / filename).read_text(encoding="utf-8")
        except OSError:
            return None

    def summarized_ordinal(self, session_id: str) -> int:
        """返回该会话已提取到的最大消息 ordinal（无记录时为 -1）。"""
        value = self._load_state().get(session_id, -1)
        return value if isinstance(value, int) else -1

    def mark_summarized(self, session_id: str, ordinal: int) -> None:
        """记录该会话的提取进度（只前进不回退，整体重写 state.json）。"""
        state = self._load_state()
        current = state.get(session_id, -1)
        if ordinal > (current if isinstance(current, int) else -1):
            state[session_id] = ordinal
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / _STATE_FILENAME).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

    def _new_filename(self) -> str:
        """生成不冲突的记忆文件名（时间戳，同秒冲突追加 -2、-3 后缀）。"""
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{base}.md"
        counter = 2
        while (self._root / filename).exists():
            filename = f"{base}-{counter}.md"
            counter += 1
        return filename

    def _index_line(self, filename: str, memories: list[str], tags: list[str]) -> str:
        """构造索引行：- 文件名｜简述（等 N 条）（tags: a, b）。"""
        brief = memories[0][:_BRIEF_MAX_CHARS]
        if len(memories) > 1:
            brief += f"（等 {len(memories)} 条）"
        line = f"- {filename}｜{brief}"
        cleaned = [tag[:_TAG_MAX_CHARS] for tag in tags if tag.strip()][:_TAG_MAX_COUNT]
        if cleaned:
            line += f"（tags: {', '.join(cleaned)}）"
        return line

    def _load_state(self) -> dict[str, Any]:
        """读取 state.json；文件不存在或损坏（告警）时按空状态处理。"""
        try:
            raw = (self._root / _STATE_FILENAME).read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            state = None
        if not isinstance(state, dict):
            logger.warning("state.json 不是合法 JSON 对象，按空进度处理")
            return {}
        return state
