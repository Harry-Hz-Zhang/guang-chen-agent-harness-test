"""输出复用器：多会话并发写终端的唯一关卡。

一把 threading.Lock 同时护「写终端」与「前台指针」。前台会话与系统提示
（session_id=None）原样输出，流式分片直通 raw_writer 保持现状逐字效果；
后台会话输出统一加 `[xxxx]` 胸牌前缀，流式分片按行缓冲，遇换行或回合
结束（session_idle_hint）才整行落屏，保证终端上任意一行只归属单一会话。
writer/raw_writer 抛出的异常一律捕获并记日志，输出层永不成为崩溃源。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

Writer = Callable[[str], None]

_IDLE_HINT_TEXT = "已完成"
_TAG_FORMAT = "[{sid}] "

logger = logging.getLogger(__name__)


class OutputMux:
    """多会话输出复用器：全局写锁 + 前台/后台判定 + 胸牌前缀。"""

    def __init__(
        self,
        writer: Writer,
        raw_writer: Writer,
        background_prefix_len: int = 4,
    ) -> None:
        """持有整行 writer 与流式 raw_writer，初始化前台指针与行缓冲表。"""
        self._writer = writer
        self._raw_writer = raw_writer
        self._prefix_len = background_prefix_len
        self._lock = threading.Lock()
        self._foreground: str | None = None
        self._pending: dict[str, str] = {}

    def _is_foreground(self, session_id: str | None) -> bool:
        """判断会话是否前台样式：系统提示（None）恒前台，或等于前台指针。"""
        if session_id is None or self._foreground is None:
            return True
        return session_id == self._foreground

    def _tag(self, session_id: str) -> str:
        """生成后台胸牌前缀，如 `[a1b2] `。"""
        return _TAG_FORMAT.format(sid=session_id[: self._prefix_len])

    def _emit_line(self, text: str) -> None:
        """写整行（自带换行）到 writer；writer 抛异常仅记日志，不上抛。

        注意：仅在持有 self._lock 时调用；writer 回调不得重入本类方法（Lock 不可重入）。
        """
        try:
            self._writer(text)
        except Exception:
            logger.warning(
                "output writer raised; line dropped: %r", text, exc_info=True
            )

    def _emit_raw(self, piece: str) -> None:
        """写流式分片到 raw_writer；抛异常仅记日志，不上抛。"""
        try:
            self._raw_writer(piece)
        except Exception:
            logger.warning("raw writer raised; piece dropped: %r", piece, exc_info=True)

    def _flush_pending_locked(self, sid: str, tagged: bool) -> None:
        """落屏并清空指定会话的半行缓冲（调用方必须已持有 self._lock）。

        tagged=True 后台样式（带胸牌）；tagged=False 用于切前台时无牌落屏。
        空缓冲不产生孤胸牌行。
        """
        pending = self._pending.pop(sid, "")
        if pending:
            text = (self._tag(sid) + pending) if tagged else pending
            self._emit_line(text + "\n")

    def set_foreground(self, session_id: str | None) -> None:
        """更新前台会话指针。

        新前台若残留后台期的半行缓冲，按 design W4 先原样（无牌）落屏一次；
        传 None 表示回到无前台初始态：全部后台缓冲带牌落屏后再清指针，
        防止悬挂缓冲破坏行所有权（design review I5 契约）。
        """
        with self._lock:
            if session_id is None:
                for sid in list(self._pending):
                    self._flush_pending_locked(sid, tagged=True)
            else:
                self._flush_pending_locked(session_id, tagged=False)
            self._foreground = session_id

    def line(self, text: str, session_id: str | None = None) -> None:
        """立即整行输出（自带换行）：前台/系统原样，后台加胸牌前缀。"""
        with self._lock:
            if session_id is None or session_id == self._foreground:
                self._emit_line(text + "\n")
            else:
                self._emit_line(self._tag(session_id) + text + "\n")

    def chunk(self, piece: str, session_id: str) -> None:
        """流式分片：前台直通 raw_writer（逐字）；后台行缓冲，遇换行整行落屏。

        空缓冲遇补位换行（如 finalize 尾部 "\n"）不产生孤胸牌行。
        """
        with self._lock:
            if self._is_foreground(session_id):
                self._emit_raw(piece)
                return
            buffer = self._pending.get(session_id, "")
            segments = piece.split("\n")
            for closed in segments[:-1]:
                buffer += closed
                if buffer:
                    self._emit_line(self._tag(session_id) + buffer + "\n")
                buffer = ""
            self._pending[session_id] = buffer + segments[-1]

    def session_idle_hint(self, sid: str) -> None:
        """回合结束提示：后台会话 flush 半行后输出 `[xxxx] 已完成`；前台静默（design §7）。"""
        with self._lock:
            if self._is_foreground(sid):
                return
            self._flush_pending_locked(sid, tagged=True)
            self._emit_line(self._tag(sid) + _IDLE_HINT_TEXT + "\n")
