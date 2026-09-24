# Proposal — build-minimal-agent-runtime

## Why

doc/PRD.md 要求从零实现一个最小可用 Agent Runtime（不依赖 langgraph/openhands/openclaw 等任何 agent 框架），当前仓库除公约与空目录外没有任何代码。exploration 阶段（见 `../../last-exploration-handoff.md`，44 个决策点已全部 resolve）已完成 4 项技术调研，并发现一个硬事实：公约约定的主模型 `deepseek-chat` 已于 2026-07-24 被 DeepSeek 官方停用，必须同步修订。

## What Changes

- 新增 `src/harness/` 完整 runtime 包（约 12 个模块）：
  - `llm.py` DeepSeek 客户端薄封装（invoke/stream 双入口，思考模式默认开，reasoning_content 透传）
  - `parser.py` LLM 输出解析（最终答案 / 工具调用二分，arguments 显式校验）
  - `loop.py` ReAct 主循环（官方 function calling 协议，单次请求决策轮上限 15，middleware 编排）
  - `middleware.py` 中间件基类（before_model / after_model / wrap_tool_call 钩子）
  - `tools/` 工具注册机制 + 4 个内置工具（calculator / search(mock) / weather(mock) / todo）
  - `session/` JSONL 会话存储（一个会话一个文件，append-only，隔离 + 持久化）
  - `context/` 上下文组装 + 压缩（会话累计对话轮 ≥60 或估算 token ≥100K 触发，带明文标记的摘要消息替代早期历史，保留最近 5 轮（10 条）原文）
  - `memory/` 长期记忆（MEMORY.md 索引 + 细碎 md + SHA-256 去重 + LLM 四动作合并）+ 闲置会话后台总结（2h 判定 + 5 分钟扫描）
  - `trace.py` OTel GenAI 命名对齐的 JSONL trace（trace/span 树，LLM 调用与工具调用全覆盖）
  - `prompts.py` 提示词集中放置
  - `__main__.py` REPL 交互式 CLI（流式渲染，思考/正文分通道）
- 新增 `data/` 运行时目录（sessions/ memory/ traces/ todos/，gitignore）
- **修订 `AGENTS.md` 与 `openspec/config.yaml`**：主模型 `deepseek-chat` → `deepseek-flash`（官方已停用旧名，reverse sync，用户已在 explore 阶段确认）；目录约定补充 `middleware.py` / `memory/` / `prompts.py`
- 新增 `tests/` 镜像测试（全部 mock，不调真实 API）

## Capabilities

### New Capabilities

- `agent-loop`: ReAct 主循环——接收输入 → LLM 决策（直接回复 vs 工具调用）→ 执行工具 → 判断继续或返回；最大轮次限制；工具错误结构化回传
- `tools`: 工具注册机制（名称 + 描述 + 参数 JSON Schema，LLM 基于 Schema 自主决策）；内置 calculator / search / weather / todo 四个工具
- `session`: 会话隔离与持久化——同用户多窗口互不影响，随时续接；JSONL append-only 存储
- `context`: 上下文组装（系统提示词 + 记忆召回 + 历史消息）与超长压缩（触发条件、标记、结构化摘要、尾部保留）
- `memory`: 长期记忆存储（哈希去重 + LLM 合并）与闲置会话后台总结
- `trace`: LLM 调用与工具调用的 trace 记录（OTel GenAI 命名对齐，JSONL 落盘，span 树）
- `cli`: REPL 交互（多轮对话、会话管理命令、流式渲染、思考/正文分离显示）

### Modified Capabilities

（无——全新项目，openspec/specs/ 当前为空）

## Impact

- 新增代码：`src/harness/**`、`tests/**`（与 src 镜像）
- 修订文档：`AGENTS.md`（主模型名 + 目录约定）、`openspec/config.yaml`（context 中模型名）
- 依赖：不变（openai>=1.60.0 + pytest，**零新增依赖**）
- `doc/PRD.md` 冻结不改
- 运行时产物：`data/` 目录（会话/记忆/trace/待办），已加入 .gitignore

## 非目标（Non-goals）

- 不接入 langfuse / OTel SDK / 任何外部观测服务（自研 JSONL；README 说明未来接 langfuse 只需换 exporter）
- 不做 Memos / MemOS 外挂存储后端（本地文件为唯一真源）
- 不做 embedding / 向量检索的语义去重与召回（哈希 + LLM 判断已覆盖需求）
- 不做多 provider 抽象（仅 DeepSeek 的 OpenAI 兼容协议；LLM_MODEL/LLM_BASE_URL 可覆盖）
- 不做复杂压缩策略（超大工具结果卸载、动态 token 预算、溢出强制压缩重试等，doc/PRD.md:40 明确「复杂的压缩不用在这里实现」）
- 不做跨会话共享记忆（记忆按 session 隔离）
- 不做单问单答 CLI 模式、Web UI、HTTP 服务
- 不做自动化真实 API 冒烟脚本（真实 API 验收走 CLI 手工操作，步骤记录在 README）
- 不做会话内「反思」机制：工具报错仅结构化回传给 LLM 供其当场决策（重试/换工具/向用户说明），不写入长期记忆、不派生反思条目、不影响后续请求组装（见 agent-loop spec「工具错误回传无副作用」）

## 验收标准（可验证）

1. `uv run pytest` 全部通过且 0 失败；测试过程无任何真实网络调用（LLM 客户端全 mock）
2. 双窗口隔离：并行启动 `uv run python -m harness --session s1` 与 `--session s2`，各自对话内容互不串扰；退出后重启 `--session s1` 能续接历史并对早期内容追问
3. 带工具追问：同一会话内「查北京天气」→ 纯对话追问「那上海呢」→ 带工具追问「北京温度乘以 2 是多少」，三次均正确返回（工具调用与上下文保持均生效）
4. 流式输出：CLI 输出中思考内容与正文分通道渲染（思考带灰色「思考」前缀逐字流出，正文随后流出）；`--no-stream` 时改为整段输出
5. 压缩：把轮次阈值配置调小（如 5）后进行 6+ 轮对话，`data/sessions/<id>.jsonl` 出现 `kind:"compaction"` 记录；压缩后对早期事实追问仍能答对（摘要生效）
6. 最大轮次：把最大轮次配置调小（如 2）后构造持续工具调用场景，第 3 轮前返回「已达最大轮次」提示并正常退出，不死循环
7. trace：`data/traces/<session_id>.jsonl` 每行均可 `json.loads`，包含 `type:"llm"` 与 `type:"tool"` 两类事件，字段含 `gen_ai.usage.input_tokens` / `gen_ai.tool.name` 等 OTel 命名；工具失败时事件 `status:"error"` 且含结构化 error
8. 记忆：闲置判定时长配置调小（如 3 秒）+ 触发一次后台扫描后，`data/memory/<session_id>/MEMORY.md` 生成且含会话总结；向同一会话写入两条完全相同的记忆，文件数不增加（哈希去重生效）
9. 工具错误回传：mock 工具抛异常后，assistant 收到 `{"error": {...}}` 结构化消息并可决定重试或向用户说明（trace 中可见 error 记录）
10. README 含：运行方式、系统设计（模块图）、memory 的召回时机与放置方式说明、AI Prompt 与问题解决记录
