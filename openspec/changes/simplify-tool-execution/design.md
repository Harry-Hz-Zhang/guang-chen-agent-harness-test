# Design — 工具执行路径简化设计（删超时线程池 + wrap_tool_call）

## 1. explore 结论（基于 codegraph 与全量测试）

| 事实 | 证据 |
| --- | --- |
| PRD 无任何超时 / 中间件条款 | `doc/PRD.md` grep「超时\|timeout\|中间件\|middleware」零命中；工具执行相关要求只有错误结构化回传 + trace |
| 超时执行链在 loop.py 内部闭环，无外部调用者 | `codegraph impact _execute_with_timeout`：仅 `_execute_through_middlewares`、`base_execute` 两个符号受影响，全在 loop.py |
| `wrap_tool_call` 无生产消费者 | `codegraph impact wrap_tool_call`：基类默认实现 + `tests/test_loop.py:80`（RecordingMiddleware）+ `tests/context/test_builder.py:190`（testDefaultNoOp）；`CompactionMiddleware` 只覆盖 `before_model` |
| `before_model` / `after_model` 承载 PRD 核心需求，必须保留 | `__main__.py:220` `middlewares=[CompactionMiddleware(compressor)]`——超长压缩（PRD 核心能力 5）经 `before_model` 钩子接入；`LoopState.trace_id` 注入服务于压缩 trace 归并 |
| `tool_timeout_seconds` 只被超时机制自身消费 | config.py L24（env 表）+ L54（默认值）→ loop.py L218 / L247 唯二消费点；删超时后即死配置 |
| 测试影响面 | test_loop.py：`testToolTimeoutStructuredReturn`（L316-331 删）、`_SlowTool`（L98-108 删）、`RecordingMiddleware.wrap_tool_call`（L68/L80-83 删）、`testMiddlewareHooksInvoked` 2 处 wrap 断言（L361/L380 改）、`time` / `Callable` import 删；test_config.py L45/L77/L104 断言删；test_builder.py L189-193 wrap 段删 + `ToolCall` import 删 |
| 文档影响面 | README.md L56（`[middleware.wrap_tool_call] → 执行(30s 超时)`）、L70（三钩子描述）；CODEGRAPH.md L125（「超时线程隔离执行」）、L128（「wrap_tool_call 扩展点，支撑压缩、安全拦截与限流」）；`.env.example` L32 |
| 基线 | `uv run pytest` 169 项全绿；工作区有 `load_dotenv` 在途未提交改动（.env 加载特性），与本 change 正交，不触碰 |

## 2. 模块划分与职责

```text
src/harness/loop.py                [MOD] 执行段直接调 tool.execute；删 2 私有方法 + functools/ThreadPoolExecutor
                                        import + TimeoutError 分支；模块/方法 docstring 同步
src/harness/middleware.py          [MOD] 删 wrap_tool_call 钩子与 Callable import；docstring 三钩子→两钩子
src/harness/config.py              [MOD] 删 tool_timeout_seconds 字段与 env 表行；docstring 同步
tests/test_loop.py                 [MOD] 删超时用例/_SlowTool/wrap 计数；testMiddlewareHooksInvoked 裁剪
tests/test_config.py               [MOD] 删 3 处 tool_timeout_seconds 断言
tests/context/test_builder.py      [MOD] testDefaultNoOp 裁剪 wrap 段 + 删 ToolCall import
README.md / CODEGRAPH.md           [MOD] 移除 wrap_tool_call 与超时描述
.env.example                       [MOD] 删 HARNESS_TOOL_TIMEOUT_SECONDS 示例行
```

`parser.py`、`registry.py`、`tools/`、session / context / memory / trace 层零改动。

## 3. 关键改动：`_handle_tool_call` 执行段

改前（loop.py L212-221 + L225-249）：

```python
        try:
            result = self._execute_through_middlewares(call, tool)   # 反向 partial 包裹链
        except TimeoutError:
            error = self._error_payload(call, "ToolTimeoutError", f"工具执行超时（超过 {...} 秒）")
        except Exception as exc:
            error = self._error_payload(call, "ToolExecutionError", str(exc))
```

改后：

```python
        try:
            result = tool.execute(**(call.args or {}))
        except Exception as exc:
            error = self._error_payload(call, "ToolExecutionError", str(exc))
```

`_execute_through_middlewares` / `_execute_with_timeout` 两个方法整体删除；`import functools`、`from concurrent.futures import ThreadPoolExecutor` 删除（`Callable` 保留——`run` / `_call_llm` / `_tee` 签名仍用）。

## 4. 异常语义推演

| 场景 | 改前 | 改后 | 评价 |
| --- | --- | --- | --- |
| 工具抛普通异常（`_BoomTool`） | `ToolExecutionError` 结构化回传 | 同左（不变） | 契约保持，`testToolExecutionErrorStructuredReturn` 零改动通过 |
| 工具自身抛 `TimeoutError`（如 HTTP 超时上抛） | 被误归类 `ToolTimeoutError`（原 docstring 自认的语义瑕疵） | 统一落 `ToolExecutionError` | 更准确：线程池超时是 runtime 强加的，工具内部超时是工具自身失败 |
| 工具死循环 | 线程池超时后主循环继续，但工作线程泄漏、进程退出被 atexit join 延迟 | 主循环阻塞 | 单用户 CLI 场景可接受；死循环属工具自身 bug，由 trace span 与用户 Ctrl-C 暴露 |
| 未注册 / 参数非法 | 结构化错误，不执行 | 同左（不变） | `testUnknownToolStructuredReturn` / `testInvalidArgumentsNotExecuted` 零改动通过 |

## 5. 被否决的备选方案与原因

### 备选 1：只删 middleware 包裹链、保留线程池超时
- 超时同样无 PRD 依据，且「线程不可强杀 + atexit join 延迟退出」的语义瑕疵只在超时路径存在；保留它就保留了 `_SlowTool` / `testToolTimeoutStructuredReturn` / `tool_timeout_seconds` 全套连带设施。删不彻底。

### 备选 2：保留 `wrap_tool_call` 作为「未来扩展点」（安全拦截 / 限流）
- 当前零生产消费者，属为假设场景设计（AGENTS.md §4 明确反对）；CODEGRAPH.md 宣称的「安全拦截与限流」从未实现。真有需要时加回一个钩子方法 + 一行循环的成本极低。

### 备选 3：把超时下放到 `BaseTool` / 各工具内部
- 仍是 PRD 外能力，且要求每个工具各自实现超时控制，重复且无法统一配置；不如不做。

### 备选 4：连 `middleware.py` 整个删掉
- `CompactionMiddleware`（超长压缩，PRD 核心能力 5）经 `before_model` 钩子接入主循环，`LoopState` 是压缩与 trace 归并的共享载体。删基类等于拆掉压缩机制，越界。

### 备选 5：`tool_timeout_seconds` 字段保留、仅停止消费
- 留一个无人读取的配置项 + 3 处测试断言 + `.env.example` 示例行，纯仓库噪音。连根删。
