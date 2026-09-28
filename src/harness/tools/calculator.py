"""CalculatorTool —— 基于 ast 白名单的简易算术求值工具，不使用 eval/exec。"""

from __future__ import annotations

import ast
import operator
from typing import Any, Callable

from harness.tools.base import BaseTool, ToolExecutionError

MAX_EXPONENT: int = 1000

_BIN_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class CalculatorTool(BaseTool):
    """简易算术计算器：把表达式解析成语法树后按白名单递归求值。

    仅支持 int/float 常量与 + - * / // % ** 及一元正负号；
    函数调用、变量名等其他语法元素一律拒绝；幂指数绝对值
    超过 MAX_EXPONENT 时先拒绝再做幂运算；任何失败统一转为
    ToolExecutionError 结构化回传，不向调用方裸抛异常。
    """

    name: str = "calculator"
    description: str = "简易计算器：对 + - * / // % ** 算术表达式安全求值并返回结果"
    parameters: dict = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的算术表达式，例如 2+3*4",
            },
        },
        "required": ["expression"],
    }

    def execute(self, **kwargs: Any) -> str:
        """对入参 expression 安全求值并返回结果字符串，失败统一抛 ToolExecutionError。"""
        expression = kwargs.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ToolExecutionError(
                "参数 expression 必须为非空字符串",
                tool=self.name,
                tool_args=kwargs,
            )
        try:
            tree = ast.parse(expression, mode="eval")
            value = self._evaluate(tree.body)
        except ZeroDivisionError as exc:
            raise ToolExecutionError(
                f"除数为零：{expression}",
                tool=self.name,
                tool_args={"expression": expression},
                original=exc,
            ) from exc
        except (
            SyntaxError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as exc:
            raise ToolExecutionError(
                f"表达式无法计算：{expression}（{exc}）",
                tool=self.name,
                tool_args={"expression": expression},
                original=exc,
            ) from exc
        return str(value)

    def _evaluate(self, node: ast.AST) -> int | float | complex:
        """按白名单递归求值单个语法树节点，白名单外元素抛 ValueError。"""
        if isinstance(node, ast.Constant):
            if type(node.value) in (int, float):
                return node.value
            raise ValueError(f"不支持的常量 {node.value!r}")
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPERATORS:
            left = self._evaluate(node.left)
            right = self._evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
                raise ValueError(f"指数过大（上限 {MAX_EXPONENT}）")
            return _BIN_OPERATORS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            return _UNARY_OPERATORS[type(node.op)](self._evaluate(node.operand))
        raise ValueError(f"不支持的语法元素 {type(node).__name__}")
