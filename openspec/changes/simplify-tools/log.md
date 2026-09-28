# Log — simplify-tools

## 2026-09-28 explore（第一轮：calculator）

| 事件 | 说明 |
| --- | --- |
| 探索启动 | 用户反馈 calculator 实现过度复杂：PRD 只要求简单加减乘除（或多几种运算），要求走 VSDD explore 流程产出简化 change 供审核 |
| PRD 核实 | `doc/PRD.md` L17-23 对 calculator 仅有「至少三个工具之一」的要求，无任何防御深度条款；现状 205 行中防御机制约占 150 行 |
| codegraph callers/impact | `CalculatorTool` 调用方全部只依赖 `expression` 字符串接口（`tools/__init__.py`、`__main__._build_registry`、`test_loop.py` 8 用例）→ 接口不变则外围零改动 |
| 用例推演 | 9 个 TestCalculator 用例：6 个断言简化后原样成立；testDeepNestingStructured 需放宽 match；2 个边界用例随行为放弃删除（design.md §4 推演表） |
| 基线确认 | `uv run pytest` 165 项全绿 |

## 2026-09-28 explore（第二轮：todo 删除并入同 change）

| 事件 | 说明 |
| --- | --- |
| 需求追加 | 用户指示：同一 change 内删除 `harness/tools/todo.py`（工具已够多，不凑数），连带部分一并清理，change 更名为 simplify-tools |
| PRD 合规确认 | PRD 对第三类工具表述为「read_docs / todo / weather（可自定义）」——todo 是备选非必需；删除后 calculator + search + weather 恰好满足「至少三个」 |
| codegraph callers/impact | `TodoTool` 影响面：todo.py 自身 + `__main__.py`（导入、`RebindableTodoTool`、`_build_registry`）+ `tools/__init__.py` + 两个测试文件 |
| 连带机制盘点 | `__main__.py` 中专为 todo 存在的机制：`RebindableTodoTool` 类（L49-74）、`run_repl.on_session_change` 参数与 3 处调用点、`main()` 的 `session_ref` 字典与回调闭包、`_build_registry` 的 config/session_ref 两参数（均只服务 todo）→ 一并删除，`_build_registry` 简化为无参 |
| 测试面盘点 | `TestTodo` 4 用例删除；test_cli.py 3 个回调用例删除；`testCommandSwitchCurrentSessionIdempotent` 裁剪 callback 断言后保留（幂等提示 + 路由断言仍有价值）；/new 路由覆盖由 `testCommandNew`（零 callback）独立保证 |
| 文档引用 | CODEGRAPH.md L155、README.md L38/L70 含 todo 描述需同步；AGENTS.md 目录树以「… 其余工具」概括无需改 |
| 数据兼容 | `data/todos/*.json` 遗留文件已 gitignore、删除后无代码引用，自然搁置不迁移 |
| 阻塞 | 无（等待用户审核合并后的方案，未获确认不进 apply） |

## 2026-09-28 propose

| 事件 | 说明 |
| --- | --- |
| change 更名 | `simplify-calculator-tool` → `simplify-tools`（目录 mv，artifacts 全部重写覆盖两部分） |
| 复杂度判定 | 🟡 standard（calculator 纯减法重构 🟢 + todo 连根拔涉及 `__main__.py` 装配层与 2 个测试文件 🟡，拆 2 个 task） |
| artifacts | proposal.md（目标 10 条 + 非目标 5 条 + 验收 6 条）→ design.md（explore 证据表 + calculator 骨架与 9 用例推演 + todo 删除三张清单 + 6 个被否决备选）→ tasks.md（Task 1 calculator 精简 / Task 2 todo 删除，后者 RED 以两条接口断言作可执行规格） |
| 关键决策 | ①calculator 接口零变更；②保留 ast 白名单与 MAX_EXPONENT；③todo 连带机制全删（含 on_session_change 钩子——为假想扩展保留死代码违反公约）；④/switch 等会话命令保留，仅摘掉 todo 专挂的回调；⑤测试净变化 165 → 156（-2 calculator 边界 -4 TestTodo -3 test_cli 回调用例） |
| HARD-GATE | 用户要求「写完我审核你的方案」——propose 后停在审核关口，未获确认不得进入 apply |

## 2026-09-28 apply

| 事件 | 说明 |
| --- | --- |
| 授权 | 用户确认：on_session_change 钩子整体删除、遗留数据文件搁置、连带部分清零；要求每完成一部分代码后用 sub-agent 审核 |
| 状态校准 | apply 前重查工作区发现代码库较 explore 时已演进：`_build_registry` 已含 read_memory（三参、5 工具），且 `loop.py` 有用户未提交的中间件重构（致 `testMiddlewareHooksInvoked` 既有失败）；据此校准 proposal/design/tasks 中的签名与计数（178 项基线、删后 169 项、`_build_registry(memory)` 4 工具） |
| Task 1 RED | TestCalculator 删 2 边界用例、testDeepNestingStructured match 放宽为「无法计算」→ 恰 1 项失败，其余全绿 |
| Task 1 GREEN | calculator.py 重写为 99 行（原 205）；工具测试 21/21 绿；全量 175 passed + 1 既有失败 |
| Task 1 审核 | sub-agent 结论「通过」：行为与 design §4 推演表一致、公约符合、无误捕 ToolExecutionError；低级提示 2 条不阻塞（白名单外运算符 fallthrough 消息可读性、幂边界与原版相同的既有边界） |
| Task 2 RED | TestTodo 整类（4 用例）删除；test_cli 删 3 个回调用例、幂等用例裁剪 callback 断言；裁剪后 41 项全绿证明与现实现兼容；两条接口断言（run_repl 无 on_session_change、_build_registry 仅 memory 参）均 AssertionError |
| Task 2 GREEN | 删 todo.py（147 行）；`__main__.py` 清除 TodoTool 导入 / RebindableTodoTool / on_session_change 3 调用点 / session_ref 闭包 / 仅 RebindableTodoTool 使用的 Path 与 BaseTool 导入；`_build_registry(memory)` 注册 4 工具；testRegistryIncludesReadMemory 适配新签名 |
| Task 2 审核 | sub-agent 结论「通过」：残留零命中、五命令行为逐行比对零变化、无过度删除、导入无 NameError；中级发现为文档 §5.3 未落地（已在随后完成）与 codegraph 待 sync（已执行） |
| 文档同步 | README.md 去 `todos/<id>.json` 与工具清单 todo 字样；CODEGRAPH.md 架构图改 4 工具、删 todo.py 行、补 read_memory.py 行、__main__ 行去 RebindableTodoTool 并修正行号锚点与 /switch 命令清单 |
| 收尾验证 | 全量 168 passed + 1 既有失败（testMiddlewareHooksInvoked，源自用户 loop.py 未提交改动，与本 change 无关）；.py 源码与 README/CODEGRAPH/AGENTS 文档残留 grep 零命中；calculator.py 99 行；codegraph sync 已执行；过期 todo pyc 缓存已清 |
| 提交 | 按公约原子提交（refactor: 实现与文档 + docs: openspec 方案产物）；用户未提交的 loop.py / builder.py / memory/store.py 与 .trae/ 不纳入本次提交 |
| 待办 | verify 阶段（archive 前置）待用户确认后进行 |
