# Proposal — 单进程多会话并发执行（ConcurrentRunner 线程池批量 run）

## 1. 背景与动机

### 1.1 现状：并发能力「有地基、没房子」

PRD 的多窗口互不影响目前由**多进程**实现（每窗口一个 CLI 进程 + session 落盘）。
进程内的真实并发只有一对：主线程 REPL + MemorySummarizer daemon 线程。

add-todo-tool 已把**会话隔离的并发地基**打好并测试固化：

- `CURRENT_SESSION_ID` ContextVar：线程/协程天然隔离，`ReactLoop.run` 入口绑定、出口复位；
- `RuntimeState` 键控 + `threading.Lock`：公共状态按会话隔离、并发写安全；
- `testConcurrentRunsIsolated`：已证明双线程并发 `loop.run` 不同会话互不串扰。

缺的只是一个**承载并发的执行入口**：当前没有任何方式在单进程内同时跑多个 session
——`run_repl` 是严格串行的 `for line in lines`。

### 1.2 目标形态

用户一句话需求：**单进程内同时跑多个 session**。即给定 N 个 (session_id, 用户输入)
任务，runtime 用线程池并发执行 N 个 ReAct 循环，每个 agent 一个线程，各自读写各自
的 session 文件与 todos，互不串扰，结果按提交顺序返回。CLI 侧以 `--concurrent
s1:输入 s2:输入` 暴露该能力（批量执行后打印结果退出，不进 REPL）。

## 2. 线程安全事实清单（explore 结论，codegraph + 逐文件核对）

| 共享组件 | 并发多 session 下的安全性 | 依据 |
| --- | --- | --- |
| LLMClient | ✅ 安全，可单实例共享 | openai SDK（httpx）线程安全；现状 summarizer daemon 线程已与主线程并发调用同一实例 |
| SessionStore（不同 session） | ✅ 安全 | 每会话独立 jsonl 文件，各写各文件 |
| SessionStore（同一 session） | ❌ 不安全 | `_prepare_append` 先读后写无锁，docstring 已声明「同一 session id 不支持并发写」→ 批内 session_id 唯一性校验守住 |
| ReactLoop | ✅ 安全，可单实例共享 | run 的全部状态均为参数/局部变量，无实例级可变状态 |
| ToolRegistry + 全部工具 | ✅ 安全 | 注册后只读；calculator/search/weather 无实例状态，read_memory 只读文件，write_todos 已持锁 |
| TraceCollector | ✅ 安全（CPython GIL 下） | span_id/trace_id 为随机唯一键，各线程只操作自己生成的键，无同键竞争；JsonlExporter 写盘已持锁 |
| MemoryStore | ✅ 沿用现状 | 写路径单写者（仅 summarizer 线程），loop 不写 memory；read_memory 工具只读 |
| ContextCompressor / Middleware | ✅ 安全 | 每次调用只读自己 session 的文件，无跨会话共享可变状态 |
| StreamRenderer / stdout | ⚠️ 会交错乱码 | 并发模式不挂 on_event（非流式），完成后按序整体输出 |

## 3. 目标

1. **新增 `src/harness/runner.py`**：`SessionJob`（session_id + user_input）、
   `JobResult`（LoopResult 平铺 + error 字段）、`ConcurrentRunner.run_batch(jobs)`——
   校验（非空 / session_id 与输入非空白 / session_id 不重复）→
   `ThreadPoolExecutor`（max_workers = min(任务数, `max_concurrent_sessions`)）并发
   `loop.run` → 单任务异常（含 LLMError）结构化为 `JobResult.error`，不中断批次 →
   按提交顺序返回结果列表。
2. **`RuntimeConfig` 新增 `max_concurrent_sessions: int = 4`**，进
   `_HARNESS_ENV_NUMERIC_FIELDS`（env `HARNESS_MAX_CONCURRENT_SESSIONS` 覆盖）。
3. **CLI `--concurrent SESSION:输入 [SESSION:输入 ...]`**：解析（首个冒号分隔，
   输入可含冒号；格式非法给出用法提示）、与 `--session` 互斥校验、批量执行、
   按提交顺序渲染结果（会话头 + 答案 + 成败汇总行）后退出，不进 REPL、不启流式。
4. **测试先行**：新建 `tests/test_runner.py`（批内真并发证明、结果与会话文件隔离、
   重复/空 session 拒绝、单任务失败不倒批、todos 跨批隔离、并发上限受配置约束）；
   `tests/test_cli.py` 新增（spec 解析、渲染、互斥校验）。
5. **文档同步**：README 功能列表、CODEGRAPH.md 模块表、AGENTS.md 目录树补 runner.py。

## 4. 非目标（Non-goals）

1. **不改造成 server**（FastAPI/WebSocket 等）——`run_batch` 是可编程入口，server
   是其上一个薄壳，留待未来独立 change。
2. **不引入 asyncio**——全栈同步（openai sync client / 同步文件 IO），改协程需重写
   llm/loop 全链路；线程 + ContextVar 语义等价且已有测试固化。
3. **不支持同一 session 并发**——SessionStore ordinal 先读后写无锁（既有约束），
   批内重复 session_id 直接拒绝，不为它加文件锁。
4. **不做并发模式下的流式渲染**——多线程写同一 stdout 必然交错；整批完成后按序输出。
5. 不改 loop / parser / session / memory / trace / renderer 既有模块（零侵入，
   并发语义全部由 runner 层组合既有线程安全组件实现）。

## 5. 验收标准

1. `uv run pytest` 全绿（189 → 约 203 项：+10 runner、+4 CLI）。
2. **真并发可验证**：测试以并发计数器证明 `run_batch` 内至少 2 个任务同时在
   LLM 调用中（串行执行无法通过）。
3. **隔离可验证**：并发批次中各 session 文件各归各（user/assistant/tool 序列完整），
   todos 按会话隔离。
4. **健壮性可验证**：单任务 LLMError 只影响该任务（JobResult.error 非空），其余
   任务结果正常；重复 session_id / 空 session_id / 空输入 / 空任务列表均 ValueError。
5. **并发上限可验证**：`max_concurrent_sessions=2` 时 10 任务并发峰值 ≤ 2。
6. **简单性预算**：`runner.py` ≤ 130 行；无 print、全函数类型注解 + 中文 docstring。
7. CLI `--concurrent` 与 `--session` 同给时给出提示并以非 0 码退出；文档三处同步；
   `codegraph sync` 成功。

## 6. 风险与权衡

| 权衡 | 决策 | 理由 |
| --- | --- | --- |
| 线程池 vs asyncio | 线程池 | 全栈同步，asyncio 需重写全链路；ContextVar 对线程原生隔离，testConcurrentRunsIsolated 已固化该模型 |
| 共享单例 loop/LLMClient vs 每任务重建 | 共享 | ReactLoop 无实例可变状态；LLMClient 连接复用；组件装配一次 |
| 同 session 并发加文件锁 vs 批内拒绝 | 拒绝 | SessionStore 既有约束；单 session 并发写没有合理语义（消息序会乱），拒绝更诚实 |
| TraceCollector 加锁 vs 依赖键唯一性 | 不加锁 | 每线程只操作自己生成的随机唯一键，CPython GIL 下 dict 单键操作原子、不同键无竞争；写盘锁已存在 |
| 失败语义：异常上抛 vs 结构化进结果 | 进 JobResult.error | 对齐 REPL「LLM 失败可恢复」哲学与 D23「错误结构化回传」契约，单任务失败不倒整批 |
| max_workers 默认值 | 4 | CLI 单机演示够用；env 可覆盖；防止海量任务打爆 API 并发 |
