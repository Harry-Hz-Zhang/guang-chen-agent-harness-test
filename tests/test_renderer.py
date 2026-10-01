"""StreamRenderer 流式事件终端渲染器的单元测试。"""

from __future__ import annotations

from harness.llm import DoneEvent, ReasoningDelta, TextDelta, UsageEvent
from harness.renderer import StreamRenderer


class _RawWriterStub:
    """收集 raw 输出片段的测试桩。"""

    def __init__(self) -> None:
        self.pieces: list[str] = []

    def __call__(self, text: str) -> None:
        self.pieces.append(text)

    @property
    def output(self) -> str:
        return "".join(self.pieces)


class TestStreamRenderer:

    def testReasoningDeltaPrefixAndContent(self) -> None:
        """传入单个 ReasoningDelta("思") → 返回包含前缀与内容，且委托 raw_writer 逐片写入。"""
        raw = _RawWriterStub()
        renderer = StreamRenderer(raw_writer=raw)
        out = renderer.render(ReasoningDelta(text="思"))
        assert out == "思考｜思"
        assert raw.pieces == ["思考｜", "思"]

    def testContinuousReasoningNoDuplicatePrefix(self) -> None:
        """连续传入两个 ReasoningDelta → 仅首片带前缀，第二片不重复前缀。"""
        raw = _RawWriterStub()
        renderer = StreamRenderer(raw_writer=raw)
        out1 = renderer.render(ReasoningDelta(text="思"))
        out2 = renderer.render(ReasoningDelta(text="考"))
        assert out1 == "思考｜思"
        assert out2 == "考"
        assert raw.pieces == ["思考｜", "思", "考"]
        assert raw.output == "思考｜思考"

    def testTransitionReasoningToTextNewline(self) -> None:
        """从思考段切换到正文段时插入换行分隔，正文不带思考前缀。"""
        raw = _RawWriterStub()
        renderer = StreamRenderer(raw_writer=raw)
        renderer.render(ReasoningDelta(text="思"))
        out = renderer.render(TextDelta(text="答"))
        assert out == "\n答"
        assert raw.pieces == ["思考｜", "思", "\n", "答"]

    def testTransitionTextToReasoningNewlineAndPrefix(self) -> None:
        """从正文段切换到思考段时插入换行并追加思考前缀。"""
        raw = _RawWriterStub()
        renderer = StreamRenderer(raw_writer=raw)
        renderer.render(TextDelta(text="答"))
        out = renderer.render(ReasoningDelta(text="思"))
        assert out == "\n思考｜思"
        assert raw.pieces == ["答", "\n", "思考｜", "思"]

    def testNonDeltaEventsNoOutput(self) -> None:
        """非 Delta 事件（如 UsageEvent、DoneEvent）返回空字符串且不写入 raw_writer。"""
        from harness.llm import Usage
        raw = _RawWriterStub()
        renderer = StreamRenderer(raw_writer=raw)
        assert renderer.render(UsageEvent(usage=Usage(10, 20, 30))) == ""
        assert renderer.render(DoneEvent(finish_reason="stop")) == ""
        assert raw.pieces == []

    def testFinalizeBehavior(self) -> None:
        """有输出时 finalize 补换行；空输出时 finalize 不输出。"""
        raw_empty = _RawWriterStub()
        renderer_empty = StreamRenderer(raw_writer=raw_empty)
        assert renderer_empty.finalize() == ""
        assert raw_empty.pieces == []

        raw_active = _RawWriterStub()
        renderer_active = StreamRenderer(raw_writer=raw_active)
        renderer_active.render(TextDelta(text="回答"))
        assert renderer_active.finalize() == "\n"
        assert raw_active.pieces == ["回答", "\n"]
