# Exploration Handoff — 从零实现最小可用 Agent Runtime

> 生成于 2026-09-24，VSDD explore 阶段产物。
> 主题：按 doc/PRD.md（冻结需求）+ doc/my-plan.md（用户草稿）从零实现最小可用 Agent Runtime，不依赖任何 agent 框架。
> 决策树 44 节点已全部 resolve，无客观阻塞，可进入 propose。

## 决策清单

### 战略与范围

| # | 决策点 | 结论 | 来源 |
| --- | --- | --- | --- |
| D8 | change 划分 | 单个 change 打包全部范围，按 task 分波实施 | 用户原话：「生成一个 change 以及具体的实现方式，开始！然后给我 propose，先调研相关的方案」 |
| 范围 | PRD 核心 | ReAct 循环 / 工具注册（≥3 个工具）/ LLM 输出解析 / session 隔离与持久化 / context 管理与基础压缩 / 异常处理 / trace / pytest 测试 | doc/PRD.md:8-49 |
| D4 | 流式输出 | 纳入本 change：think/text 分离的流式管道，用户要求 demo 审核 | 用户选择：「流式输出（推荐）」；doc/my-plan.md:22「重点实现 stream 流式输出……给出一个demo代码，让我审核」 |
| D5 | 闲置会话后台总结 | 纳入：后台协程定期扫 session 文件 mtime，超 2h 未活跃自动总结入记忆 | 用户选择：「闲置会话后台总结（推荐）」 |
| D6 | middleware 架构骨架 | 纳入：langchain 式 bind_tools + before_model/after_model/wrap_tool_call 钩子，压缩做成 middleware | 用户选择：「middleware 架构骨架（推荐）」 |
| D7/D18 | trace 程度 | 自研 OTel GenAI 命名对齐的 JSONL（TraceCollector + span 树）；langfuse 不进主流程 | 用户选择：「OTel 对齐 trace（推荐）」；langfuse 选项未选 |
| D9 | 非目标 | 不做：langfuse 主流程接入、Memos 外挂后端、embedding 语义去重、多 provider 抽象、复杂压缩策略、单问单答 CLI、Web UI | 由用户各选择项排除 + doc/PRD.md:40「context 过长要有基础的压缩，复杂的压缩不用在这里实现」 |
| D10 | PRD 提交物 | README（运行方式/系统设计/memory 召回时机与放置方式说明）+ AI Prompt 与问题解决记录 + github 链接，作为本 change 收尾 task | doc/PRD.md:51-55 + D8 单 change 推论 |
| D11 | 真实 API 与测试的调和 | pytest 全 mock（假模型）；真实 API 走 CLI 手工验收，README 记录验收步骤，不写自动化冒烟脚本 | AGENTS.md:108「测试必须用假模型 / mock」+ doc/PRD.md:53「需要使用真实的 LLM Api」+ 用户委托确认 |

### 选型

| # | 决策点 | 结论 | 来源 |
| --- | --- | --- | --- |
| D12 | 工具调用协议 | 官方 function calling（tool_calls 结构化字段 + finish_reason="tool_calls" 作循环控制信号），不自造 ReAct 文本协议；思考模式默认开，reasoning_content 提取思考过程，消息结构持久化该字段并每轮回传 | 用户选择：「function calling + 思考开（推荐）」；调研：api-docs.deepseek.com |
| D13 | session 持久化 | 本地 JSONL：一个会话一个文件（data/sessions/&lt;session_id&gt;.jsonl），append-only，第一行会话元数据，每行 {timestamp, ordinal, item}（Codex rollout 同款） | 用户选择：「本地 JSONL + md（推荐）」；调研：github.com/openai/codex（codex-rs/rollout） |
| D14 | 长期记忆存储 | 本地 md 文件夹：data/memory/&lt;session_id&gt;/MEMORY.md（索引）+ 细碎 md（具体条目，frontmatter 记 hash/created/tags） | 用户选择：「本地 JSONL + md（推荐）」；用户原话 doc/my-plan.md:14「如果保持轻量的话，应该直接本地磁盘开一个文件夹……一个是 MEMORY.md，写入所有记忆的目录」 |
| D15 | 记忆去重与更新 | 两层：写入时规范化内容 SHA-256 哈希防程序性重复；低频 LLM 合并时做 ADD/UPDATE/DELETE/NOOP 判断（mem0 四动作）；不上 embedding | 用户 Q4 选择认可 + 调研：mem0（arXiv:2504.19413）、Codex memories（github.com/openai/codex/blob/main/codex-rs/memories/README.md） |
| D16 | 压缩策略 | AgentScope 标记法：不删消息、给被压消息打标记 + 5 字段结构化摘要（task_overview / current_state / important_discoveries（重要发现，无工具报错反思的特殊语义）/ next_steps / context_to_preserve），被压消息归档 JSONL 留全量，摘要回填上下文；工具结果进上下文截 2000 字符；不采纳参考项目的超大工具结果卸载与入参截断（非目标）<br>**2026-09-24 修订**：去掉「专收工具报错反思」表述，反思不作为独立机制 | 用户数值确认 + 调研：doc.agentscope.io（task_agent/task_memory）+ explorer 核实参考代码；修订来源：用户指令（2026-09-24） |
| D17 | 压缩形态 | middleware：压缩检查挂 before_model 钩子，压缩失败降级告警不阻断主循环 | 用户选择 Q2 + 用户原话 doc/my-plan.md:24「关于上下文压缩做成 middleware 的形式」 |
| D19 | LLM 客户端抽象 | 借鉴 langchain 的薄封装：invoke/stream 双入口 + bind_tools 语义 + AIMessage 结构（content / reasoning_content / tool_calls（args 为解析后 dict）/ usage）；砍 Runnable 全家桶、batch、callbacks、多 provider 抽象 | 用户原话 doc/my-plan.md:18「对于模型调用的http进行抽象，具体的话参考 langchain 的做法」+ langchain 调研 |
| D21 | token 计数 | 双层：字符近似（≈2.5 字符/token + 消息开销）作压缩触发判断；API usage 累计进 trace（流式需 stream_options.include_usage） | 用户数值确认（第 ⑧ 项） |
| D22 | CLI 形态 | REPL 交互式（--session 续接历史会话，支持 REPL 内部命令），不做单问单答 | 用户选择：「REPL 交互式（推荐）」 |

### 数值/常量

| # | 决策点 | 结论 | 来源 |
| --- | --- | --- | --- |
| D36 | 单次请求 ReAct 最大轮次 | 15 | 用户选择：「全部按默认（推荐）」 |
| D37 | 压缩 token 触发阈值 | 估算 token ≥ 100K（用户原定 80% 在 1M 窗口下永不触发，已重校准为绝对值） | 用户选择：「全部按默认（推荐）」；原倾向 doc/my-plan.md:26 |
| D38 | 压缩轮次触发阈值 | 对话轮次 ≥ 60<br>**术语明确（2026-09-24）**：此「轮」= 会话累计的 user/assistant 对话对数，与 D36 的「单次请求决策轮」是两个不同量，全文已分别定名避免撞词 | 用户原话 doc/my-plan.md:27「另外就是对话轮次达到 60轮」+ 数值确认；术语修订来源：用户指令（2026-09-24） |
| D40 | 闲置判定 / 扫描间隔 | 2h 未活跃判定 + 每 5 分钟扫描一轮 | 用户原话 doc/my-plan.md:14「大概 2h」+ 数值确认 |
| D41 | LLM 超时 / 重试 | 60s / 重试 2 次 | 用户选择：「全部按默认（推荐）」 |
| D42 | 工具执行超时 | 30s，超时错误结构化回传 LLM 决定重试 | 用户选择：「全部按默认（推荐）」 |
| D43 | 压缩保留最近消息 | ~~20 条~~ → **最近 5 轮原文（≙ 10 条 user/assistant 消息）**，按「对话轮」边界切分并连带保留轮内工具配对消息（实际条数可多于 10） | 原「条数」口径在带工具的会话中不保证轮数（工具消息占配额）；用户 2026-09-24 裁定改按轮计、值取 10 条 |
| D44 | 截断上限 | 工具结果进上下文截 2000 字符，归档 JSONL 留全量 | 用户选择：「全部按默认（推荐）」 |

### 模型与硬事实（B 类，已核实）

| # | 决策点 | 结论 | 来源 |
| --- | --- | --- | --- |
| D39 | 默认模型与窗口 | deepseek-flash（V4.1-Flash）；deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用；现役两模型均 1M 上下文、384K 最大输出。**propose 须同步修订 AGENTS.md 主模型约定（reverse sync）** | 用户选择：「deepseek-flash（推荐）」；调研：api-docs.deepseek.com（news260424、quick_start/pricing、updates） |
| D1 | smart-delivery-agent 参考路径 | 存在且完整可借鉴：middleware 压缩链（eviction→compaction 顺序装配）、压缩前全量归档 JSONL、摘要以 user 角色特殊标记消息回填、token 字符近似估算、溢出强制压缩兜底 | explorer 核实：D:\Users\hongze01.zhang\PycharmProjects\smart-delivery-agent\smart-delivery-agent-service\src\vip_ads_agent\agent\executor\context（compaction_middleware.py:21、conversation_compactor.py:64、compaction_config.py:133-145、session_transcript_writer.py:23 等）；agent\context 为跨会话记忆引擎（MemOS HTTP 后端，本项目不采用） |
| D2 | openai SDK 能力 | 3.19.2；流式 delta 类型定义无 reasoning_content，但 pydantic extra="allow" 使其运行时可透传（代码用 getattr 取）；流式 usage 需 stream_options={"include_usage": true} 且在最后一个 chunk | explorer 核实：.venv\Lib\site-packages\openai\_version.py:2、types\chat\chat_completion_chunk.py:74-93、_models.py:128-130 |
| D24 | 目录布局 | src/harness/{\_\_main\_\_,loop,llm,parser,trace}.py + tools/{base,registry,calculator,search,…}.py + session/store.py + context/{builder,compressor}.py；tests/ 与 src 镜像 | AGENTS.md:41-58 |
| D33 | CLI 参数 | --session 已定（python -m harness --session s1）；REPL 内部命令集委托 propose | AGENTS.md:91 + 用户委托 |
| D35 | 提示词模板 | 集中放置（具体文件/目录位置委托 propose） | AGENTS.md:73「提示词模板集中放置，不散落在业务逻辑里」 |

### 委托 propose 的细节（C 类，用户明确延迟决策）

| # | 决策点 | 来源 |
| --- | --- | --- |
| D20 | 流式 think/text 分离的具体事件设计（StreamEvent 枚举、tool_calls 增量聚合器、CLI 渲染方式） | 用户延迟决策：「我让 propose 阶段定（推荐）」 |
| D23 | 工具错误回传 JSON 具体格式（方向已定：结构化回传 LLM） | 用户延迟决策：「我让 propose 阶段定（推荐）」 |
| D25-D32 | 命名：loop 引擎类、LLM 客户端类、parser 输出类型、BaseTool 接口、ToolRegistry、SessionStore、AgentContext 结构与字段、trace 类与事件格式 | 用户延迟决策：「我让 propose 阶段定（推荐）」 |
| D34 | 压缩摘要消息标记与归档文件命名（参考项目 \_\_compaction\_\_summary\_\_ / sessions/{id}.jsonl 可照搬） | 用户延迟决策：「我让 propose 阶段定（推荐）」 |

### 关键调研事实（供 propose 直接引用）

- **DeepSeek 现役模型**：deepseek-flash / deepseek-v4-pro，1M 上下文，均支持 function calling；deepseek-flash 默认开思考模式（reasoning_effort=high），关闭需 extra_body={"thinking": {"type": "disabled"}}（openai SDK 非标准参数须走 extra_body）
- **思考模式坑**：带 tools 的请求，每轮必须完整回传 reasoning_content（含未调工具的轮次），漏了 400；思考模式下 temperature 无效；tool_choice 只准 auto
- **流式结构**：delta.tool_calls 每个调用首片带 id/type/function.name，后续片只带 function.arguments 增量，按 index 聚合后统一 json.loads；delta.reasoning_content 与 delta.content 分阶段流出
- **官方警告**：function.arguments 不保证合法 JSON——parser 必须显式校验并结构化回传错误（正好落进 AGENTS.md「对外部输入显式校验」规范）
- **工具结果回传形状**：{"role": "tool", "tool_call_id": ..., "content": ...}，与 assistant 消息的 tool_calls[].id 配对
- **langchain 借鉴清单**：AIMessage 双层结构（标准化字段 + 原始 metadata）、invoke/stream 门面 + 私有钩子、bind_tools 语义、tool_call_id 配对、reasoning_content 双通道、middleware 钩子形状；砍除：Runnable 组合体系、batch、callbacks、多 provider 工厂、astream_events 完整事件系统
- **AgentScope 压缩**：CompressionConfig(enable, trigger_threshold, keep_recent) + 消息打标记（COMPRESSED）+ 5 字段结构化摘要，摘要在下次压缩时链式纳入
- **Codex 参考**：rollout JSONL（年/月/日分桶 + SessionMeta 首行 + ordinal）；memories 双阶段（逐会话抽取 → 全局合并成 MEMORY.md），按 usage_count/last_usage 淘汰
- **trace 方案**：langfuse 自托管需 6 容器（过重）；自研 = TraceCollector + TraceExporter(Protocol) + JsonlExporter，事件字段对齐 OTel GenAI（gen_ai.operation.name / gen_ai.provider.name="deepseek" / gen_ai.usage.input_tokens / gen_ai.conversation.id=session_id / gen_ai.tool.name），trace_id 32 位 hex（W3C），落盘即树（trace_id/span_id/parent_span_id）

## 客观阻塞

无。

## 下一步建议

1. **进入 propose**（用户已预先授权：「然后给我 propose，先调研相关的方案」——4 份调研均已完成）：建议 change 名 `build-minimal-agent-runtime`；复杂度 🔴 standard（AGENTS.md:114 已定 standard 模式，新建多模块、task 数 > 5）
2. **propose 必做**：
   - 修订 AGENTS.md 主模型 deepseek-chat → deepseek-flash（reverse sync，改前向用户确认）
   - proposal.md 含非目标小节（openspec/config.yaml:39）
   - design.md 含模块划分 + 关键数据结构 + ≥1 处被否决备选（config.yaml:48），并给出 C 类委托项（D20/D23/D25-D32/D34）的完整命名表
   - tasks.md 每 task 含 RED/GREEN/ASSERT/DoD，RED ≥ 5 条（config.yaml:43-47）
   - 流式 demo 审核安排：作为流式 task 的 DoD（用户要求 demo 审核，doc/my-plan.md:22）
3. 实施顺序建议按依赖推进：core（loop/parser/tools/session）→ context（压缩/middleware）→ 流式 → trace → 记忆与闲置总结 → CLI/README 收尾
