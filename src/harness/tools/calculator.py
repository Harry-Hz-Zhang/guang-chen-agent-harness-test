"""CalculatorTool —— 基于 ast 白名单的算术表达式安全求值工具，绝不执行任意代码。"""

from __future__ import annotations

import ast
import math
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
}

_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_EVAL_ERROR_MESSAGES: dict[type[BaseException], str] = {
    ZeroDivisionError: "除数为零：除法、整除或取模运算的分母不能为 0",
    RecursionError: "表达式嵌套过深",
    OverflowError: "计算溢出，结果超出可表示范围",
    ValueError: "计算过程出现非法值",
    TypeError: "计算过程出现不支持的类型组合",
}


class _UnsupportedNode(ValueError):
    """内部信号：表达式含白名单外元素，element 为中文元素描述，由 execute 统一转结构化错误。"""

    def __init__(self, element: str) -> None:
        super().__init__(element)
        self.element: str = element


class _PowExponentTooLarge(ValueError):
    """内部信号：幂运算指数绝对值超过 MAX_EXPONENT，exponent 为指数字符串描述。"""

    def __init__(self, exponent: str) -> None:
        super().__init__(exponent)
        self.exponent: str = exponent


class CalculatorTool(BaseTool):
    """算术计算器工具：先把表达式解析成语法树，再按白名单递归求值。

    白名单仅覆盖数字常量（int/float，布尔拒绝）、二元运算
    + - * / // % **、一元正负号；函数调用、变量名、属性访问、
    下标等任何其他语法元素一律拒绝，全程不使用 eval/exec。
    幂运算指数绝对值超过 MAX_EXPONENT 时先拒绝再计算，防止
    构造超大整数耗尽内存；复数与非有限（inf/NaN）结果同样
    拒绝；解析、求值、字符串转换各阶段的异常一律转为
    ToolExecutionError，不向调用方裸抛任何原始异常。
    """

    name: str = "calculator"
    description: str = "算术计算器：对四则运算、整除、取模与幂运算表达式安全求值并返回结果"
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
        """对入参 expression 安全求值并返回结果字符串。

        语法错误、嵌套过深、白名单外元素、指数过大、除零、
        溢出与结果越界（复数/无穷/NaN/超长整数）统一抛
        ToolExecutionError（中文消息、携带工具名与入参）。
        """
        expression = kwargs.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ToolExecutionError(
                "参数 expression 必须为非空字符串",
                tool=self.name,
                tool_args=kwargs,
            )
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ToolExecutionError(
                f"表达式语法错误：{expression}",
                tool=self.name,
                tool_args={"expression": expression},
                original=exc,
            ) from exc
        except RecursionError as exc:
            raise ToolExecutionError(
                "表达式嵌套过深，无法解析",
                tool=self.name,
                tool_args={"expression": expression},
                original=exc,
            ) from exc
        except ValueError as exc:
            raise ToolExecutionError(
                f"表达式无法解析：{expression}",
                tool=self.name,
                tool_args={"expression": expression},
                original=exc,
            ) from exc
        try:
            value = self._evaluate(tree.body)
        except _PowExponentTooLarge as exc:
            raise ToolExecutionError(
                f"指数过大，拒绝计算（指数绝对值上限 {MAX_EXPONENT}，"
                f"当前指数约为 {exc.exponent}）",
                tool=self.name,
                tool_args={"expression": expression},
                original=exc,
            ) from exc
        except _UnsupportedNode as exc:
            raise ToolExecutionError(
                f"不支持的表达式：{exc.element}"
                "（仅支持整数、小数与 + - * / // % ** 运算）",
                tool=self.name,
                tool_args={"expression": expression},
                original=exc,
            ) from exc
        except (
            ZeroDivisionError,
            OverflowError,
            RecursionError,
            ValueError,
            TypeError,
        ) as exc:
            message = _EVAL_ERROR_MESSAGES.get(
                type(exc), "计算过程发生错误"
            )
            raise ToolExecutionError(
                f"{message}：{expression}",
                tool=self.name,
                tool_args={"expression": expression},
                original=exc,
            ) from exc
        if isinstance(value, complex):
            raise ToolExecutionError(
                "计算结果超出可表示范围（复数结果不支持）",
                tool=self.name,
                tool_args={"expression": expression},
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ToolExecutionError(
                "计算结果超出可表示范围（无穷或 NaN）",
                tool=self.name,
                tool_args={"expression": expression},
            )
        try:
            return str(value)
        except (ValueError, OverflowError) as exc:
            raise ToolExecutionError(
                "计算结果位数过长，超出可转换范围",
                tool=self.name,
                tool_args={"expression": expression},
                original=exc,
            ) from exc

    def _evaluate(self, node: ast.AST) -> int | float | complex:
        """按白名单递归求值单个语法树节点，白名单外或指数越界抛内部信号异常。"""
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(
                node.value, (int, float)
            ):
                raise _UnsupportedNode(f"常量 {node.value!r}")
            return node.value
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Pow):
                exponent = self._evaluate(node.right)
                if abs(exponent) > MAX_EXPONENT:
                    raise _PowExponentTooLarge(_describe_number(exponent))
                return self._evaluate(node.left) ** exponent
            op = _BIN_OPERATORS.get(type(node.op))
            if op is None:
                raise _UnsupportedNode(f"运算符 {type(node.op).__name__}")
            return op(self._evaluate(node.left), self._evaluate(node.right))
        if isinstance(node, ast.UnaryOp):
            op = _UNARY_OPERATORS.get(type(node.op))
            if op is None:
                raise _UnsupportedNode(f"一元运算符 {type(node.op).__name__}")
            return op(self._evaluate(node.operand))
        raise _UnsupportedNode(f"语法元素 {type(node).__name__}")


def _describe_number(value: int | float | complex) -> str:
    """把指数数值转为短字符串描述，超大值降级为数量级提示。"""
    try:
        text = str(value)
    except (ValueError, OverflowError):
        return "超出显示范围的数"
    if len(text) > 30:
        return f"{text[:15]}...（共 {len(text)} 位）"
    return text
