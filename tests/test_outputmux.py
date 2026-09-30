"""OutputMux 输出复用器的单元测试。

覆盖：前台整行透传 / 后台胸牌前缀 / 后台流式行缓冲（W3） / 前台流式
逐字直通 / idle hint 前台抑制与后台 flush / writer 异常护栏 / 多会话
并发下的行所有权。方法名沿用 openspec/changes/run-background-sessions/
tasks.md 的 RED 原名（camelCase，AGENTS.md 命名规范例外条款）。
"""

from __future__ import annotations

import threading

from harness.outputmux import OutputMux


class RecordingWriter:
    """记录每次调用入参的测试桩 writer（多线程下由 mux 锁串行化调用）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> None:
        self.calls.append(text)


class RaisingWriter:
    """每次调用先计数再抛 RuntimeError 的测试桩 writer。"""

    def __init__(self) -> None:
        self.calls: int = 0

    def __call__(self, text: str) -> None:
        self.calls += 1
        raise RuntimeError("writer broken")


class TestOutputMux:
    def shouldPassthroughForegroundSessionLine(self) -> None:
        """前台会话整行输出原样透传，不带胸牌前缀。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("a1b2c3d4")
        mux.line("你好", session_id="a1b2c3d4")
        assert writer.calls == ["你好\n"]

    def shouldPrefixBackgroundSessionLine(self) -> None:
        """后台会话整行输出带 `[a1b2] ` 胸牌前缀（8 位 id 取前 4 位）。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")
        mux.line("你好", session_id="a1b2c3d4")
        assert writer.calls == ["[a1b2] 你好\n"]

    def shouldEmitIdleHintForBackgroundSession(self) -> None:
        """后台会话回合结束输出 `[a1b2] 已完成` 提示行。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")
        mux.session_idle_hint("a1b2c3d4")
        assert writer.calls == ["[a1b2] 已完成\n"]

    def shouldEmitIdleHintForForegroundSession(self) -> None:
        """前台会话 idle hint 完全静默：不产生任何 writer/raw_writer 调用（design §7 前台抑制）。"""
        writer = RecordingWriter()
        raw = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=raw)
        mux.set_foreground("a1b2c3d4")
        mux.session_idle_hint("a1b2c3d4")
        assert writer.calls == []
        assert raw.calls == []

    def shouldBufferBackgroundChunkUntilNewline(self) -> None:
        """后台流式分片行缓冲：换行前零落屏，遇 \\n 整行一次写出（W3 行同步）。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")
        mux.chunk("思", session_id="a1b2c3d4")
        mux.chunk("考", session_id="a1b2c3d4")
        assert writer.calls == []  # flush 前落屏 0 次
        mux.chunk("\n", session_id="a1b2c3d4")
        assert writer.calls == ["[a1b2] 思考\n"]

    def shouldStreamForegroundChunkPiecewise(self) -> None:
        """前台流式分片逐字直通 raw_writer：不缓冲、不加前缀、不合并。"""
        writer = RecordingWriter()
        raw = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=raw)
        mux.set_foreground("a1b2c3d4")
        mux.chunk("思", session_id="a1b2c3d4")
        mux.chunk("考", session_id="a1b2c3d4")
        assert raw.calls == ["思", "考"]
        assert writer.calls == []

    def shouldFlushPendingBufferOnIdle(self) -> None:
        """回合结束把未换行的半行缓冲 flush 成完整行，再输出提示行，不丢缓冲。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")
        mux.chunk("半行", session_id="a1b2c3d4")
        assert writer.calls == []
        mux.session_idle_hint("a1b2c3d4")
        assert writer.calls == ["[a1b2] 半行\n", "[a1b2] 已完成\n"]

    def shouldSuffixExceptionOnGuard(self) -> None:
        """writer/raw_writer 抛异常时 mux 方法不上抛，调用方存活（输出层永不成为崩溃源）。"""
        writer = RaisingWriter()
        raw = RaisingWriter()
        mux = OutputMux(writer=writer, raw_writer=raw)
        mux.set_foreground("ffffffff")
        mux.line("你好", session_id="a1b2c3d4")  # 后台整行 → writer 抛 → 吞
        mux.set_foreground("a1b2c3d4")
        mux.chunk("思", session_id="a1b2c3d4")  # 前台直通 → raw 抛 → 吞
        mux.chunk("\n", session_id="a1b2c3d4")
        mux.session_idle_hint("a1b2c3d4")  # 前台抑制 → 零输出
        mux.line("再一行")  # 存活证明：后续调用仍正常
        assert writer.calls == 2
        assert raw.calls == 2  # 前台 "思" 与补位 "\n" 各直通一次

    def shouldHoldLineOwnershipAcrossSessions(self) -> None:
        """两会话线程交错 chunk：落屏行各自完整归属单一会话，无行内碎片混杂。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")
        barrier = threading.Barrier(2, timeout=5)

        def drive(sid: str, marker: str) -> None:
            barrier.wait()
            for _ in range(40):
                mux.chunk(marker, session_id=sid)
            mux.chunk("\n", session_id=sid)

        threads = [
            threading.Thread(target=drive, args=("aaaaaaaa", "A")),
            threading.Thread(target=drive, args=("bbbbbbbb", "B")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        for t in threads:
            assert not t.is_alive(), "并发线程未在时限内结束（疑似死锁或异常未传播）"

        assert writer.calls, "后台流式应有整行落屏"
        for line in writer.calls:
            assert line.endswith("\n")
            body = line[:-1]
            if body.startswith("[aaaa] "):
                assert set(body[len("[aaaa] ") :]) == {"A"}
            elif body.startswith("[bbbb] "):
                assert set(body[len("[bbbb] ") :]) == {"B"}
            else:
                raise AssertionError(f"不明的行前缀: {line!r}")

    def shouldTreatNullSessionAsForegroundStyle(self) -> None:
        """session_id=None（系统提示）以前台样式输出，永不带胸牌。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")  # 即便已有前台会话，None 仍以前台样式输出
        mux.line("系统提示", session_id=None)
        assert writer.calls == ["系统提示\n"]

    def shouldFlushFinalNewlineThenIdleHintAsTwoLines(self) -> None:
        """换行 flush 的行与 idle hint 行是两次独立落屏：恰 2 行、无粘连、无多余空行。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")
        mux.chunk("半行", session_id="a1b2c3d4")
        mux.chunk("\n", session_id="a1b2c3d4")
        mux.session_idle_hint("a1b2c3d4")
        assert writer.calls == ["[a1b2] 半行\n", "[a1b2] 已完成\n"]

    def shouldFlushPendingUntaggedOnForegroundSwitch(self) -> None:
        """W4：切回以前台身份落屏的残留半行，切指针时先无牌落屏，防输出丢失。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")
        mux.chunk("半行", session_id="a1b2c3d4")
        mux.set_foreground("a1b2c3d4")
        assert writer.calls == ["半行\n"]

    def shouldSplitMultiNewlineChunkIntoLines(self) -> None:
        """单个分片含多个换行：闭合段逐行落屏，尾段留缓冲待拼接。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")
        mux.chunk("a\nb", session_id="a1b2c3d4")
        assert writer.calls == ["[a1b2] a\n"]
        mux.chunk("c\n", session_id="a1b2c3d4")
        assert writer.calls == ["[a1b2] a\n", "[a1b2] bc\n"]

    def shouldSkipEmptyLineAfterTrailingNewline(self) -> None:
        """正文以换行收尾后 finalize 的补位换行：空缓冲不产生孤胸牌行（I1 修复验证）。"""
        writer = RecordingWriter()
        mux = OutputMux(writer=writer, raw_writer=RecordingWriter())
        mux.set_foreground("ffffffff")
        mux.chunk("正文\n", session_id="a1b2c3d4")
        mux.chunk("\n", session_id="a1b2c3d4")  # StreamRenderer.finalize 的补位换行
        mux.session_idle_hint("a1b2c3d4")
        assert writer.calls == ["[a1b2] 正文\n", "[a1b2] 已完成\n"]
