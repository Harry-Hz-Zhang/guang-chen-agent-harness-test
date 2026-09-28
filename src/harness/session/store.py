"""SessionStore —— 基于本地 JSONL 文件的会话存储（append-only、按会话隔离）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COMPACTION_SUMMARY_NAME: str = "__compaction_summary__"


class SessionStore:
    """会话存储：每个 session 对应一个 JSONL 文件，全部写入均为追加模式。

    文件位于 <data_dir>/sessions/<session_id>.jsonl，每行一个独立 JSON
    对象，公共字段仅为 kind（message / compaction 两类）；ordinal 不
    落盘，由读取方按行序派生（append-only 文件行序即稳定序号）。
    注意：同一 session id 不支持多进程并发写——追加写无锁，多窗口
    并行仅在不同 session 各写各文件的前提下安全（决策 6）。
    """

    def __init__(self, data_dir: Path) -> None:
        """以数据根目录构造存储实例。

        data_dir 为运行期产物根目录（如 data/），会话文件实际落在其
        sessions/ 子目录下，目录在首次写入时自动创建。
        """
        self._data_dir = Path(data_dir)

    def append_message(self, session_id: str, message: dict[str, Any]) -> None:
        """向指定会话追加一条消息记录（纯追加，不预读文件）。"""
        self._append_record(
            session_id,
            {"kind": "message", "message": message},
        )

    def append_compaction(
        self, session_id: str, compressed_up_to: int, summary: str
    ) -> None:
        """向指定会话追加一条压缩记录；原始消息行不删除、不改写。"""
        self._append_record(
            session_id,
            {
                "kind": "compaction",
                "compressed_up_to": compressed_up_to,
                "summary": summary,
            },
        )

    def load_records(self, session_id: str) -> list[dict[str, Any]]:
        """按文件行序返回会话全部记录（每条含读取时派生的 ordinal）。

        非法 JSON 行（损坏行）跳过并记录 warning，不影响其余行；
        会话文件不存在时返回空列表。
        """
        return self._read_valid_records(self._file_path(session_id))

    def read_context_messages(self, session_id: str) -> list[dict[str, Any]]:
        """读取组装上下文用的消息列表（应用最后一次压缩记录）。

        单趟遍历：同时收集 message 记录与最后一条压缩记录，再按压缩
        区间上限截取。存在压缩记录时返回 [压缩摘要消息] + ordinal 大于
        上限的消息体，摘要消息形如
        {"role":"user","name":"__compaction_summary__","content":摘要原文}；
        不存在压缩记录时返回全部消息体。均按文件行序（ordinal 顺序）。
        """
        last_compaction: dict[str, Any] | None = None
        kept: list[dict[str, Any]] = []
        for record in self.load_records(session_id):
            kind = record.get("kind")
            if kind == "compaction":
                last_compaction = record
            elif kind == "message":
                message = record.get("message")
                if isinstance(message, dict):
                    kept.append(record)
                else:
                    logger.warning(
                        "会话 %s 的消息记录 ordinal=%r 缺少合法 message 字段，已跳过",
                        session_id,
                        record.get("ordinal"),
                    )
        if last_compaction is None:
            return [record["message"] for record in kept]
        cut = last_compaction.get("compressed_up_to")
        if not isinstance(cut, int):
            cut = -1
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "name": COMPACTION_SUMMARY_NAME,
                "content": str(last_compaction.get("summary", "")),
            }
        ]
        messages.extend(
            record["message"] for record in kept if record["ordinal"] > cut
        )
        return messages

    def session_ids(self) -> list[str]:
        """列出全部会话 id（sessions 目录下 *.jsonl 文件名去后缀，按名称排序）。"""
        return sorted(
            path.stem for path in self._sessions_dir().glob("*.jsonl")
        )

    def last_modified(self, session_id: str) -> float | None:
        """返回会话文件的最后修改时间（mtime）；文件不存在或不可访问时返回 None。"""
        try:
            return self._file_path(session_id).stat().st_mtime
        except OSError:
            return None

    def message_rounds(self, session_id: str) -> int:
        """统计会话累计对话轮数（全部消息记录中的 user 消息数，仅供 /history 展示）。

        统计口径为会话文件中全部 kind=="message" 且 message.role=="user"
        的记录数（原始 user 消息不会带 __compaction_summary__ 标记，无需
        过滤），压缩后不回落；压缩判定请使用 compressor 的
        uncompressed_rounds（未压缩窗口口径）。
        """
        return sum(
            1
            for record in self.load_records(session_id)
            if record.get("kind") == "message"
            and isinstance(record.get("message"), dict)
            and record["message"].get("role") == "user"
        )

    def _sessions_dir(self) -> Path:
        """返回会话文件所在目录路径。"""
        return self._data_dir / "sessions"

    def _file_path(self, session_id: str) -> Path:
        """返回指定会话的 JSONL 文件路径。"""
        return self._sessions_dir() / f"{session_id}.jsonl"

    def _append_record(self, session_id: str, record: dict[str, Any]) -> None:
        """以追加模式（"a"）写一行 JSON 记录并立即落盘，不重写整文件。"""
        path = self._file_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_valid_records(self, path: Path) -> list[dict[str, Any]]:
        """逐行读取 JSONL 文件并返回可解析的记录列表，任何情况不抛异常。

        每条记录注入读取时派生的 ordinal（当前已解析条数，损坏行不
        计入）；空行静默跳过；非法 JSON 或非 JSON 对象的行记 warning
        后跳过；文件不存在返回空列表（正常路径，无 warning）；文件级
        读取失败返回已解析的部分。
        """
        records: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError:
                        logger.warning(
                            "会话文件 %s 第 %d 行非法 JSON，已跳过", path, line_no
                        )
                        continue
                    if not isinstance(parsed, dict):
                        logger.warning(
                            "会话文件 %s 第 %d 行不是 JSON 对象，已跳过",
                            path,
                            line_no,
                        )
                        continue
                    parsed["ordinal"] = len(records)
                    records.append(parsed)
        except FileNotFoundError:
            return records
        except (OSError, UnicodeDecodeError):
            logger.warning(
                "会话文件 %s 读取失败，返回已解析的 %d 条记录", path, len(records)
            )
        return records
