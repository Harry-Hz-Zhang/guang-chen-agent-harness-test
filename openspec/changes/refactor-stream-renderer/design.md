# Design — StreamRenderer 架构设计

## 1. 模块划分与职责

```text
src/harness/
  renderer.py        [NEW] 流式事件终端渲染器与分通道状态机
  __main__.py        [MOD] CLI 入口，run_repl 委托 StreamRenderer，re-export render_event
tests/
  test_renderer.py   [NEW] StreamRenderer 独立单元测试
  test_cli.py        [MOD] 保持现有用例不变，验证与 CLI 集成
```

### 1.1 `src/harness/renderer.py` 职责
- 维护流式状态机：`in_reasoning`（是否在思考段中）、`streamed_any`（是否已输出过内容）。
- 事件分片渲染：
  - `ReasoningDelta`：若首次进入思考段，先输出 `_REASONING_PREFIX`（默认 `"思考｜"`），若之前输出过内容则首补 `\n`；后续分片直接透传内容。
  - `TextDelta`：若刚脱离思考段，先输出 `\n`；后续分片直接透传内容。
  - 其他事件（`UsageEvent`, `DoneEvent` 等）：不产生可见文本。
- 逐片输出支持：接收可选的 `raw_writer: Callable[[str], None]`，支持逐字即时写入与返回字符串双重语义。
- 回合收尾：`finalize()` 在有流式输出时补齐结尾换行。
- 向后兼容：提供纯函数 `render_event(event: StreamEvent, prefix: str = ...) -> str`，兼容原有无状态单事件测试/接口。

## 2. 关键数据结构与接口契约

```python
class StreamRenderer:
    """流式事件终端渲染器（维护分通道状态）。"""

    def __init__(
        self,
        raw_writer: Callable[[str], None] | None = None,
        reasoning_prefix: str = "思考｜",
    ) -> None:
        ...

    def render(self, event: StreamEvent) -> str:
        """接收单个流式事件，触发状态迁移，执行 raw_writer 输出并返回渲染文本片段。"""
        ...

    def finalize(self) -> str:
        """回合结束时补齐换行，若有输出则写入并返回换行符，否则返回空字符串。"""
        ...

    def reset(self) -> None:
        """重置状态供下一轮使用。"""
        ...

def render_event(
    event: StreamEvent,
    prefix: str = "思考｜",
) -> str:
    """把单个流式事件渲染为终端文本（纯函数向后兼容接口）。"""
    ...
```

## 3. 被否决的备选方案与原因

### 备选方案 1：直接从 `__main__.py` 删除 `render_event`，不抽离类
- **做法**：直接将 `render_event` 删掉，并修改 `tests/test_cli.py` 的 `TestRender` 用例使其删除。
- **否决原因**：
  1. `run_repl` 内部仍残留 20+ 行内联闭包状态机，CLI 入口仍然混杂流式格式化与 I/O 细节；
  2. 违背了 OpenSpec 既有规范中对流式渲染的断言要求，破坏了已有公共约定的平滑性；
  3. 无法将流式渲染作为一个独立可测试的单元进行覆盖。

### 备选方案 2：将流式渲染器内置在 `ReactLoop` 或 `LLMClient` 中
- **做法**：在 `loop.py` 或 `llm.py` 中直接维护终端输出与通道前缀。
- **否决原因**：
  1. 严重违反单一职责与核心/表现层隔离原则（Core vs Presentation）；
  2. `ReactLoop` 是通用 Agent 运行时，不应知道标准输出、终端前缀或 ANSI 转义字符的存在；
  3. 降低了运行时的可移植性（例如在 API 服务端或无界面测试环境中运行时）。
