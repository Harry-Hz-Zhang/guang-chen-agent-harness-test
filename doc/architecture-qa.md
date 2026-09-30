# Agent Runtime 架构问答


## 模块一：Context / Performance


### 1.2 session 聊了 200 轮 context 快爆，怎么做压缩？如何保证压缩后仍流畅？

- **触发**：双阈值（`compact_rounds=60` 轮 或 估算 token 达 `compact_tokens=100_000`），且按「未压缩窗口」计数——已压缩区间不计入，压缩后计数回落，不会每轮重复触发；
- **保留近期原文**：保留最近 `keep_recent_rounds=5` 轮逐字原文，只压更早历史——近期细节（用户刚说的约束、刚出的结果）不丢，这是「流畅」的第一保障；
- **结构化摘要**：被压区间用固定 5 字段提示词（`COMPACTION_PROMPT`）生成结构化摘要，不是流水账，目标/事实/决定类关键信息有专门字段；
- **链式压缩**：再次触发时旧摘要并入压缩输入，摘要不因多级压缩而丢失；
- **原始不删**：append-only，压缩记录追加写入会话文件（区间上限 ordinal ＋ 摘要），原始消息永不删除，会话文件是真源；
- **失败降级**：压缩 LLM 调用失败仅告警、不阻断主循环（`CompactionMiddleware` 挂在 `before_model` 钩子）。

**流畅性保障**（关键点）：

1. **结构完整性**：切点保证 assistant(tool_calls)/tool 配对不拆散（必要时回退到轮起点）——拆散的配对 API 直接报错，这是硬约束，不是可选项；
2. **摘要的身份明确**：摘要以带明文标记的独立消息注入（「以下为此前对话的压缩摘要」），模型明确知道这是摘要而非用户原话，不会把摘要当指令执行；
3. **长短期分层**：摘要管长期语义、近 5 轮原文管短期细节，用户追问近期细节时模型有原文可依。

---

## 模块二：Memory

### 2.1 熟悉半个月后，用户问了以前问过的问题，memory 召回如何做更合理？

**问题**：和聊天 Agent 熟悉半个月后，用户问了一个以前问过的问题。Agent 如何做 memory 召回更合理？

**简答**：

本项目现状是两层召回：`MEMORY.md` 索引全量注入 system（「## 历史记忆」段＋`read_memory` 使用提示，[builder.py](../src/harness/context/builder.py)），正文由 LLM 用 `read_memory` 按文件名读取（[read_memory.py](../src/harness/tools/read_memory.py)）。

这个形态合理之处：**索引常驻**（每条一行简述，token 成本低）、**正文按需**（是否读全文由模型判断，召回决策交给智能而不是硬编码规则）。

半个月规模后的演进一步步走：

- **检索式召回**：索引行数增长后全量注入不可扩展，应改为对索引做 embedding/关键词检索、只注入 top-k 相关条目（RAG over memory）；
- **时效衰减**：记忆带日期，召回优先近期；用户偏好变化后旧记忆不应压过新记忆；
- **去重与冲突消解**：本项目刻意「只追加、不去重、不合并」（demo 决策，[store.py](../src/harness/memory/store.py)），生产必须做记忆合并与失效标记——否则半个月后同一问题会召回多条互相矛盾的记忆；
- **注入位置**：召回结果作为带来源与日期标记的独立段注入 system，不伪装成对话历史。

---

## 模块三：Task

### 3.1 长程任务执行中忘掉目标：解决方案与优缺

**问题**：对于长程任务，大模型执行一段时间可能会忘掉目标，你知道哪些解决方案，有什么优缺？

**简答**：

四类方案：

1. **目标重申**（prompt 层）：每轮把任务目标放在 system 或上下文首尾。优点：便宜；缺点：占 token，且长上下文存在中段注意力衰减（lost in the middle），重申≠记住；
2. **计划外置**（todo/plan 文件）：任务拆 checklist 存文件，每完成一步更新。优点：状态显式、可审计（Claude Code 的 TodoWrite 即此路线）；缺点：依赖模型遵守「更新纪律」，漏更新即失效；
3. **阶段性摘要接力**：压缩摘要中显式保留「目标＋当前进度＋下一步」。本项目走此路线（5 字段结构化摘要），单请求内另有 `max_rounds=15` 轮上限兜底防无限漂移；
4. **外层编排/子任务分解**：runtime 或主 agent 把长任务拆成子任务，各子任务独立上下文、只回传结论。优点：隔离彻底，单个子上下文不会过长；缺点：协调成本高、跨子任务信息传递有损耗。

实践上组合使用：外层拆任务＋子上下文内目标重申＋摘要接力兜底。


## 模块四：Tool / Session Runtime

### 4.1 异步工具的执行与完成通知设计

**问题**：Agent 工具有同步和异步两类。异步工具不能让用户一直等，但结果依然重要。你会如何设计异步工具执行和完成通知？

**简答**：

现状：本项目工具全部同步执行（`loop._handle_tool_call` 直接 `tool.execute`，结果同步回传）。异步工具的合理设计：

1. **接口分层**：`BaseTool` 增加 `is_async` 标记或独立 AsyncTool 基类；异步工具 `execute` 立即返回 `job_id`；
2. **回传协议**：loop 把「已受理」结构化结果（job_id＋预期行为）作为 role=tool 消息落库，LLM 据此告知用户「已提交，完成后通知」——这保持了 assistant(tool_calls)/tool 配对完整，**ReAct 循环本身无需感知异步**；
3. **完成事件**：后台执行器把结果写 job 存储（`data/jobs/*.json`：job_id、状态、结果），完成后向 session 投递「工具完成事件」；
4. **通知三路径**：(a) 用户下次交互时，完成结果作为新消息注入上下文，由 LLM 主动汇报；(b) 主动推送到 renderer/通知渠道；(c) LLM 用查询工具（如 `check_job`）主动轮询——覆盖「系统推、用户问、模型查」三种时序。

### 4.2 session busy 时新消息 / 异步完成事件到达，runtime 如何处理？

**问题**：如果 session state 为 busy，此时用户又发来新消息，或者异步工具完成事件也到达，runtime 应该如何处理？

**简答**：

核心是 **mailbox 串行模型**：

1. **每 session 一个入站事件队列**：busy 期间用户消息与工具完成事件一律入队，当前 run 结束后按 FIFO drain，每条事件触发一次新的 `loop.run`；
2. **同 session 必须串行**：本项目 `SessionStore` 的 ordinal 先读后写且无锁（[store.py](../src/harness/session/store.py) 决策 6：同 session 不支持并发写）——串行是**正确性约束**，不只是简化；
3. **用户消息的可选打断**：用户明确要求取消时支持 interrupt（置 cancel 标志，循环在轮间检查点安全退出），比排队到自然结束体验好，但要求工具幂等/可安全中止；
4. **忙碌反馈**：入队时立即回执「已收到，当前任务完成后处理」，避免用户以为消息丢失；
5. **现状**：CLI 单线程 REPL 输入天然逐行排队（stdin 迭代器即 mailbox 的退化形式），真正的队列需求来自引入异步工具之后。

---

## 模块五：Agent Runtime 架构对比

### 5.1 Claude Code 的工具输出方式 vs OpenAI-compatible function calling

**问题**：Claude Code 的工具输出方式和国内 GLM / 豆包等 OpenAI-compatible function calling 有什么不同？他们各自这样设计的优缺点是什么？

**简答**：

**协议层差异**：

- **OpenAI-compatible**（本项目，DeepSeek/GLM/豆包均走此协议）：**分离式**——assistant 消息带 `tool_calls` 数组（id＋name＋arguments JSON 字符串），工具结果用独立 role=tool 消息携带，靠 `tool_call_id` 配对；流式时参数是分片，需按 index 聚合拼接后统一 `json.loads`（本项目 `collect_stream`）；
- **Anthropic/Claude**：**内嵌式**——调用与结果都是 content blocks（assistant 的 `tool_use` block，下一轮 user 消息里的 `tool_result` block），配对在消息结构内部完成；Claude Code 在此之上把工具输出**文本化呈现**（带标记的输出段、system-reminder 注入运行时提示），把工具输出当「环境反馈流」而非结构化往返。

**优缺点**：

- OpenAI 式：生态最广、参数是 JSON 可强校验（本项目 `validate_arguments` 按 Schema 校验必填/未知字段）；缺点是 arguments 为字符串（非法 JSON 需兜底——本项目 args=None 保留原文回传纠错）、配对约束强（拆散即 API 报错，压缩切点保护专门处理）、长文本结果要过字符串转义；
- Claude 式：`tool_result` 是原生内容块，长输出（bash 日志等）直接放不受转义折磨，标记风格对模型更「原生」（训练分布友好）、人类可读性好、软控制灵活；缺点是格式约束弱于 Schema 校验、更依赖模型自觉、协议绑定单一厂商。