# Proposal — 简化工具集：精简 CalculatorTool + 删除 TodoTool

## 1. 背景与动机

### 1.1 calculator 过度复杂

PRD（[`doc/PRD.md`](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/doc/PRD.md) L17-23）对 calculator 的全部要求只有一行：作为「至少三个工具」之一被 LLM 调用。当前 [`src/harness/tools/calculator.py`](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/src/harness/tools/calculator.py) 却长到 205 行，堆叠了 2 个私有信号异常类、5 类异常文案映射、三层异常处理、复数/inf/NaN/str 转换收尾检查（后者接近死代码）——约 150 行是 PRD 外自加戏。用户定位明确：简单加减乘除，或多一些运算方式。

### 1.2 todo 属于凑数工具，且拖出一串连带机制

PRD 对第三类工具的表述是「read_docs / todo / weather（**可自定义**）」，todo 只是备选之一。当前实现不仅有一个 150 行的 [`src/harness/tools/todo.py`](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/src/harness/tools/todo.py)，还在 [`src/harness/__main__.py`](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/src/harness/__main__.py) 拖出一串只为它服务的机制：

- `RebindableTodoTool` 包装类（会话切换时换绑存储文件）；
- `run_repl` 的 `on_session_change` 回调参数及 3 处调用点（初始 / `/new` / `/switch`）；
- `main()` 里的 `session_ref` 共享字典 + 回调闭包；
- `_build_registry` 的 `config` 与 `session_ref` 两个参数（均只服务 todo）。

工具已有 calculator / search / weather / read_memory 四个，超过 PRD「至少三个」；todo 删除后这串机制全部成为死代码，应连根拔掉。

## 2. 目标

### 2.1 calculator 简化

1. 重写 `src/harness/tools/calculator.py` 至 **≤ 100 行**（当前 205 行），核心保持「`ast.parse` 解析 + 白名单递归求值」最短安全路径。
2. **对外接口零变更**：`name` / `parameters`（expression 字符串）/ `execute(expression=...) -> str` 不动 —— `test_loop.py` 8 个用例、`__main__.py` 注册、`tools/__init__.py` 导出无需改动。
3. 运算集：`+ - * / // % **` 与一元正负号；常量仅 int/float（bool 精确拒绝）。
4. 异常处理收敛：入参校验 1 处 + `ZeroDivisionError` 专案 + 其余异常一条 broad except 转 `ToolExecutionError`（链上 `original`）。

### 2.2 todo 删除（含连带机制）

5. 删除 `src/harness/tools/todo.py` 整个文件、`tools/__init__.py` 的 `TodoTool` 导出。
6. `__main__.py` 删除 `RebindableTodoTool` 类、`on_session_change` 参数与全部调用点、`session_ref` 与回调闭包；`_build_registry` 简化为 `(memory)` 单参（read_memory 仍需 memory），注册 calculator / search / weather / read_memory 四个工具。
7. `/new`、`/switch`、`/sessions`、`/history` 命令本身**全部保留**（会话切换与历史概览是独立能力，与 todo 无关）。

### 2.3 测试与文档同步

8. `tests/tools/test_builtin_tools.py`：`TestCalculator` 删 2 个超边界用例、放宽 1 处 match；`TestTodo` 整类删除。
9. `tests/test_cli.py`：删除 3 个只为回调/换绑存在的用例，`testCommandSwitchCurrentSessionIdempotent` 去掉 callback 断言后保留，`testRegistryIncludesReadMemory` 适配新 `_build_registry(memory)` 签名并删除 todo 断言。
10. 同步 CODEGRAPH.md 与 README.md 中 todo 相关描述。

## 3. 非目标（Non-goals）

1. 不改 calculator 的 `expression` 字符串入参接口（不改成 `a / op / b` 三参数式）。
2. 不移除 `ast` 白名单、不引入 `eval`；不删 `MAX_EXPONENT` 指数上限（`9**9**9` 挂死进程是真实可达输入）。
3. 不动 search / weather 工具，不动 loop / parser / registry / session / memory 层。
4. 不删 `/switch`、`/new` 等会话命令本身，只删其上专为 todo 挂的回调钩子。
5. 不修改 `doc/PRD.md`。

## 4. 验收标准

1. **calculator 体量**：`calculator.py` ≤ 100 行；不再包含 `_UnsupportedNode` / `_PowExponentTooLarge` / `_EVAL_ERROR_MESSAGES` / `_describe_number`。
2. **todo 痕迹清零**：`src/` 与 `tests/` 中不再出现 `TodoTool` / `RebindableTodoTool` / `on_session_change` / `session_ref`；`_build_registry(memory)` 注册恰 4 个工具（calculator / search / weather / read_memory）。
3. **行为保持**：`TestCalculator` 保留的 6 个原样用例全部通过；`TestSessionSwitch` 保留的用例（去掉 callback 断言后的幂等用例在内）与 `testRegistryIncludesReadMemory` 全部通过；`test_loop.py` 零改动。
4. **全量回归**：`uv run pytest` 除既有失败 `testMiddlewareHooksInvoked`（源自 `loop.py` 未提交的中间件重构，与本 change 无关、不处理）外全绿（178 → 169 项：calculator 删 2 + TestTodo 删 4 + test_cli 删 3）。
5. **文档同步**：CODEGRAPH.md 删除 todo.py 行；README.md 去掉 `todos/<id>.json` 与工具清单中的 todo 字样；AGENTS.md 目录树用「… 其余工具」概括、无需改。
6. **图谱同步**：apply 完成后执行 `codegraph sync` 成功。
