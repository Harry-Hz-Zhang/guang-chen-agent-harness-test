# Log — simplify-tool-execution apply 记录

## 2026-09-28

### Task 1: 删除工具执行超时链与 wrap_tool_call 钩子，工具改为直接执行

**RED**

- 裁剪测试：`test_loop.py` 删 `testToolTimeoutStructuredReturn` / `_SlowTool` / `RecordingMiddleware.wrap_tool_call` / `time`·`Callable` import；`testMiddlewareHooksInvoked` 删 2 处 wrap 断言；`test_config.py` 删 3 处 `tool_timeout_seconds` 断言；`test_builder.py#testDefaultNoOp` 裁剪 wrap 段并删 `ToolCall` import
- 接口断言（对现实现必失败，作为可执行规格）：`RuntimeConfig.__dataclass_fields__` 含 `tool_timeout_seconds` → AssertionError ✓；`hasattr(Middleware, 'wrap_tool_call')` → AssertionError ✓；`hasattr(ReactLoop, '_execute_with_timeout'/'_execute_through_middlewares')` → AssertionError ✓
- 裁剪后测试对现实现全绿（34 项，无副作用）

**GREEN**

- `loop.py`：执行段改为 `tool.execute(**(call.args or {}))`；删 `except TimeoutError` 专案（工具自身 TimeoutError 统一落 `ToolExecutionError`）；删 `_execute_through_middlewares` / `_execute_with_timeout` / `functools` / `ThreadPoolExecutor` import；docstring 同步
- `middleware.py`：删 `wrap_tool_call` 钩子与 `Callable` import，两钩子（before_model / after_model）
- `config.py`：删 `tool_timeout_seconds` 字段与 `HARNESS_TOOL_TIMEOUT` env 表行（未触碰在途 `load_dotenv` 改动）

**ASSERT**

- `uv run pytest` 全绿；用例数 176 → 175（基线经 `grep -c "def test"` 逐文件核对；pytest `addopts="-q"` 无总结行，点阵目测不可靠，tasks/proposal 中最初按目测写的 169/168 已修正）
- `grep -rn "wrap_tool_call\|_execute_with_timeout\|_execute_through_middlewares\|tool_timeout_seconds\|ToolTimeoutError\|ThreadPoolExecutor" src/ tests/` 零命中
- 三条接口断言全部通过
- 既有错误回传用例（testToolExecutionErrorStructuredReturn / testUnknownToolStructuredReturn / testInvalidArgumentsNotExecuted / testTraceSpansEmitted）零改动通过，D23 契约不变

### Task 2: 同步文档并重建代码图谱

**GREEN**

- README.md L56 数据流改「ToolCallBatch → 逐个 validate → 直接执行」；L70 两钩子描述
- CODEGRAPH.md L125 删「超时线程隔离执行」；L128 改「before_model、after_model 扩展点，支撑超长压缩等会话级处理」
- `.env.example` 删 `HARNESS_TOOL_TIMEOUT_SECONDS` 行

**ASSERT**

- `grep -rn "wrap_tool_call\|HARNESS_TOOL_TIMEOUT\|tool_timeout" README.md CODEGRAPH.md .env.example` 零命中
- `uv run pytest` 全绿（175 项）
- `codegraph sync` 完成（索引 up to date；`codegraph impact wrap_tool_call` / `_execute_with_timeout` 均报 "Symbol not found"，确认图谱与代码一致）。注：sync 的遥测文件（~/.codegraph/telemetry-queue.jsonl）写入被本环境沙箱拦截致命令 exit 1，不影响索引本体

### 遗留说明

- 工作区在途的 `load_dotenv` 未提交改动（.gitignore / README / __main__ / config / test_cli / test_config / .env.example）与本 change 正交，未触碰其逻辑；`.env.example` 的 `HARNESS_TOOL_TIMEOUT_SECONDS` 行删除是本 change 的必要同步
- 工具自身抛 `TimeoutError` 的语义变化（原 `ToolTimeoutError` → 现 `ToolExecutionError`）已在 design.md §4 推演，结构化回传契约不变
