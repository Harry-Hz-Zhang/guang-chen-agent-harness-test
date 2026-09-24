"""内置工具单元测试：calculator ast 白名单安全求值、search/weather 预置数据、todo 会话隔离与持久化。"""

from pathlib import Path

import pytest

from harness.tools.base import ToolExecutionError
from harness.tools.calculator import CalculatorTool
from harness.tools.search import KNOWLEDGE_BASE, SearchTool
from harness.tools.todo import TodoTool
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
        """超长链式加法（解析期嵌套过深）不裸抛 RecursionError，而是结构化错误。"""
        tool = CalculatorTool()
        with pytest.raises(ToolExecutionError, match="嵌套"):
            tool.execute(expression="1+" * 5000 + "1")

    def testHugePowExponentRejected(self) -> None:
        """10**10**5（内层指数结果 100000 超上限）被直接拒绝，不做大规模计算。"""
        tool = CalculatorTool()
        with pytest.raises(ToolExecutionError, match="指数"):
            tool.execute(expression="10**10**5")

    def testComplexResultRejected(self) -> None:
        """(-8)**0.5 产生复数结果时抛结构化错误，不返回复数字符串。"""
        tool = CalculatorTool()
        with pytest.raises(ToolExecutionError, match="范围"):
            tool.execute(expression="(-8)**0.5")

    def testNonFiniteResultRejected(self) -> None:
        """1e308*10 溢出到 inf 时抛结构化错误，不返回 "inf"。"""
        tool = CalculatorTool()
        with pytest.raises(ToolExecutionError, match="范围"):
            tool.execute(expression="1e308*10")


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


class TestTodo:
    """覆盖 todo 的添加列出、会话隔离与持久化。"""

    def testAddAndList(self, tmp_path: Path) -> None:
        """add 返回含编号 1 的提示、list_todos 返回该待办；execute 按 action 分发两动作。"""
        tool = TodoTool(data_dir=tmp_path, session_id="s1")
        assert tool.name == "todo"
        assert tool.parameters["type"] == "object"
        assert tool.parameters["properties"]["action"]["enum"] == ["add", "list"]
        assert tool.parameters["required"] == ["action"]
        added = tool.add(todo="写周报")
        assert "1" in added
        assert "写周报" in added
        assert tool.list_todos() == ["写周报"]
        assert "写周报" in tool.execute(action="list")
        second = tool.execute(action="add", todo="开会")
        assert "2" in second
        assert tool.list_todos() == ["写周报", "开会"]

    def testSessionIsolation(self, tmp_path: Path) -> None:
        """同目录不同 session 的待办互不可见：s2 列出为空列表（长度 0，非 None）。"""
        s1 = TodoTool(data_dir=tmp_path, session_id="s1")
        s1.add(todo="写周报")
        s1.add(todo="回邮件")
        s2 = TodoTool(data_dir=tmp_path, session_id="s2")
        todos = s2.list_todos()
        assert todos is not None
        assert len(todos) == 0

    def testPersistence(self, tmp_path: Path) -> None:
        """新建同目录同会话实例（模拟重启）仍能按原顺序读回全部待办。"""
        first = TodoTool(data_dir=tmp_path, session_id="s1")
        first.add(todo="写周报")
        first.add(todo="回邮件")
        restarted = TodoTool(data_dir=tmp_path, session_id="s1")
        assert restarted.list_todos() == ["写周报", "回邮件"]

    def testInvalidSessionIdRejected(self, tmp_path: Path) -> None:
        """含 Windows 保留字符（冒号等）的 session_id 在构造期即被拒绝：抛 ToolExecutionError。"""
        with pytest.raises(ToolExecutionError, match="session_id"):
            TodoTool(data_dir=tmp_path, session_id="a:b")
