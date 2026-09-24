# Design — build-minimal-agent-runtime

## Context

仓库当前无任何代码（src/ 不存在）。全部决策来源于 explore 阶段的 44 节点决策树（`../../last-exploration-handoff.md`，用户已逐项确认）与 4 份技术调研（DeepSeek API / langchain / AgentScope+Codex+Memos / trace 方案，结论已摘录在 handoff）。关键外部事实：DeepSeek 现役模型 `deepseek-flash`（1M 窗口、function calling、默认思考模式）；openai SDK 3.19.2 类型层无 `reasoning_content` 但运行时可透传（pydantic `extra="allow"`，用 `getattr` 取）；带 tools 的思考模式请求每轮必须回传 `reasoning_content`，否则 400。

## Goals / Non-Goals

**Goals:**

- 12 个模块的最小 runtime，每个模块可独立测试（依赖全注入、LLM 客户端可替换为 fake）
- 全同步实现（pytest 友好），后台任务用 daemon 线程
- 零新增第三方依赖（仅 openai + pytest）
- 所有提示词、数值常量集中管理

**Non-Goals（设计级边界，不重复 proposal 非目标）:**

- 不做 asyncio 异步体系（理由见决策 4）
- 不做 langchain 式 Runnable 组合抽象 / bind 拷贝语义（工具表直接作参数传递）
- 消息结构只支持 OpenAI dict 格式，不做多格式互转层
- 不做独立「压缩专用小模型」配置（压缩/总结/合并复用同一 LLMClient，`LLM_MODEL` 可整体覆盖）

## Decisions

### 决策 1：模块划分

```
src/harness/
├── __init__.py
├── __main__.py        # CLI 入口：argparse + REPL 循环 + 流式渲染
├── config.py          # RuntimeConfig（全部数值常量 + 环境变量覆盖）
├── prompts.py         # 4 个提示词模板常量
├── llm.py             # LLMClient + AIMessage/ToolCall/Usage/StreamEvent
├── parser.py          # parse_response + AgentDecision + 参数校验
├── loop.py            # ReactLoop（主循环 + middleware 编排 + 轮次上限）
├── middleware.py      # Middleware 基类 + LoopState
├── trace.py           # TraceCollector + TraceExporter/JsonlExporter
├── tools/
│   ├── base.py        # BaseTool + ToolExecutionError
│   ├── registry.py    # ToolRegistry + 异常
│   ├── calculator.py  # ast 安全求值
│   ├── search.py      # mock 知识库检索
│   ├── weather.py     # mock 城市天气
│   └── todo.py        # 待办（文件持久化，按会话隔离）
├── session/
│   └── store.py       # SessionStore（JSONL append-only）
├── context/
│   ├── builder.py     # ContextBuilder + estimate_tokens
│   └── compressor.py  # ContextCompressor + CompactionMiddleware
└── memory/
    ├── store.py       # MemoryStore（哈希去重 + LLM 合并）
    └── summarizer.py  # MemorySummarizer（daemon 线程扫描）

data/                  # 运行时产物（gitignore）
├── sessions/<session_id>.jsonl
├── memory/<session_id>/MEMORY.md + entries/<sha256[:12]>.md
├── traces/<session_id>.jsonl
└── todos/<session_id>.json
```

单次用户输入的数据流：

```
用户输入
  → TraceCollector.start_trace（trace_id 注入 LoopState，middleware 可据此挂 span）
  → SessionStore.append(user message)
  → [middleware.before_model]  CompactionMiddleware: should_compact? → ContextCompressor.compact()
      （压缩内的摘要 LLM 调用经 start_llm_span(kind="compaction") 包裹，与主循环 span 同 trace 平级）
  → ContextBuilder.build()     系统提示词(+记忆段) + 压缩摘要 + 未压缩历史 + 当前输入
  → TraceCollector.start_llm_span
  → LLMClient.stream()/invoke()
      ├─ on_event 回调 → CLI 分通道渲染（思考/正文/工具调用中）
      └─ 聚合为 AIMessage
  → SessionStore.append(assistant message，含 reasoning_content)
  → TraceCollector.end_llm_span(usage)
  → parse_response() → AgentDecision
      ├─ FinalAnswer → [middleware.after_model] → 返回
      └─ ToolCallBatch → 逐个 validate → [middleware.wrap_tool_call] → 执行(30s 超时)
            ├─ 成功 → ToolMessage(结果，组装时截 2000 字符)
            └─ 失败 → ToolMessage(结构化错误 JSON)
          → SessionStore.append + TraceCollector tool span
          → 回到 before_model（round_no+1，上限 15）
```

### 决策 2：工具调用协议 = 官方 function calling（否决：自造 ReAct 文本协议）

`finish_reason == "tool_calls"` 即「继续循环」信号；工具结果以 `{"role":"tool","tool_call_id":...,"content":...}` 回传。**被否决备选**：prompt 里约定 `Thought/Action/Observation` 文本格式自行解析——模型不保证格式、正则解析脆弱、无明确终止信号、吃不到官方后训练红利；DeepSeek 官方文档明确警告 `function.arguments` 可能是非法 JSON，故 parser 层必须 `json.loads` + Schema 校验 + 失败结构化回传（对应 spec agent-loop「工具参数显式校验」）。

### 决策 3：默认模型 deepseek-flash，思考模式默认开

思考内容以 `reasoning_content` 独立字段提取（满足 PRD「提取思考过程」）。三个落地约束写进实现：① 代码用 `getattr(message, "reasoning_content", None)` 取值；② assistant 消息持久化时保留该字段并在后续每轮请求原样回传（带 tools 时漏传即 400）；③ 关闭思考用 `extra_body={"thinking": {"type": "disabled"}}`（CLI `--no-thinking`）。**被否决备选**：保持 `deepseek-chat`——已于 2026-07-24 停用，不可行；`deepseek-v4-pro` 更贵，CLI 工具场景收益不明显。

### 决策 4：全同步 + daemon 线程（否决：asyncio 全家桶）

主循环、LLM 调用、流式消费全部同步；仅闲置会话总结用 `threading.Thread(daemon=True)` 每 `SCAN_INTERVAL` 秒扫一轮 session 文件 mtime。**被否决备选**：asyncio——会把 async 传染到 loop/llm/trace/memory 全部接口，pytest 需要 asyncio 插件，CLI 单用户场景无并发收益。

### 决策 5：关键数据结构与命名表（explore 阶段 D20/D23/D25-D32/D34 委托项定稿）

```python
# llm.py
@dataclass
class ToolCall:
    id: str; name: str; arguments_raw: str; args: dict | None   # args 为解析后 dict，未解析为 None

@dataclass
class Usage:
    prompt_tokens: int; completion_tokens: int; total_tokens: int; reasoning_tokens: int = 0

@dataclass
class AIMessage:
    role: str = "assistant"
    content: str = ""
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str = ""

# StreamEvent（流式管道，think/text 分离的载体）
StreamEvent = ReasoningDelta(text) | TextDelta(text)
            | ToolCallDelta(index, id, name, args_delta)
            | UsageEvent(usage) | DoneEvent(finish_reason)      # dataclass 联合，tag 用类名判别

class LLMClient:
    def __init__(self, config: RuntimeConfig) -> None: ...
    def invoke(self, messages: list[dict], tools: list[dict] | None = None) -> AIMessage: ...
    def stream(self, messages: list[dict], tools: list[dict] | None = None) -> Iterator[StreamEvent]: ...


class LLMError(Exception): ...  # LLM 调用失败（网络/超时/鉴权/缺 key）统一封装，含可读中文消息；CLI 据此输出提示并保持 REPL 可用
```

```python
# parser.py
@dataclass
class FinalAnswer:
    content: str


@dataclass
class ToolCallBatch:
    calls: list[ToolCall]


AgentDecision = FinalAnswer | ToolCallBatch


class ToolArgumentError(Exception): ...  # 含 tool_call_id 与原因


def parse_response(message: AIMessage) -> AgentDecision: ...
def validate_arguments(
    call: ToolCall, schema: dict
) -> list[str]: ...  # 返回错误列表（空=通过）
```

```python
# tools/base.py + registry.py
class BaseTool(ABC):
    name: str
    description: str
    parameters: dict  # parameters 为 JSON Schema

    @abstractmethod
    def execute(
        self, **kwargs: Any
    ) -> str: ...  # 返回字符串结果；失败抛 ToolExecutionError


class ToolExecutionError(Exception): ...  # 含 tool 名 / args / 原始异常


class ToolRegistry:
    def register(self, tool: BaseTool) -> None: ...  # 重名抛 DuplicateToolError
    def get(self, name: str) -> BaseTool: ...  # 未注册抛 ToolNotFoundError
    def names(self) -> list[str]: ...
    def to_openai_tools(
        self,
    ) -> list[
        dict
    ]: ...  # [{"type":"function","function":{name,description,parameters}}]
```

```python
# loop.py + middleware.py
@dataclass
class LoopState:
    session_id: str
    round_no: int
    messages: list[dict]
    user_input: str


class Middleware(ABC):
    def before_model(self, state: LoopState) -> None: ...  # 默认空实现
    def after_model(self, state: LoopState) -> None: ...
    def wrap_tool_call(
        self, call: ToolCall, execute: Callable[[ToolCall], str]
    ) -> str: ...


@dataclass
class LoopResult:
    answer: str
    rounds: int
    tool_call_count: int
    truncated: bool  # truncated=是否因最大轮次截断


class ReactLoop:
    def __init__(
        self, llm, registry, sessions, builder, trace, middlewares, config
    ) -> None: ...
    def run(
        self, user_input: str, on_event: Callable[[StreamEvent], None] | None = None
    ) -> LoopResult: ...
```

```python
# session/store.py —— JSONL 记录结构（kind 枚举：session_meta / message / compaction）
# 每行：{"ts": iso8601, "ordinal": int, "kind": "...", ...}
#   session_meta: {"session_id", "created", "model"}
#   message:      {"message": {role, content, reasoning_content?, tool_calls?, tool_call_id?}}
#   compaction:   {"compressed_up_to": int, "summary": str, "summary_model": str}

class SessionStore:
    def append_message(self, session_id: str, message: dict) -> None: ...
    def append_compaction(self, session_id: str, compressed_up_to: int, summary: str, model: str) -> None: ...
    def load_records(self, session_id: str) -> list[dict]: ...
    def read_context_messages(self, session_id: str) -> list[dict]:   # 应用压缩区间后的 OpenAI 消息列表
    def session_ids(self) -> list[str]: ...
    def last_modified(self, session_id: str) -> float | None: ...     # mtime，供闲置扫描
    def message_rounds(self, session_id: str) -> int: ...             # 会话累计对话轮数（仅 /history 用；压缩判定用 compressor.uncompressed_rounds）
```

```python
# context/builder.py + compressor.py
class ContextBuilder:
    def __init__(self, sessions, memory, config) -> None: ...
    def build(self, session_id: str, user_input: str) -> list[dict]: ...
    #   组装顺序：system(含记忆段) → __compaction_summary__ 消息 → 未压缩历史 → 当前输入
    #   摘要消息 content 首行带明文标记「以下为此前对话的压缩摘要」（与原文可区分）；
    #   tool 结果 content 超 TOOL_RESULT_MAX_CHARS(2000) 时截断并附「全文见会话记录」尾注

def estimate_tokens(messages: list[dict]) -> int:
    # ceil(字符数/2.5) + 每条消息 5 + 每个工具调用块 10 + 每个工具结果块 8（参考项目口径）

class ContextCompressor:
    def __init__(self, sessions, llm, trace, config) -> None: ...
    def should_compact(self, session_id: str) -> bool: ...    # 未压缩窗口内：对话轮数≥COMPACT_ROUNDS 或 估算 token≥COMPACT_TOKENS
    def uncompressed_rounds(self, session_id: str) -> int: ...  # 统计口径：仅未压缩区间的 user/assistant 对数（压缩后回落）
    def compact(self, session_id: str, trace_id: str) -> None: ...
    #   保留最近 KEEP_RECENT_ROUNDS(5) 轮原文（按对话轮边界切分，连带保留轮内 tool 配对消息）；
    #   切点回退保证 tool_call/tool 配对不拆散；
    #   COMPACTION_PROMPT 生成 5 字段摘要（该 LLM 调用经 start_llm_span(kind="compaction") 包裹，挂 trace_id 下）；失败告警不抛出

class CompactionMiddleware(Middleware):
    def before_model(self, state) -> None: ...                # should_compact → compact(session_id, state.trace_id)，失败 logging.warning
```

```python
# memory/store.py + summarizer.py
class MemoryStore:
    def write(
        self, session_id: str, content: str, tags: list[str] | None = None
    ) -> bool: ...
    #   规范化(strip) → sha256 → 已存在返回 False；否则写 entries/<sha256[:12]>.md + 更新 MEMORY.md 索引
    def list_entries(self, session_id: str) -> list[dict]: ...
    def render_summary(
        self, session_id: str, max_chars: int = 2000
    ) -> str | None: ...  # 供 ContextBuilder 注入
    def merge(self, session_id: str) -> int: ...

    #   MEMORY_MERGE_PROMPT → LLM 输出 [{"action": "ADD|UPDATE|DELETE|NOOP", ...}] → 应用 → 重写 MEMORY.md


class MemorySummarizer:
    def __init__(
        self, sessions, memory, llm, trace, config
    ) -> None: ...  # 总结的 LLM 调用经 trace.start_trace(会话 id) 生成独立 trace + start_llm_span(kind="idle_summary") 包裹

    def scan_once(
        self,
    ) -> list[str]: ...  # 返回本次总结的 session_id 列表（已总结过/活跃的跳过）
    def start(self) -> None: ...  # daemon Thread + 周期循环，幂等可重入
    def stop(self) -> None: ...
```

```python
# trace.py —— 事件 schema（OTel GenAI 对齐；完整 JSON 样例见验收 7）
class TraceExporter(Protocol):
    def export(self, event: dict) -> None: ...  # 失败仅告警不抛出


class JsonlExporter:  # data/traces/<session_id>.jsonl 追加写
    def __init__(self, trace_dir: Path) -> None: ...


class TraceCollector:
    def start_trace(
        self, session_id: str
    ) -> str: ...  # 返回 trace_id（token_hex(16)，32 hex；后台总结复用同入口生成独立 trace）

    def start_llm_span(
        self, trace_id: str, model: str, messages: list[dict], kind: str = "chat"
    ) -> str: ...  # span_id=token_hex(8)（16 hex）；kind: "chat"|"compaction"|"idle_summary" → harness.span.kind 属性

    def end_llm_span(
        self,
        span_id: str,
        output: dict,
        usage: Usage,
        finish_reason: str,
        error: dict | None = None,
    ) -> None: ...
    def start_tool_span(
        self, trace_id: str, parent_span_id: str, call: ToolCall
    ) -> str: ...
    def end_tool_span(
        self, span_id: str, result: str | None, error: dict | None = None
    ) -> None: ...
    # 正文字段（gen_ai.input.messages / gen_ai.output.messages / gen_ai.tool.call.result）逐项截 2000 字符 + 「已截断」尾注，全量以会话文件为真源
    # 无 flush()：JsonlExporter 逐行即写即落盘，无缓冲语义（YAGNI，未来接 langfuse exporter 再引入）
```

```python
# config.py —— 全部数值常量（explore 阶段数值包定稿，环境变量 HARNESS_* 可覆盖）
@dataclass
class RuntimeConfig:
    model: str = "deepseek-flash"  # LLM_MODEL
    base_url: str = "https://api.deepseek.com"  # LLM_BASE_URL
    thinking_enabled: bool = True  # --no-thinking
    stream_enabled: bool = True  # --no-stream
    max_rounds: int = 15  # 【单次请求决策轮】一次用户输入内 LLM 决策（调工具）步进上限
    compact_rounds: int = 60  # 压缩触发：【会话对话轮】累计 user/assistant 对数
    compact_tokens: int = 100_000  # 压缩触发：估算 token
    keep_recent_rounds: int = (
        5  # 压缩保留最近【对话轮】原文（5 轮 ≙ 10 条消息，轮内 tool 配对连带保留）
    )
    idle_seconds: int = 7200  # 闲置判定（默认 2h）
    scan_interval_seconds: int = 300  # 后台扫描间隔（默认 5 分钟）
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    tool_timeout_seconds: float = 30.0
    tool_result_max_chars: int = 2000  # 工具结果进上下文截断（存储留全量）
    data_dir: Path = Path("data")
```

**工具错误回传 JSON 格式（D23 定稿）**——所有工具失败统一形状，作为 ToolMessage content 回传：

```json
{"error": {"type": "ToolExecutionError|ToolTimeoutError|InvalidToolArguments|ToolNotFoundError",
           "message": "人类可读错误描述",
           "tool": "search", "args": {"query": "..."}, "tool_call_id": "call_xxx"}}
```

**压缩摘要消息标记（D34 定稿）**：摘要以 `{"role": "user", "name": "__compaction_summary__", "content": "<标记行>\n<summary>"}` 注入上下文（照搬参考项目命名），其中标记行为固定明文「以下为此前对话的压缩摘要」，使 LLM（及人类看 trace 时）能将摘要与原始对话消息区分开。

**术语约定（避免「轮次」撞词）**：全文「**单次请求决策轮**」专指 `max_rounds`——一次用户输入内 LLM 决策+执行工具的次数（熔断用，与压缩无关）；「**会话对话轮**」专指 `compact_rounds` / `keep_recent_rounds`——会话累计的 user/assistant 对话对数（存量统计量，用于压缩触发与保留）。请求组装不因决策轮而改变，两者不互相影响。

**REPL 命令集（D33 定稿）**：`/exit` 退出、`/new` 新会话、`/sessions` 列出会话、`/history` 当前会话概览。CLI 参数：`--session <id>`、`--no-stream`、`--no-thinking`。

**提示词模板（prompts.py，D35 定稿）**：`SYSTEM_PROMPT`（角色 + 工具使用约定 + 记忆段占位）、`COMPACTION_PROMPT`（5 字段结构化：任务概览/当前状态/重要发现/下一步/需保留上下文）、`MEMORY_SUMMARY_PROMPT`（闲置会话总结为记忆条目）、`MEMORY_MERGE_PROMPT`（输出 ADD/UPDATE/DELETE/NOOP 动作 JSON）。

### 决策 6：会话存储 = 本地 JSONL（否决：SQLite / Memos）

一个会话一个文件，append-only，天然隔离与持久化，多窗口并发写各自文件互不干扰；闲置检测直接用文件 mtime。**被否决备选**：SQLite——引入查询能力但本项目无查询需求，且单文件多会话写入需处理锁；Memos（usememos）——需常驻 HTTP 服务，无会话隔离语义，为纯增量复杂度。

### 决策 7：压缩 = 打标记 + 结构化摘要（否决：删除式压缩 / 复杂分层策略）

被压消息保留在会话文件中不删除，仅追加一条 `kind:"compaction"` 记录（含区间上限 ordinal 与摘要），`read_context_messages` 据此跳过被压区间。摘要链式纳入下次压缩输入。压缩判定与 token 估算的统计口径均为**未压缩窗口**（已压缩区间不计入），因此压缩后计数回落，不会每轮重复触发。**被否决备选**：① 删除式压缩（直接删消息行）——破坏 append-only 与可审计性；② 参考项目的「超大工具结果卸载 + 入参截断」分层策略——PRD 明确「复杂的压缩不用在这里实现」。

### 决策 8：流式管道 = 6 种事件 + 聚合器（think/text 天然分离）

`LLMClient.stream` 把 openai SDK 的 chunk delta 翻译成 StreamEvent：`delta.reasoning_content → ReasoningDelta`、`delta.content → TextDelta`、`delta.tool_calls → ToolCallDelta`（按 index 累积 arguments 片段，流结束后统一 `json.loads`）、末 chunk `usage/finish_reason → UsageEvent/DoneEvent`。CLI 通过 `on_event` 回调渲染：思考带「思考」前缀弱化样式，正文正常样式。**被否决备选**：langchain 的 `astream_events` 事件体系——需要 async 与完整事件总线，6 种自定义事件已覆盖全部需求。

### 决策 9：trace = 自研迷你 OTel 模型（否决：langfuse / OTel SDK）

TraceCollector 管 ID 与计时，JsonlExporter 落盘，字段名抄 OTel GenAI 语义约定（`gen_ai.usage.input_tokens` 等）。**被否决备选**：langfuse 自托管（6 容器）与 OTel SDK（引入依赖树）——均超出作业级需要；未来接 langfuse 只需新写一个 exporter 把同样的 attributes 翻译成 OTLP JSON，业务代码零改动。

### 决策 10：记忆 = 哈希去重 + LLM 四动作合并（否决：embedding 语义去重）

写入路径零 LLM 成本（规范化 → SHA-256 → 撞则跳过）；合并仅在低频路径跑一次 LLM（mem0 的 ADD/UPDATE/DELETE/NOOP 范式）。**被否决备选**：embedding 相似度去重——DeepSeek API 不提供 embedding 接口，需另找模型，违背轻量原则。

### 决策 11：测试策略 = 依赖注入 + FakeLLMClient

ReactLoop 等组件全部构造注入；测试提供 `FakeLLMClient`（按脚本返回预置 AIMessage/StreamEvent 序列）与临时目录 SessionStore/MemoryStore（pytest `tmp_path`）。任何测试不得发起网络调用。真实 API 验收 = CLI 手工操作（README 记录步骤清单）。

## Risks / Trade-offs

- [字符近似 token 估算偏差] → 仅用于压缩触发判断（阈值 100K 有充分余量）；真实用量以 API usage 记入 trace
- [思考模式每轮回传 reasoning_content 增大请求体] → 属协议硬约束（漏传 400）；`--no-thinking` 可关；上下文超长由压缩兜底
- [openai SDK 类型层无 reasoning_content] → 已核实 pydantic extra="allow" 运行时可透传；统一用 `getattr` 访问，封装在 LLMClient 单点
- [daemon 线程与主进程退出竞态] → 总结写入幂等（哈希去重），线程 daemon=True 随进程退出，中断最多丢一次扫描
- [LLM 输出动作 JSON 非法（记忆合并）] → 解析失败保留原状并告警，不应用半截动作
- [Windows 旧控制台 ANSI 颜色支持不稳] → 颜色输出包 try/except，失败降级纯文本前缀（「思考」前缀不依赖颜色）
- [deepseek-flash 默认 effort=high 消耗思考 token] → usage.reasoning_tokens 记入 trace 可观测；`--no-thinking` 一键关闭

## Migration Plan

全新代码，无存量迁移。Task 1 提交时同步修订 AGENTS.md 与 openspec/config.yaml（deepseek-chat → deepseek-flash、目录约定补充 config.py/prompts.py/middleware.py/memory/）。回滚策略：整分支 revert 即可，运行时 data/ 目录不影响代码。

## Open Questions

（无——所有决策已在 explore 阶段由用户确认或按上述定稿；实现期发现的偏差走 Reverse Sync 流程回写 artifacts。）
