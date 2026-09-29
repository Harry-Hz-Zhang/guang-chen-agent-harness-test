"""内置工具单元测试：calculator 安全求值、search/weather 预置数据、read_memory 记忆读取、write_todos 会话隔离待办。"""

import threading
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.memory.store import MemoryStore
from harness.state import CURRENT_SESSION_ID, RuntimeState
from harness.tools.base import ToolExecutionError
from harness.tools.calculator import CalculatorTool
from harness.tools.read_memory import ReadMemoryTool
from harness.tools.search import KNOWLEDGE_BASE, SearchTool
from harness.tools.todo import WriteTodosTool
from harness.tools.weather import WeatherTool

_WEATHER_TERMS: tuple[str, ...] = ("晴", "多云", "阴", "雨", "雪", "阵雨")


class _SystemCallRecorder:
    """os.system 的计数替身：记录调用次数，用于断言危险表达式零系统调用。"""

    def __init__(self) -> None:
        self.call_count: int = 0

    def __call__(self, command: str) -> int:
        """每次被调用时计数加一并返回 0。"""
        self.call_count += 1
        return 0


class TestCalculator:
    """覆盖 calculator 的运算优先级、幂运算、除零与危险表达式拒绝。"""

    def testArithmeticPrecedence(self) -> None:
        """2+3*4 按优先级求值得 "14"；parameters 为 object 型 JSON Schema 且必填 expression。"""
        tool = CalculatorTool()
        assert tool.name == "calculator"
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == ["expression"]
        assert tool.execute(expression="2+3*4") == "14"

    def testPower(self) -> None:
        """幂运算 2**10 求值得 "1024"。"""
        tool = CalculatorTool()
        assert tool.execute(expression="2**10") == "1024"

    def testDivisionByZero(self) -> None:
        """1/0 抛 ToolExecutionError 且消息含「除」字样。"""
        tool = CalculatorTool()
        with pytest.raises(ToolExecutionError, match="除"):
            tool.execute(expression="1/0")

    def testDangerousExpressionRejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """危险表达式（含函数调用）被白名单拒绝：抛 ToolExecutionError 且 os.system 零调用。"""
        recorder = _SystemCallRecorder()
        monkeypatch.setattr("os.system", recorder)
        tool = CalculatorTool()
        with pytest.raises(ToolExecutionError, match="不支持"):
            tool.execute(expression="__import__('os').system('dir')")
        assert recorder.call_count == 0

    def testFloatPowOverflowStructured(self) -> None:
        """2.0**2.0**1000 不裸抛 OverflowError，而是结构化 ToolExecutionError。"""
        tool = CalculatorTool()
        with pytest.raises(ToolExecutionError):
            tool.execute(expression="2.0**2.0**1000")

    def testDeepNestingStructured(self) -> None:
        """超长链式加法（解析期嵌套过深）不裸抛 RecursionError，而是结构化 ToolExecutionError。"""
        tool = CalculatorTool()
        with pytest.raises(ToolExecutionError, match="无法计算"):
            tool.execute(expression="1+" * 5000 + "1")

    def testHugePowExponentRejected(self) -> None:
        """10**10**5（内层指数结果 100000 超上限）被直接拒绝，不做大规模计算。"""
        tool = CalculatorTool()
        with pytest.raises(ToolExecutionError, match="指数"):
            tool.execute(expression="10**10**5")


class TestSearch:
    """覆盖 search 的关键词命中与未命中路径。"""

    def testKeywordHit(self) -> None:
        """查询命中预置关键词返回对应知识库文本；parameters 为 object 型 JSON Schema。"""
        tool = SearchTool()
        assert tool.name == "search"
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == ["query"]
        result = tool.execute(query="公司愿景")
        assert KNOWLEDGE_BASE["公司愿景"] in result
        assert "未找到" not in result

    def testKeywordMiss(self) -> None:
        """未预置的关键词返回含「未找到」的字符串，不抛异常。"""
        tool = SearchTool()
        result = tool.execute(query="量子纠缠")
        assert isinstance(result, str)
        assert "未找到" in result


class TestWeather:
    """覆盖 weather 的预置城市与未知城市路径。"""

    def testPresetCity(self) -> None:
        """预置城市返回含数字温度与天气词的文本；parameters 为 object 型 JSON Schema。"""
        tool = WeatherTool()
        assert tool.name == "weather"
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == ["city"]
        result = tool.execute(city="北京")
        assert "北京" in result
        assert any(ch.isdigit() for ch in result)
        assert any(term in result for term in _WEATHER_TERMS)

    def testUnknownCity(self) -> None:
        """未预置城市返回含「暂无」的说明，不抛异常。"""
        tool = WeatherTool()
        result = tool.execute(city="亚特兰蒂斯")
        assert isinstance(result, str)
        assert "暂无" in result


class TestReadMemoryTool:
    """覆盖 read_memory 的正常读取、未找到提示与非法文件名拒绝。"""

    @staticmethod
    def _make_tool(tmp_path: Path) -> ReadMemoryTool:
        """构造绑定了真实 MemoryStore 的 ReadMemoryTool，并预置一条记忆。"""
        memory_dir = tmp_path / "MEMORY"
        memory_dir.mkdir()
        (memory_dir / "20260928-143005.md").write_text(
            "日期：2026-09-28\n\n- 用户偏好简洁回复\n", encoding="utf-8"
        )
        return ReadMemoryTool(MemoryStore(tmp_path))

    def testReadExistingMemoryFile(self, tmp_path: Path) -> None:
        """按索引文件名读取记忆全文（含日期行）。"""
        tool = self._make_tool(tmp_path)
        content = tool.execute(file="20260928-143005.md")
        assert content.startswith("日期：")
        assert "- 用户偏好简洁回复" in content

    def testReadMissingMemoryFileFriendlyMessage(self, tmp_path: Path) -> None:
        """文件不存在返回友好提示字符串，不抛异常。"""
        tool = self._make_tool(tmp_path)
        result = tool.execute(file="nope.md")
        assert "未找到记忆文件" in result
        assert "nope.md" in result

    def testInvalidFileParamRaises(self, tmp_path: Path) -> None:
        """空文件名抛 ToolExecutionError。"""
        tool = self._make_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="file"):
            tool.execute(file="")

    def testNonMdExtensionRaises(self, tmp_path: Path) -> None:
        """非 .md 后缀抛 ToolExecutionError。"""
        tool = self._make_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="file"):
            tool.execute(file="MEMORY.md.txt")

    def testTraversalPathRejected(self, tmp_path: Path) -> None:
        """路径穿越形态（../ 与子目录）抛 ToolExecutionError，data_dir 外零读取。"""
        tool = self._make_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="file"):
            tool.execute(file="../state.json")
        with pytest.raises(ToolExecutionError, match="file"):
            tool.execute(file="a/b.md")

    def testNonStringParamRaises(self, tmp_path: Path) -> None:
        """非字符串参数抛 ToolExecutionError。"""
        tool = self._make_tool(tmp_path)
        with pytest.raises(ToolExecutionError, match="file"):
            tool.execute(file=None)


@contextmanager
def _session_context(session_id: str) -> Iterator[None]:
    """在测试内绑定会话上下文（结束后复位，不泄漏到其他用例）。"""
    token = CURRENT_SESSION_ID.set(session_id)
    try:
        yield
    finally:
        CURRENT_SESSION_ID.reset(token)


class TestWriteTodos:
    """覆盖 write_todos 的会话隔离、上下文绑定、入参校验与全量替换语义。"""

    def _make_tool(self, state: RuntimeState) -> WriteTodosTool:
        """构造绑定指定公共状态的 write_todos 工具。"""
        return WriteTodosTool(state)

    @staticmethod
    def _todo(content: str, status: str = "pending") -> dict:
        """构造一条合法待办项。"""
        return {"content": content, "status": status}

    def testSessionIsolatedState(self) -> None:
        """两会话各自写入互不可见：todos 按会话隔离。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"):
            tool.execute(todos=[self._todo("任务甲"), self._todo("任务乙")])
        with _session_context("s2"):
            tool.execute(todos=[self._todo("任务丙")])
        assert [t["content"] for t in state.todos("s1")] == ["任务甲", "任务乙"]
        assert [t["content"] for t in state.todos("s2")] == ["任务丙"]

    def testFullReplaceOverwrites(self) -> None:
        """同会话再次写入为全量替换，旧列表整体失效。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"):
            tool.execute(todos=[self._todo(f"旧任务{i}") for i in range(3)])
            tool.execute(todos=[self._todo("新任务A"), self._todo("新任务B")])
        assert [t["content"] for t in state.todos("s1")] == ["新任务A", "新任务B"]

    def testMissingSessionContextRejected(self) -> None:
        """未绑定会话上下文时结构化报错（match 会话）。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with pytest.raises(ToolExecutionError, match="会话"):
            tool.execute(todos=[self._todo("任务")])

    def testStatusEnumRejected(self) -> None:
        """非法 status 抛 ToolExecutionError（match status）。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"), pytest.raises(ToolExecutionError, match="status"):
            tool.execute(todos=[self._todo("任务", status="done")])

    def testEmptyContentRejected(self) -> None:
        """空白 content 抛 ToolExecutionError（match content）。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"), pytest.raises(
            ToolExecutionError, match="content"
        ):
            tool.execute(todos=[self._todo("   ")])

    def testNonListTodosRejected(self) -> None:
        """todos 非数组抛 ToolExecutionError（match 数组）。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"), pytest.raises(ToolExecutionError, match="数组"):
            tool.execute(todos="不是数组")

    def testNonDictItemRejected(self) -> None:
        """列表含非对象项抛 ToolExecutionError（match 项）。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"), pytest.raises(ToolExecutionError, match="项"):
            tool.execute(todos=[self._todo("任务甲"), "不是对象"])

    def testTooManyTodosRejected(self) -> None:
        """超过 100 条上限抛 ToolExecutionError（match 上限）。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"), pytest.raises(ToolExecutionError, match="上限"):
            tool.execute(todos=[self._todo(f"任务{i}") for i in range(101)])

    def testOverlongContentRejected(self) -> None:
        """单条 content 超 1000 字符抛 ToolExecutionError（match 超长）。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"), pytest.raises(ToolExecutionError, match="超长"):
            tool.execute(todos=[self._todo("长" * 1001)])

    def testEmptyListClears(self) -> None:
        """写入空列表清空当前会话待办，返回含「清空」。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"):
            tool.execute(todos=[self._todo("任务A"), self._todo("任务B")])
            result = tool.execute(todos=[])
        assert state.todos("s1") == []
        assert "清空" in result

    def testResultRendersStatus(self) -> None:
        """返回文本按编号与中文状态渲染待办。"""
        state = RuntimeState()
        tool = self._make_tool(state)
        with _session_context("s1"):
            result = tool.execute(todos=[
                self._todo("调研", "in_progress"),
                self._todo("写方案", "completed"),
                self._todo("收尾", "pending"),
            ])
        assert "1. [进行中] 调研" in result
        assert "2. [已完成] 写方案" in result
        assert "3. [待办] 收尾" in result

    def testConcurrentSessionsIsolated(self) -> None:
        """双线程各自绑定会话并发写入，结果按会话隔离、互不串扰。"""
        state = RuntimeState()
        tool = self._make_tool(state)

        def _worker(session_id: str, marker: str, errors: list[str]) -> None:
            """在线程内绑定会话并以递增清单连续全量替换 20 次。"""
            token = CURRENT_SESSION_ID.set(session_id)
            try:
                for i in range(20):
                    tool.execute(todos=[self._todo(f"{marker}{j}") for j in range(i + 1)])
            except Exception as exc:  # pragma: no cover - 记录线程内意外失败
                errors.append(f"{session_id}: {exc}")
            finally:
                CURRENT_SESSION_ID.reset(token)

        errors: list[str] = []
        threads = [
            threading.Thread(target=_worker, args=("s1", "甲", errors)),
            threading.Thread(target=_worker, args=("s2", "乙", errors)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert len(state.todos("s1")) == 20
        assert state.todos("s1")[0]["content"] == "甲0"
        assert len(state.todos("s2")) == 20
        assert state.todos("s2")[0]["content"] == "乙0"
