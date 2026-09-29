# Proposal — 简化工具执行路径：删除超时线程池与 wrap_tool_call 包裹链

## 1. 背景与动机

PRD 全文（[`doc/PRD.md`](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/doc/PRD.md)）对「超时 / timeout / 中间件 / middleware」**零命中**——PRD 对工具执行的全部要求是「异常处理 + 工具调用 trace 日志」（AGENTS.md 核心能力 6）：失败结构化回传给 LLM、调用过程可追踪。当前 [`src/harness/loop.py`](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/src/harness/loop.py) 的工具执行路径却堆了三层 PRD 外机制：

```text
_handle_tool_call
  └─ _execute_through_middlewares   # functools.partial 反向包裹 middleware 链
       └─ _execute_with_timeout     # 每次调用新建 ThreadPoolExecutor(1) + future.result(timeout)
            └─ tool.execute(...)
```

连带拖出的死重量：

- **`wrap_tool_call` 钩子**（[middleware.py L42-46](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/src/harness/middleware.py#L42)）：全仓库**无任何生产代码覆盖**——`CompactionMiddleware` 只用 `before_model`，唯一「消费者」是基类自己的默认透传和测试里的 `RecordingMiddleware`。
- **`tool_timeout_seconds` 配置项**：config.py 字段 + `HARNESS_TOOL_TIMEOUT_SECONDS` 环境变量覆盖表 + `.env.example` 示例行 + `test_config.py` 3 处断言，全部只服务超时机制本身。
- **专用测试设施**：`_SlowTool`、`testToolTimeoutStructuredReturn`、`RecordingMiddleware.wrap_tool_call` 计数。
- **已知语义瑕疵**（docstring 自述）：Python 线程无法强杀，超时后工具线程仍在后台运行；工具死循环时进程退出被 atexit join 延迟。

用户定位明确：校验通过后**直接 `tool.execute()`** 即可，超时与包裹链均无必要。

## 2. 目标

1. `_handle_tool_call` 执行段改为直接 `tool.execute(**(call.args or {}))`；删除 `_execute_through_middlewares` / `_execute_with_timeout` / `base_execute` 闭包与 `TimeoutError` 专案分支。
2. **错误回传契约不变**：执行异常仍结构化为 `ToolExecutionError` JSON 回传 LLM、主循环不中断；工具自身抛的 `TimeoutError`（如 HTTP 客户端超时）统一落入通用异常分支。
3. [middleware.py](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/src/harness/middleware.py) 删除 `wrap_tool_call` 钩子；**保留** `before_model` / `after_model`（`CompactionMiddleware` 的压缩入口，PRD 核心能力 5「超长基础压缩」）。
4. [config.py](file:///Users/zhanghongze/PycharmProjects/guang-chen-agent-harness-test/src/harness/config.py) 删除 `tool_timeout_seconds` 字段与 `HARNESS_TOOL_TIMEOUT_SECONDS` 环境变量表行。
5. 测试同步：删 `testToolTimeoutStructuredReturn` / `_SlowTool` / `RecordingMiddleware.wrap_tool_call`；`testMiddlewareHooksInvoked` 去 wrap 断言；`testDefaultNoOp` 裁剪 wrap 段；`test_config.py` 3 处断言删除。
6. 文档同步：README.md（L56 数据流、L70 模块说明）、CODEGRAPH.md（L125、L128）、`.env.example`（L32）。

## 3. 非目标（Non-goals）

1. **不删 middleware 架构本身**：`Middleware` 基类、`LoopState`、`before_model` / `after_model` 钩子、`CompactionMiddleware` 及 `__main__.py` 装配全部保留。
2. 不动 `llm_timeout_seconds` / `llm_max_retries`（LLM 客户端自身的超时重试，独立配置，PRD 之外但属于已定稿设计且被 `llm.py` 实际消费）。
3. 不动工具查找 / `validate_arguments` 参数校验 / trace span / tool 消息落库逻辑。
4. 不给 `BaseTool` 增加任何超时约定（不在工具层复活该能力）。
5. 不修改 `doc/PRD.md`；不处理工作区在途的 `load_dotenv` 未提交改动（与本 change 正交）。

## 4. 验收标准

1. **超时链清零**：`src/` 中不再出现 `ThreadPoolExecutor` / `functools` / `_execute_through_middlewares` / `_execute_with_timeout` / `ToolTimeoutError` / `tool_timeout_seconds`。
2. **钩子收敛**：`Middleware` 仅剩 `before_model` / `after_model` 两钩子；`wrap_tool_call` 在 `src/` 与 `tests/` 痕迹清零。
3. **行为保持**：`testToolExecutionErrorStructuredReturn` 等既有错误回传用例、`testMiddlewareHooksInvoked`（裁剪后）、`testTraceSpansEmitted` 原样通过；`RuntimeConfig()` 其余字段默认值不变。
4. **全量回归**：`uv run pytest` 全绿，用例数 176 → 175（仅删 `testToolTimeoutStructuredReturn`）。
5. **接口断言**：`hasattr` 检查 `RuntimeConfig.tool_timeout_seconds` / `Middleware.wrap_tool_call` / `ReactLoop._execute_with_timeout` 均为 False。
6. **文档同步**：README / CODEGRAPH / `.env.example` 无 `wrap_tool_call`、`30s 超时`、`HARNESS_TOOL_TIMEOUT_SECONDS` 字样。
7. **图谱同步**：apply 完成后 `codegraph sync` 成功。
