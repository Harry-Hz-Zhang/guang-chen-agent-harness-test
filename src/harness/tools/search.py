"""SearchTool —— 基于预置知识库的关键词检索工具（模拟搜索，不联网）。"""

from __future__ import annotations

from typing import Any

from harness.tools.base import BaseTool, ToolExecutionError

KNOWLEDGE_BASE: dict[str, str] = {
    "公司愿景": "让每一位开发者都拥有可信赖的 AI 助手运行时。",
    "主营业务": "企业级 Agent 运行时与开发者工具的研发与技术服务。",
    "联系方式": "客服邮箱 support@example.com，服务热线 400-000-0000。",
    "办公地点": "总部位于北京市海淀区中关村软件园二期。",
    "核心价值观": "诚实守信、按时交付、长期主义。",
    "发展历程": "2020 年成立，2023 年发布第一代 Agent 运行时。",
}


class SearchTool(BaseTool):
    """知识库检索工具：在预置的中文关键词知识库里做互相包含匹配。

    匹配规则：遍历知识库键，查询词包含某键、或某键包含查询词即
    视为命中；命中多条时逐条拼接返回；全部未命中时返回含
    「未找到」的提示字符串，不抛异常。
    """

    name: str = "search"
    description: str = "知识库检索：按关键词查询公司愿景、主营业务、联系方式、办公地点等预置信息"
    parameters: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索关键词，例如 公司愿景",
            },
        },
        "required": ["query"],
    }

    def execute(self, **kwargs: Any) -> str:
        """按互相包含匹配规则检索预置知识库并返回结果文本；未命中不抛异常。"""
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolExecutionError(
                "参数 query 必须为非空字符串",
                tool=self.name,
                tool_args=kwargs,
            )
        hits = [
            f"{key}：{value}"
            for key, value in KNOWLEDGE_BASE.items()
            if key in query or query in key
        ]
        if not hits:
            keywords = "、".join(KNOWLEDGE_BASE)
            return f"未找到与「{query}」相关的内容（可用关键词：{keywords}）"
        return "\n".join(hits)
