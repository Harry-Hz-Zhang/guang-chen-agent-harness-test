"""长期记忆存储：MEMORY.md 索引 + 细碎条目文件（哈希去重 + LLM 合并）。

目录结构：data_dir/memory/<session_id>/MEMORY.md（全部条目的索引）与
entries/<sha256 前 12 位>.md（单条记忆，frontmatter 含完整哈希、创建
时间与标签）。写入按规范化内容的 SHA-256 去重；merge 以 LLM 输出的
四动作（ADD/UPDATE/DELETE/NOOP）整理条目后重写索引，输出非法时保留
原状并返回 -1。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.prompts import MEMORY_MERGE_PROMPT

logger = logging.getLogger(__name__)

_HASH_PREFIX_LEN: int = 12
_FRONTMATTER_SEPARATOR: str = "\n---\n"


def _content_hash(content: str) -> str:
    """计算规范化（strip 后）内容的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


class MemoryStore:
    """按会话隔离的长期记忆存储（本地文件为唯一真源）。

    写路径（write/merge 的读-改-写）无锁、不保证线程安全：与
    session/store.py 的既有限制一致。后台总结线程与主线程并发
    调 write 时，条目文件名即去重键（天然幂等），最坏情况是
    MEMORY.md 索引丢行（下次写入自动重扫重建）。
    """

    def __init__(self, data_dir: Path, llm: Any) -> None:
        """注入数据根目录与 LLM 客户端（仅 merge 使用）。"""
        self._root = Path(data_dir) / "memory"
        self._llm = llm

    def write(
        self, session_id: str, content: str, tags: list[str] | None = None
    ) -> bool:
        """写入一条记忆：规范化后按 SHA-256 去重，重复时跳过返回 False。"""
        normalized = content.strip()
        if not normalized:
            logger.warning("拒绝写入空记忆内容（session=%s）", session_id)
            return False
        digest = _content_hash(normalized)
        entries_dir = self._entries_dir(session_id)
        entry_path = entries_dir / f"{digest[:_HASH_PREFIX_LEN]}.md"
        if entry_path.exists():
            return False
        entries_dir.mkdir(parents=True, exist_ok=True)
        tag_list = tags if tags is not None else []
        created = datetime.now(timezone.utc).isoformat()
        frontmatter = (
            "---\n"
            f"hash: {digest}\n"
            f"created: {created}\n"
            f"tags: [{', '.join(tag_list)}]\n"
            "---\n"
        )
        entry_path.write_text(frontmatter + normalized + "\n", encoding="utf-8")
        self._rewrite_index(session_id)
        return True

    def list_entries(self, session_id: str) -> list[dict[str, Any]]:
        """列出该会话全部记忆条目（无目录时返回空列表）。"""
        entries_dir = self._entries_dir(session_id)
        if not entries_dir.exists():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(entries_dir.glob("*.md")):
            entry = self._parse_entry(path)
            if entry is not None:
                entries.append(entry)
        return entries

    def render_summary(self, session_id: str, max_chars: int = 2000) -> str | None:
        """把全部条目渲染为一段摘要文本（超 max_chars 截断）；无记忆返回 None。"""
        entries = self.list_entries(session_id)
        if not entries:
            return None
        lines = [f"- {entry['content']}" for entry in entries]
        return "\n".join(lines)[:max_chars]

    def merge(self, session_id: str) -> int:
        """用 LLM 输出的动作数组整理记忆并重写索引。

        返回应用的动作数；条目为空返回 0；LLM 输出不是合法 JSON
        数组时返回 -1（失败标记），此时任何文件都不被改动。
        """
        entries = self.list_entries(session_id)
        if not entries:
            return 0
        rendering = "\n".join(
            f"- [{entry['hash'][:_HASH_PREFIX_LEN]}] {entry['content']}"
            for entry in entries
        )
        prompt = MEMORY_MERGE_PROMPT + rendering
        message = self._llm.invoke([{"role": "user", "content": prompt}])
        raw = str(getattr(message, "content", "") or "")
        try:
            actions = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("记忆合并输出不是合法 JSON，保留原状（session=%s）", session_id)
            return -1
        if not isinstance(actions, list):
            logger.warning("记忆合并输出不是 JSON 数组，保留原状（session=%s）", session_id)
            return -1
        applied = 0
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = action.get("action")
            if kind == "ADD":
                content = action.get("content")
                if isinstance(content, str) and self.write(session_id, content):
                    applied += 1
            elif kind == "DELETE":
                if self._delete_entry(session_id, action.get("hash")):
                    applied += 1
            elif kind == "UPDATE":
                content = action.get("content")
                if isinstance(content, str) and self._delete_entry(
                    session_id, action.get("hash")
                ):
                    self.write(session_id, content)
                    applied += 1
            # NOOP 与未知动作跳过
        self._rewrite_index(session_id)
        return applied

    def _session_dir(self, session_id: str) -> Path:
        """返回该会话的记忆目录路径。"""
        return self._root / session_id

    def _entries_dir(self, session_id: str) -> Path:
        """返回该会话的条目文件目录路径。"""
        return self._session_dir(session_id) / "entries"

    def _index_path(self, session_id: str) -> Path:
        """返回该会话的 MEMORY.md 索引路径。"""
        return self._session_dir(session_id) / "MEMORY.md"

    def _parse_entry(self, path: Path) -> dict[str, Any] | None:
        """解析单个条目文件（frontmatter + 正文）；文件不可读时告警返回 None。

        非 frontmatter 格式的损坏文件宽容降级为普通条目（hash 取
        文件名前缀、正文取全文，仍可按 stem 匹配删除）；文件不可读
        时告警返回 None。
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("记忆条目读取失败，已跳过（%s）：%s", path, exc)
            return None
        hash_value = path.stem
        created = ""
        tags: list[str] = []
        content = text
        if text.startswith("---\n"):
            end = text.find(_FRONTMATTER_SEPARATOR, 4)
            if end != -1:
                header = text[4:end]
                content = text[end + len(_FRONTMATTER_SEPARATOR) :]
                for line in header.splitlines():
                    key, _, value = line.partition(":")
                    key = key.strip()
                    if key == "hash":
                        hash_value = value.strip()
                    elif key == "created":
                        created = value.strip()
                    elif key == "tags":
                        inner = value.strip().strip("[]")
                        tags = [t.strip() for t in inner.split(",") if t.strip()]
        return {
            "hash": hash_value,
            "content": content.strip(),
            "created": created,
            "tags": tags,
            "path": path,
        }

    def _rewrite_index(self, session_id: str) -> None:
        """根据当前条目集合重写 MEMORY.md 索引。"""
        entries = self.list_entries(session_id)
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        lines = [f"# 会话 {session_id} 的长期记忆索引", ""]
        if entries:
            for entry in entries:
                first_line = (
                    entry["content"].splitlines()[0] if entry["content"] else ""
                )
                tag_text = "、".join(entry["tags"]) if entry["tags"] else "无"
                lines.append(
                    f"- [{entry['hash'][:_HASH_PREFIX_LEN]}] {first_line}（标签：{tag_text}）"
                )
        else:
            lines.append("（暂无记忆条目）")
        self._index_path(session_id).write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _delete_entry(self, session_id: str, target: Any) -> bool:
        """按哈希（全长或不少于 12 位的前缀）删除条目文件，返回是否删除。

        短于 12 位的目标直接拒绝（删除是破坏性操作，对 LLM 回传的
        哈希做最小长度校验，防止过短前缀误删无关条目）。
        """
        if not isinstance(target, str) or len(target) < _HASH_PREFIX_LEN:
            return False
        for entry in self.list_entries(session_id):
            entry_hash = entry["hash"]
            if (
                entry_hash == target
                or entry_hash.startswith(target)
                or target.startswith(entry_hash)
            ):
                Path(entry["path"]).unlink(missing_ok=True)
                return True
        return False
