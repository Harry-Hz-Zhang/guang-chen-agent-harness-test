# Design — 工具集简化设计（calculator 精简 + todo 删除）

## 1. explore 结论（基于 codegraph 与全量测试）

| 事实 | 证据 |
| --- | --- |
| PRD 仅要求 calculator 作为三工具之一 | `doc/PRD.md` L17-23，无任何防御深度条款 |
| calculator 现实现 205 行，防御机制占 ~150 行 | `src/harness/tools/calculator.py`：2 个私有异常类 + 5 类文案映射 + 三层异常处理 + 复数/非有限/str 转换收尾检查 + `_describe_number` |
| calculator 外部依赖全部只走 `expression` 接口 | codegraph callers：`tools/__init__.py`、`__main__.py:_build_registry`、`test_loop.py` 8 个用例（仅用 `"2+3*4"` / `"16*2"` / `"1+1"` / `"2+2"`） |
| PRD 对第三类工具的表述是「可自定义」备选 | `doc/PRD.md` L21-22：read_docs / todo / weather 三选一及以上即可；删 todo 后 calculator + search + weather 恰好 3 个，仍满足「至少三个」 |
| todo 连带机制全在 `__main__.py` | `RebindableTodoTool`（L49-74）、`on_session_change` 参数 + 3 处调用点、`session_ref` 字典与回调闭包、`_build_registry` 的 config/session_ref 参数（二者均只服务 todo；memory 参数服务 read_memory，保留） |
| todo 测试面 | `test_builtin_tools.py` `TestTodo` 4 用例；`test_cli.py` 3 个用例专为回调/换绑存在（testOnSessionChangeCallbackInvoked / testRebindableTodoToolSwitchesSession / testCommandSwitchInvokesCallback），1 个用例带 callback 断言可裁剪保留（testCommandSwitchCurrentSessionIdempotent），`testRegistryIncludesReadMemory` 需适配新签名并删 todo 断言 |
| /new 路由行为有独立覆盖 | `test_cli.py` `testCommandNew`（L95-107）不依赖 callback，删除回调用例不损失 /new 路由覆盖 |
| 基线 | `uv run pytest` 178 项，其中 1 项既有失败（`testMiddlewareHooksInvoked`，源自 `loop.py` 未提交的中间件重构，与本 change 无关、不处理）；当前 `_build_registry(config, session_ref, memory)` 注册 5 工具（含 read_memory） |
| 文档引用 | CODEGRAPH.md L155（todo.py 行）、README.md L38（todos/<id>.json）与 L70（工具清单含 todo）；AGENTS.md 目录树以「… 其余工具」概括，无需改 |

## 2. 模块划分与职责

```text
src/harness/tools/calculator.py   [MOD] 重写为 ~90 行简化版
src/harness/tools/todo.py         [DEL] 整文件删除
src/harness/tools/__init__.py     [MOD] 移除 TodoTool 导出与 __all__ 条目
src/harness/__main__.py           [MOD] 删 RebindableTodoTool / on_session_change /
                                       session_ref；_build_registry 简化为无参
tests/tools/test_builtin_tools.py [MOD] TestCalculator 删 2 用例放宽 1 match；TestTodo 整类删除
tests/test_cli.py                 [MOD] 删 3 用例、1 用例去 callback 断言
CODEGRAPH.md / README.md          [MOD] 移除 todo 相关描述
```

`test_loop.py`、`loop.py`、`registry.py`、session / memory 层零改动。

## 3. Part A —— calculator 简化后实现骨架（205 行 → ~90 行）

```python
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
    超过 MAX_EXPONENT 先拒绝再做幂运算；任何失败统一转为
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
```

要点：

- `type(node.value) in (int, float)` 精确类型判断天然拒绝 `bool`（`True` 是 `int` 子类但 `type` 不等），替代原 4 行判断。
- 白名单外元素直接 `raise ValueError(...)`，由 `execute` 顶部唯一 broad except 统一包装 —— 两个私有信号异常类整体消失。
- `ZeroDivisionError` 单独列出：最高频用户可见错误，值得一条干净中文消息，同时保住既有用例断言。
- 幂指数上限压缩为 1 个 `if`；`abs(right)` 对 complex 操作数抛 `TypeError`，也被 broad except 兜住。

## 4. Part A —— 既有 9 用例逐条推演

| 用例 | 断言 | 简化后 | 处置 |
| --- | --- | --- | --- |
| testArithmeticPrecedence | `"2+3*4"` → `"14"` | ✅ 白名单求值不变 | 原样保留 |
| testPower | `"2**10"` → `"1024"` | ✅ 指数 10 ≤ 1000 | 原样保留 |
| testDivisionByZero | match `"除"` | ✅ 专案消息「除数为零」 | 原样保留 |
| testDangerousExpressionRejected | match `"不支持"` + `os.system` 零调用 | ✅ 消息含「不支持的语法元素 Call」；无 eval | 原样保留 |
| testFloatPowOverflowStructured | `2.0**2.0**1000` 抛结构化错误 | ✅ 外层指数 `2.0**1000≈1.07e301 > 1000` 先拒 | 原样保留 |
| testHugePowExponentRejected | match `"指数"` | ✅ 消息「指数过大（上限 1000）」 | 原样保留 |
| testDeepNestingStructured | match `"嵌套"` | ⚠️ 新消息「表达式无法计算：…（maximum recursion depth exceeded…）」 | **放宽 match 为 `"无法计算"`** |
| testComplexResultRejected | `(-8)**0.5` 抛错 match `"范围"` | ❌ 简化版返回复数字符串 | **删除**（接受复数结果原样返回） |
| testNonFiniteResultRejected | `1e308*10` 抛错 match `"范围"` | ❌ 简化版返回 `"inf"` | **删除**（接受非有限结果原样返回） |

被放弃的行为及理由：复数 / inf 结果对「简单计算器」是无害输出（LLM 能读懂并自行说明）；为它们维持 20+ 行收尾检查属于 PRD 外自加戏。

## 5. Part B —— todo 删除清单（含连带机制）

### 5.1 生产代码

| 位置 | 动作 |
| --- | --- |
| `src/harness/tools/todo.py` | 整文件删除（~150 行） |
| `src/harness/tools/__init__.py` | 删 `from harness.tools.todo import TodoTool` 与 `__all__` 中的 `"TodoTool"` |
| `src/harness/__main__.py` L32 | 删 `TodoTool` 导入 |
| `src/harness/__main__.py` L49-74 | 删 `RebindableTodoTool` 整类 |
| `src/harness/__main__.py` `run_repl` 签名 | 删 `on_session_change` 参数；删 3 处调用点（初始会话 L111-112、`/new` L135-136、`/switch` L170-171） |
| `src/harness/__main__.py` `main()` | 删 `session_ref` 字典（L255）与 `on_session_change` 闭包（L257-258）；`run_repl(...)` 调用去掉该实参 |
| `src/harness/__main__.py` `_build_registry` | 签名从 `(config, session_ref, memory)` 改为 `(memory)`（config 与 session_ref 只服务 todo，memory 服务 read_memory），函数体注册 Calculator / Search / Weather / ReadMemory 四个工具，docstring 同步 |
| `src/harness/__main__.py` 模块 docstring | 无需大改（未提及 todo） |

`/new` `/switch` `/sessions` `/history` 命令行为本身完全不变（切换后 `loop.run` 的 session_id 参数即完成会话路由，本就不依赖回调）。

### 5.2 测试

| 用例 | 处置 | 理由 |
| --- | --- | --- |
| `TestTodo` 4 用例（testAddAndList / testSessionIsolation / testPersistence / testInvalidSessionIdRejected） | **整类删除** | 被测对象消失 |
| `test_builtin_tools.py` 模块 docstring | 改写 | 去掉「todo 会话隔离与持久化」字样 |
| `test_cli.py#testOnSessionChangeCallbackInvoked` | **删除** | 回调唯一目的是 todo 换绑；/new 路由行为已由 `testCommandNew`（L95-107，零 callback）覆盖 |
| `test_cli.py#testRebindableTodoToolSwitchesSession` | **删除** | 被测类消失 |
| `test_cli.py#testCommandSwitchInvokesCallback` | **删除** | 回调参数消失 |
| `test_cli.py#testCommandSwitchCurrentSessionIdempotent` | **裁剪保留** | 去掉 `on_session_change=callback` 实参与 2 条 callback 断言，保留「已切换会话 s1」幂等提示与 `loop.run` 路由断言 |
| `test_cli.py#testRegistryIncludesReadMemory` | **适配调整** | 调用改为 `_build_registry(memory)` 单参；删除 `assert "todo" in names`；保留 read_memory / calculator / search / weather 断言 |
| `TestSessionSwitch` 其余用例 | 原样保留 | 不依赖回调 |

### 5.3 文档

| 文件 | 动作 |
| --- | --- |
| CODEGRAPH.md L155 | 删除 todo.py 那一行 |
| README.md L38 | 运行期产物清单去掉 `todos/<id>.json`（待办） |
| README.md L70 | 工具清单 `calculator(ast 白名单)/search/weather/todo` 去掉 `/todo` |
| AGENTS.md | 无需改（目录树以「… 其余工具」概括，未点名 todo.py） |

### 5.4 数据兼容性说明

已有 `data/todos/*.json` 遗留文件不删除不迁移（已 gitignore、无代码再引用），自然搁置。

## 6. 被否决的备选方案与原因

### 备选 1（calculator）：改成 `a / op / b` 三参数单步运算
- 更「简单」，但 `test_loop.py` 8 个用例与提示词示例全部基于 expression 字符串，接口变更波及 loop 层；多步表达式（`2+3*4`）是演示 LLM 自主工具决策的更好载体。

### 备选 2（calculator）：直接 `eval(expression)` + 正则预校验
- 行数最少，但 AGENTS.md §4 要求对 LLM 产出表达式显式校验；正则 + eval 存在绕过空间。`ast` 白名单递归求值本身仅 ~25 行，是「安全」与「简短」的最优交点。

### 备选 3（calculator）：连 `MAX_EXPONENT` 也删
- 少 3 行，但 `9**9**9`（3.7 亿位整数）会实际挂死 agent 进程 —— LLM 完全可能生成的合法表达式，属运行时真实可达输入。保留。

### 备选 4（calculator）：保留复数 / inf/NaN 收尾检查
- 简化幅度不足（约只能减 60 行），`str(value)` 转换期 try/except 仍属死代码。不彻底。

### 备选 5（todo）：保留 `on_session_change` 作为通用扩展钩子
- 「未来可能有别的组件需要感知会话切换」是假想需求（AGENTS.md 反对为假设场景设计）；删除后若真有需要，加回一个参数的成本极低。当前留着它就是死代码 + 3 个只为它存在的测试。

### 备选 6（todo）：保留 todo.py 文件、仅从注册表摘除
- 留着不被注册的工具文件与 4 个测试用例纯属仓库噪音；用户明确「不需要再凑数」。连根拔。
