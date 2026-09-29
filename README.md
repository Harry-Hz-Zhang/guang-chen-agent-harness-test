# guang-chen-agent-harness-test

从零实现的最小可用 Agent Runtime——不依赖 langgraph / openhands / openclaw 等任何 agent 框架，仅用 `openai` 官方 SDK 直连 DeepSeek（OpenAI 兼容协议）。

- 需求原文：`doc/PRD.md`（冻结不改）
- 方案真源：`openspec/changes/build-minimal-agent-runtime/`（VSDD artifacts）
- 项目公约：`AGENTS.md`

## 一、运行方式

环境要求：Python 3.13+、[uv](https://docs.astral.sh/uv/)、DeepSeek API Key。

```bash
# 1. 安装依赖（创建 .venv 并安装 openai / pytest）
uv sync

# 2. 配置密钥（环境变量，绝不写入代码）
#    PowerShell 临时生效：
$env:DEEPSEEK_API_KEY = "sk-xxx"
#    模型名 / base_url 可选覆盖：
$env:LLM_MODEL = "deepseek-flash"
$env:LLM_BASE_URL = "https://api.deepseek.com"

# 3. 启动 REPL（src 布局需 PYTHONPATH；PowerShell 先执行 $env:PYTHONPATH = "src"）
PYTHONPATH=src uv run python -m harness

# 常用启动方式
PYTHONPATH=src uv run python -m harness --session s1        # 指定会话（已存在则续接历史）
PYTHONPATH=src uv run python -m harness --no-stream         # 关闭流式（整段输出）
PYTHONPATH=src uv run python -m harness --no-thinking       # 关闭思考模式

# 4. 运行测试（全 mock，零真实网络调用）
uv run pytest
```

REPL 内置命令（不进入 LLM）：`/exit` 退出、`/new` 切换新会话、`/sessions` 列出全部会话、`/history` 当前会话概览。

运行期产物全部落在 `data/`（已 gitignore，不入版本库）：`sessions/<id>.jsonl`（会话）、`MEMORY/`（全局长期记忆：`MEMORY.md` 索引、单条记忆文件与 `state.json` 进度）、`traces/<id>.jsonl`（调用追踪）。

## 二、系统设计（模块图）

```
用户输入
  → TraceCollector.start_trace（trace_id 注入 LoopState）
  → [middleware.before_model]  CompactionMiddleware → ContextCompressor.compact()
  → ContextBuilder.build()     系统提示词(+记忆段) + 压缩摘要 + 未压缩历史 + 当前输入
  → SessionStore.append(user message)
  → TraceCollector.start_llm_span
  → LLMClient.stream()/invoke() ─ on_event → CLI 分通道渲染（思考/正文）
  → SessionStore.append(assistant message，含 reasoning_content)
  → TraceCollector.end_llm_span(usage)
  → parse_response() → AgentDecision
      ├─ FinalAnswer → [middleware.after_model] → 返回
      └─ ToolCallBatch → 逐个 validate → 直接执行
            ├─ 成功 → tool 消息（组装时截 2000 字符）
            └─ 失败 → tool 消息（结构化错误 JSON 回传 LLM）
          → SessionStore.append + tool span → 回到 before_model（上限 15 轮）
```

```
src/harness/
├── __main__.py        # CLI：argparse + REPL 循环 + 流式分通道渲染
├── config.py          # RuntimeConfig（全部数值常量 + HARNESS_*/LLM_* 环境变量覆盖）
├── prompts.py         # SYSTEM_PROMPT / COMPACTION_PROMPT / MEMORY_*_PROMPT
├── llm.py             # LLMClient（invoke/stream 双入口）+ AIMessage/ToolCall/StreamEvent
├── parser.py          # parse_response（答案/工具调用二分）+ validate_arguments
├── loop.py            # ReactLoop（主循环 + middleware 编排 + 决策轮上限）
├── middleware.py      # Middleware 基类（before_model/after_model）
├── trace.py           # TraceCollector + JsonlExporter（OTel GenAI 命名对齐）
├── tools/             # BaseTool/ToolRegistry + calculator(ast 白名单)/search/weather/read_memory
├── session/store.py   # SessionStore（JSONL append-only，一会话一文件）
├── context/           # ContextBuilder + estimate_tokens；ContextCompressor + CompactionMiddleware
└── memory/            # MemoryStore（全局 MEMORY 目录、时间戳 md 归档与 MEMORY.md 索引）；MemorySummarizer（闲置会话增量提取）
```

关键决策（完整版见 `openspec/changes/build-minimal-agent-runtime/design.md`）：

- **工具调用协议** = DeepSeek 官方 function calling（`finish_reason=="tool_calls"` 作循环继续信号），不自造文本协议；`function.arguments` 不保证合法 JSON，parser 层显式校验、失败结构化回传。
- **压缩** = 打标记 + 结构化摘要：不删消息，仅追加 `kind:"compaction"` 记录；触发口径为「未压缩窗口」的对话轮（默认 60）或估算 token（默认 100K）；保留最近 5 轮原文，切点保证 tool_call/tool 配对不拆散。
- **思考模式** 默认开：`reasoning_content` 经 `getattr` 透传（封装在 llm.py 单点），随 assistant 消息持久化并在带 tools 的请求中每轮回传（漏传即 400）；`--no-thinking` 关闭。
- **测试策略** = 依赖注入 + FakeLLM：所有组件构造注入，pytest 全 mock 零网络；真实 API 验收走 CLI 手工操作（见下节）。

## 三、memory 的召回时机与放置方式

**召回时机**：组装上下文时（每次 LLM 调用前）。`ContextBuilder.build` 每轮请求都会调用 `MemoryStore.render_index()`——若全局记忆索引存在，则将索引内容及 `read_memory` 工具使用说明注入到 system 消息尾部；若尚无记忆则 system 提示词保持原文。所有会话共享同一份全局记忆，新增记忆在所有会话的后续轮次中立即全局生效。

**放置方式**：system 消息内追加独立段落，形如：

```
<SYSTEM_PROMPT 原文>

## 历史记忆
- 20260928-150000.md｜用户偏好中文回复（tags: 偏好, 语言）
- 20260928-150100.md｜用户的小猫名叫花花（tags: 宠物）
（如需某条记忆的完整内容，用 read_memory 工具按文件名读取）
```

**记忆的产生与提取**：

- **产生时机**：后台闲置会话扫描（`MemorySummarizer` daemon 线程周期扫描，默认 5 分钟扫描一次，会话闲置达阈值且有未提取新消息时触发）。
- **增量提取**：按各会话在 `state.json` 中的提取进度（消息 `ordinal`），仅将新产生的消息提交给 LLM，按结构化 JSON 提取核心记忆条目与标签（如用户偏好、重要事实），提取后推进进度，无新消息时不重复调用。
- **存储结构**：
  - `data/MEMORY/MEMORY.md`：全局索引文件（只追加，每行包含：记忆文件名｜首条简述/条数｜tags）。
  - `data/MEMORY/<YYYYMMDD-HHMMSS>.md`：单条记忆归档文件（正文记录归档日期与具体记忆条目）。
  - `data/MEMORY/state.json`：记录每个会话已提取到的最大消息 ordinal 进度。
- **按需详情召回**：通过内置 `read_memory` 工具，LLM 可在需要时根据索引中的文件名主动读取特定记忆的全文内容。

## 四、AI Prompt 与问题解决记录

本仓库由 AI 助手（VSDD 流程：explore → propose → apply）开发。开发过程中使用的主要提示词与工作流约束集中在 `openspec/changes/build-minimal-agent-runtime/`（proposal / specs / design / tasks / log 五类 artifact，13 个 task 全部 TDD 先红后绿），以及仓库级公约 `AGENTS.md`。

### 已解决的关键问题（摘要）

| # | 问题 | 解法 | 证据 |
| --- | --- | --- | --- |
| 1 | `deepseek-chat` 已于 2026-07-24 停用 | 主模型改 `deepseek-flash`（AGENTS.md / config 同步修订） | `openspec/.../log.md` explore 节 |
| 2 | `src/` 布局 + `package=false` 导致 CLI 不可直跑 | 全部命令统一 `PYTHONPATH=src` 前缀（AGENTS.md §5 与 tasks.md DoD 同步修订） | log.md「Task 1 Reverse Sync ×1」 |
| 3 | calculator 危险表达式逃逸（`__import__` 等） | `ast` 节点白名单递归求值，禁 eval/exec；指数上限与结果类型校验兜底 | `tests/tools/test_builtin_tools.py` 17 用例 |
| 4 | ReAct 循环中当前输入每轮出现两次 | Reverse Sync 修订数据流：首轮 build 后落库 user 消息，后续轮传空串 | log.md「Task 9 审查后 Reverse Sync ×5」 |
| 5 | openai SDK 类型层无 `reasoning_content` | `getattr` 运行时透传，封装收敛在 `llm.py` 单点（pydantic extra=allow） | `tests/test_llm.py` / `tests/test_stream.py` |
| 6 | 后台总结线程与主线程并发写 trace 文件 | `JsonlExporter` 写盘段 `threading.Lock` 互斥 | `tests/test_trace.py` |
| 7 | 会话隔离记忆无法跨会话共享且合并脆弱 | 重构为全局 MEMORY 追加归档 + MEMORY.md 索引注入 + `read_memory` 工具按需读取 | `openspec/changes/refactor-global-memory/` |

### 手工验收清单（真实 API，由人工执行后填写结果）

> 前置：`uv sync` + `$env:DEEPSEEK_API_KEY = "sk-xxx"` + `$env:PYTHONPATH = "src"`。

| # | 验收项 | 步骤 | 结果（待填） |
| --- | --- | --- | --- |
| 1 | 双窗口隔离 | 并行启动 `--session s1` 与 `--session s2` 各自对话；退出后重启 `--session s1` 续接并对早期内容追问 | 待验收 |
| 2 | 续接追问 | 同一会话「查北京天气」→「那上海呢」→「北京温度乘以 2 是多少」三次均正确返回 | 待验收 |
| 3 | 流式分通道 | 默认启动观察「思考」前缀逐字流出、正文随后；`--no-stream` 整段输出 | 待验收 |
| 4 | 压缩 | `$env:HARNESS_COMPACT_ROUNDS = "5"` 后进行 6+ 轮对话；检查 `data/sessions/<id>.jsonl` 出现 `kind:"compaction"` 记录；压缩后追问早期事实仍答对 | 待验收 |
| 5 | 最大轮次 | `$env:HARNESS_MAX_ROUNDS = "2"` 后构造持续工具调用场景，第 3 轮前返回「已达最大轮次」并正常退出 | 待验收 |
| 6 | trace 抽查 | `data/traces/<id>.jsonl` 逐行可 `json.loads`，含 `type:"llm"`/`type:"tool"` 事件与 `gen_ai.*` 字段 | 待验收 |
| 7 | 记忆 | `$env:HARNESS_IDLE_SECONDS = "3"` + `$env:HARNESS_SCAN_INTERVAL_SECONDS = "5"`，对话后闲置 3 秒等待扫描；检查 `data/MEMORY/` 生成索引与记忆文件，并在跨会话中由 `read_memory` 读取 | 待验收 |
| 8 | 工具错误回传 | 对话中诱导工具报错（如让 calculator 算 1/0），观察 LLM 收到结构化错误后向用户说明 | 待验收 |
