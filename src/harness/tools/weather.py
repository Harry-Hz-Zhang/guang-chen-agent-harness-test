"""WeatherTool —— 返回预置城市模拟天气数据的工具（不联网）。"""

from __future__ import annotations

from typing import Any

from harness.tools.base import BaseTool, ToolExecutionError

CITY_WEATHER: dict[str, tuple[int, str]] = {
    "北京": (22, "晴"),
    "上海": (26, "多云"),
    "广州": (31, "阵雨"),
    "深圳": (30, "晴"),
}


class WeatherTool(BaseTool):
    """天气查询工具：仅覆盖预置城市，返回温度与天气状况的模拟数据。

    未预置的城市返回含「暂无」的说明字符串（附当前支持的城市
    列表），不抛异常。
    """

    name: str = "weather"
    description: str = "天气查询：返回指定城市当前的温度与天气状况（预置城市模拟数据）"
    parameters: dict = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "要查询天气的城市名称，例如 北京",
            },
        },
        "required": ["city"],
    }

    def execute(self, **kwargs: Any) -> str:
        """查询预置城市天气；未预置城市返回「暂无」说明，不抛异常。"""
        city = kwargs.get("city")
        if not isinstance(city, str) or not city.strip():
            raise ToolExecutionError(
                "参数 city 必须为非空字符串",
                tool=self.name,
                tool_args=kwargs,
            )
        preset = CITY_WEATHER.get(city)
        if preset is None:
            supported = "、".join(CITY_WEATHER)
            return f"暂无城市「{city}」的天气数据（当前支持：{supported}）"
        temperature, condition = preset
        return f"{city}当前天气：{condition}，气温 {temperature}℃（模拟数据）"
