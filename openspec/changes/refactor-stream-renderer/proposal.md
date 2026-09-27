# Proposal — 抽象 StreamRenderer 并解耦 __main__.py

## 1. 背景与动机

在当前实现中，[`src/harness/__main__.py`](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/src/harness/__main__.py) 定义了 `render_event(event: StreamEvent) -> str` 函数，但该函数并未参与任何生产链路调用，仅被 [`tests/test_cli.py`](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/tests/test_cli.py) 中的 `TestRender#testStreamEventRendering` 调用。
真正的终端流式输出逻辑硬编码在 `run_repl` 函数内部的 `on_event` 局部闭包中，维护 `in_reasoning` 与 `streamed_any` 等状态。

这种实现存在以下问题：
1. **死代码与职责污染**：`__main__.py` 作为入口装配模块，承载了未被调用的渲染函数以及复杂的局部闭包状态机；
2. **测试与生产脱节**：单测用例只测试了无状态的纯函数，未直接复用流式分通道的渲染引擎；
3. **扩展性差**：流式格式化与终端输出强耦合，未来若接入 WebSockets/GUI 等出口无法复用。

因此，提出抽象独立的 `StreamRenderer` 模块，将流式事件的分通道（思考段/正文段）格式化与状态转换逻辑封装为独立组件，`run_repl` 统一委托其处理，并保持旧有 `render_event` 接口向下兼容。

## 2. 目标

1. 新建 `src/harness/renderer.py`，实现 `StreamRenderer` 类，接管思考前缀、正文切换、分片透传与回合结束换行状态机。
2. 在 `src/harness/renderer.py` 中提供 `render_event` 接口，并在 `src/harness/__main__.py` 中保持导入兼容，确保不破坏既有导入。
3. 重构 `src/harness/__main__.py` 中的 `run_repl`，消除内联闭包状态机，委托 `StreamRenderer`。
4. 在 `tests/test_renderer.py` 中增加对 `StreamRenderer` 各种分片序列、通道切换、换行处理的完整独立单元测试。
5. 保证既有全部 148+ 项 pytest 测试全绿。

## 3. 非目标（Non-goals）

1. 不改变 LLM 客户端产出的 `StreamEvent` 数据结构（`ReasoningDelta`、`TextDelta`、`UsageEvent`、`DoneEvent` 等保持不变）。
2. 不重构 `loop.py` 中的 `_tee` 或流式聚合逻辑。
3. 不引入富文本渲染库（如 rich、prompt_toolkit 等第三方依赖），保持标准库纯原生实现。
4. 不修改 `doc/PRD.md` 需求文档。

## 4. 验收标准

1. **测试覆盖**：新增 `tests/test_renderer.py`，覆盖正常流、连续思考流、通道切换换行、非 Delta 事件忽略、结束补换行等至少 5 组用例。
2. **全量回归**：`uv run pytest` 全绿，原 148 个测试及新增测试 100% 通过。
3. **调用链收敛**：通过 `codegraph callers StreamRenderer` 和 `codegraph impact render_event` 验证调用链清晰，`__main__.py` 内部 `run_repl` 真实调用 `StreamRenderer`。
4. **架构文档同步**：同步更新 `AGENTS.md` 目录登记与 `CODEGRAPH.md` 拓扑，并执行 `codegraph sync` 同步索引。
