# Tasks — build-minimal-agent-runtime

> complexity: 🔴 standard | phase: propose

## 任务依赖关系

```
T1 骨架+config ─┬─> T2 registry ──> T3 内置工具 ──────────────┐
                ├─> T4 session store ──┬─> T7 builder+middleware ─┐
                │                      └─> T12 memory+summarizer   │
                ├─> T5 llm.invoke ─┬─> T6 parser                  │ ├─> T9 loop ─> T10 compressor ─> T13 CLI+README
                │                  ├─> T11 stream                 │ │
                │                  └──────────────────────────────┴─┘
                └─> T8 trace ──────────────────────────────────────┘
```

- **可并行**：Wave 2（T2/T4/T5/T8 互不共享文件）、Wave 3（T3/T6/T12）、Wave 4（T7/T11）——各自独立文件与测试
- **必须串行**：T9（loop）汇聚 T2-T8 全部接口，为阻塞点；T10 依赖 T9 的 middleware 编排；T13 汇聚全部
- **Worktree 分组建议**：若并行实施，Wave 2 可开 4 个 worktree（T2/T4/T5/T8），汇合点为 T9 前 rebase；单分支串行执行（AGENTS.md 原子 commit 约定）为默认
- **共享状态注意**：T1 创建 src/harness/__init__.py 与 tests/ 骨架，所有 Wave 2+ task 依赖它先行提交

## Wave 1

- [x] Task 1: 搭建包骨架与 RuntimeConfig 并修订 AGENTS.md
  - complexity: 🟢
  - files: Create `src/harness/__init__.py`、`src/harness/config.py`、`tests/__init__.py`；Modify `AGENTS.md`、`openspec/config.yaml`、`.gitignore`（追加 `data/`）
  - RED:
    - `TestRuntimeConfig#testDefaultValues`（直接构造 RuntimeConfig 返回默认值 → 断言 model=="deepseek-flash"、max_rounds==15、compact_rounds==60、compact_tokens==100000、keep_recent_rounds==5、idle_seconds==7200、scan_interval_seconds==300、llm_timeout_seconds==60.0、llm_max_retries==2、tool_timeout_seconds==30.0、tool_result_max_chars==2000）
    - `TestRuntimeConfig#testEnvOverrideModel`（monkeypatch 环境变量 LLM_MODEL 返回 "m1"、LLM_BASE_URL 返回 "http://x" → from_env() 构造后 model=="m1"、base_url=="http://x"）
    - `TestRuntimeConfig#testHarnessEnvOverride`（monkeypatch HARNESS_MAX_ROUNDS 返回 "3" → from_env() 后 max_rounds==3，其余默认不变）
    - `TestRuntimeConfig#testInvalidEnvFallsBack`（monkeypatch HARNESS_MAX_ROUNDS 返回 "abc"（非法整数）→ from_env() 不抛异常、max_rounds 回退为默认 15）
    - `TestRuntimeConfig#testThinkingAndStreamFlags`（构造 config 返回 thinking_enabled=False、stream_enabled=False → 断言两开关为 False 且其余字段仍为默认值）
  - GREEN:
    - `uv run pytest tests/test_config.py -q`（全部转绿）
  - ASSERT:
    - 默认值逐项精确断言（上表 11 项），不允许只断言「字段存在」
    - 非法环境变量回退路径不抛异常、不产生 None
  - DoD:
    - `tests/test_config.py` 全部转绿 + `PYTHONPATH=src uv run python -c "import harness"` exit 0（src 布局下普通 Python 进程需显式 PYTHONPATH，仅 pytest 自动注入搜索路径，PowerShell 先 `$env:PYTHONPATH="src"`）+ AGENTS.md 含 "deepseek-flash" 且目录约定含 config.py/prompts.py/middleware.py/memory/ + .gitignore 含 data/
  - 最小验证: `uv run pytest tests/test_config.py -q`

## Wave 2

- [x] Task 2: 实现 BaseTool 抽象与 ToolRegistry
  - complexity: 🟡
  - files: Create `src/harness/tools/__init__.py`、`src/harness/tools/base.py`、`src/harness/tools/registry.py`、`tests/tools/__init__.py`、`tests/tools/test_registry.py`
  - RED:
    - `TestToolRegistry#testRegisterAndGet`（构造 fake BaseTool（name="calc"、description="计算器"、parameters={"type":"object",...}）注册 → get("calc") 返回同一实例且 name/description/parameters 与注册时一致）
    - `TestToolRegistry#testGetUnknownRaises`（get("nope")（注册表中无该名）→ 抛 ToolNotFoundError 且异常消息含 "nope"）
    - `TestToolRegistry#testDuplicateRegisterRaises`（注册 name="calc" 两次（第二次为另一实例）→ 抛 DuplicateToolError，且 get("calc") 仍返回第一个实例）
    - `TestToolRegistry#testToOpenaiToolsFormat`（注册 2 个 fake 工具 → to_openai_tools() 返回长度 2 的列表，每项为 {"type":"function","function":{"name","description","parameters"}}，name 集合与注册一致）
    - `TestToolRegistry#testNamesSorted`（按序注册 "b","a","c" → names() 返回 ["a","b","c"]）
    - `TestBaseTool#testExecuteReturnsString`（fake 工具 execute(expression="1+1") 返回 "2" → 返回值为 str 类型）
  - GREEN:
    - `uv run pytest tests/tools/test_registry.py -q`（全部转绿）
  - ASSERT:
    - 重复注册后 names() 长度仍为 1（未覆盖）
    - ToolNotFoundError 消息精确包含被查名称，不返回 None
    - to_openai_tools 输出可直接 json.dumps（纯 JSON 类型）
  - DoD:
    - `tests/tools/test_registry.py` 全部转绿 + BaseTool 为 ABC 且 execute 带 @abstractmethod + 全部公共方法有类型注解与中文 docstring
  - 最小验证: `uv run pytest tests/tools/test_registry.py -q`

- [x] Task 4: 实现 SessionStore（JSONL 会话存储）
  - complexity: 🟡
  - files: Create `src/harness/session/__init__.py`、`src/harness/session/store.py`、`tests/session/__init__.py`、`tests/session/test_session_store.py`
  - RED:
    - `TestSessionStore#testAppendAndLoad`（对 tmp 目录 store 写 3 条 message → load_records() 返回 4 条（1 条 session_meta + 3 条 message），ordinal 从 0 连续递增，每行可 json.loads）
    - `TestSessionStore#testIsolation`（s1 写消息 A、s2 写消息 B → s1 的 load_records() 返回记录只含 A、s2 只含 B，两文件互不包含对方内容）
    - `TestSessionStore#testPersistAcrossInstances`（写 2 条后用同目录新建 SessionStore 实例（模拟重启）→ load_records 仍返回全部记录且顺序不变）
    - `TestSessionStore#testAppendCompactionAndReadContext`（写 5 条 message 后 append_compaction(compressed_up_to=ordinal_3, summary="S") → read_context_messages() 返回 [name=="__compaction_summary__" 的 user 消息] + ordinal>3 的消息，不含 ordinal≤3 的原始消息）
    - `TestSessionStore#testCorruptLineSkipped`（手工向文件追加一行非法 JSON → load_records() 跳过该行返回其余记录，不抛异常）
    - `TestSessionStore#testLastModifiedAndIds`（写 s1、s2 → session_ids() 返回 ["s1","s2"]，last_modified("s1") 返回 float 且 > 0）
    - `TestSessionStore#testMessageRounds`（写入 3 组 user/assistant 对（6 条 message）→ message_rounds() 返回 3（会话累计对话轮 = user/assistant 对数））
  - GREEN:
    - `uv run pytest tests/session/test_session_store.py -q`（全部转绿）
  - ASSERT:
    - ordinal 严格连续（无空洞、无重复）
    - 压缩区间读取时摘要消息 role=="user" 且 name=="__compaction_summary__"
    - 损坏行不影响其余行计数（3 好 1 坏 → 返回 3 条 message + meta）
  - DoD:
    - `tests/session/test_session_store.py` 全部转绿 + 所有写方法为 append-only（不重写整文件，用 "a" 模式验证）
  - 最小验证: `uv run pytest tests/session/test_session_store.py -q`

- [x] Task 5: 实现 LLMClient 与 AIMessage（invoke 非流式）
  - complexity: 🟡
  - files: Create `src/harness/llm.py`、`tests/test_llm.py`
  - RED:
    - `TestLLMClient#testInvokeReturnsAIMessage`（monkeypatch openai.OpenAI 返回 mock client，mock chat.completions.create 返回预置 response（content="你好"、usage prompt=312/completion=47、finish_reason="stop"）→ invoke() 返回 AIMessage 且 content/usage/finish_reason 逐字段一致）
    - `TestLLMClient#testReasoningContentPassthrough`（mock response 的 message 挂额外属性 reasoning_content="思考…" → AIMessage.reasoning_content=="思考…"（经 getattr 透传））
    - `TestLLMClient#testToolCallsParsed`（mock 返回 tool_calls=[{"id":"call_1","type":"function","function":{"name":"calc","arguments":'{"expression":"1+1"}'}}] → AIMessage.tool_calls 长度 1 且 args=={"expression":"1+1"}、name=="calc"）
    - `TestLLMClient#testInvalidArgumentsKeptRaw`（mock 返回 arguments='not-json' → 不抛异常，args 为 None 且 arguments_raw=="not-json"）
    - `TestLLMClient#testThinkingDisabledExtraBody`（config.thinking_enabled=False → mock create 收到的调用参数含 extra_body=={"thinking":{"type":"disabled"}}）
    - `TestLLMClient#testTimeoutAndRetriesPassed`（monkeypatch openai.OpenAI 捕获构造参数（config timeout=60.0、max_retries=2）→ 断言 OpenAI(timeout=60.0, max_retries=2) 精确传入）
    - `TestLLMClient#testApiKeyMissingRaises`（monkeypatch delenv DEEPSEEK_API_KEY → 构造 LLMClient 抛出异常且消息含 "DEEPSEEK_API_KEY" 指引）
    - `TestLLMClient#testInvokeApiErrorRaisesLLMError`（monkeypatch openai.OpenAI，mock create 抛 APIConnectionError/Exception("boom") → invoke 抛 LLMError 且消息含 "boom"，不裸抛原始异常）
  - GREEN:
    - `uv run pytest tests/test_llm.py -q`（全部转绿）
  - ASSERT:
    - create 收到的 messages/tools 与入参一致（verify 精确入参）
    - thinking 默认开时不传 extra_body（0 次该参数）
    - 非法 arguments 不抛异常（留给 parser 校验）
  - DoD:
    - `tests/test_llm.py` 全部转绿 + 测试全程无真实网络（openai.OpenAI 全 mock）+ reasoning_content 访问统一封装在 llm.py 单点
  - 最小验证: `uv run pytest tests/test_llm.py -q`

- [x] Task 8: 实现 TraceCollector 与 JsonlExporter
  - complexity: 🟡
  - files: Create `src/harness/trace.py`、`tests/test_trace.py`
  - RED:
    - `TestTrace#testLlmSpanLifecycle`（注入 mock exporter（内存 fake 实现）→ start_trace("s1") + start_llm_span + end_llm_span(usage=312/47) → exporter.export 恰好收到 1 条事件，返回的事件含 trace_id/span_id/parent_span_id==None、attributes["gen_ai.operation.name"]=="chat"、"gen_ai.usage.input_tokens"==312、duration_ms≥0）
    - `TestTrace#testToolSpanParent`（mock ToolCall(id="call_1", name="search") 传给 start_tool_span（父为 llm span）+ end_tool_span → 返回的事件 parent_span_id==llm span_id、attributes["gen_ai.tool.name"]=="search"、"gen_ai.tool.call.id"=="call_1"、"gen_ai.conversation.id"=="s1"）
    - `TestTrace#testErrorSpan`（mock end_tool_span 传入 error={"error.type":"ToolExecutionError","message":"超时"} → 事件 status=="error" 且 error.message 含 "超时"）
    - `TestTrace#testExporterFailureSwallowed`（fake exporter.export 抛 IOError → collector 各方法不抛异常（pytest.raises(None) 即无异常），logging.warning 被记录）
    - `TestTrace#testJsonlFileWritten`（JsonlExporter 写 tmp 目录，export 2 条事件 → 返回检查：文件恰好 2 行且逐行 json.loads 成功，行内含 schema_version==1）
    - `TestTrace#testUniqueTraceIds`（mock collector 连续两次 start_trace → 返回的 trace_id 互不相同且均为 32 位小写 hex）
    - `TestTrace#testSpanIdSixteenHex`（mock start_llm_span 两次调用 → 返回的 span_id 均匹配 16 位小写 hex 且互不相同）
    - `TestTrace#testReasoningTokensRecorded`（mock end_llm_span 传入 usage 返回 reasoning_tokens=128 → 事件 attributes["gen_ai.usage.reasoning_tokens"]==128）
    - `TestTrace#testContentTruncatedTo2000`（mock end_llm_span 传入 output 正文 5000 字符 → 事件 gen_ai.output.messages 内容长度 ≤2000+尾注，尾注含「已截断」）
    - `TestTrace#testSpanKindAttribute`（mock start_llm_span 分别传 kind="compaction" 与 kind="idle_summary" → 两条事件 attributes["harness.span.kind"] 分别为对应值；默认不传 kind → "chat"）
  - GREEN:
    - `uv run pytest tests/test_trace.py -q`（全部转绿）
  - ASSERT:
    - 事件 attributes 的 OTel 命名精确匹配（gen_ai.provider.name=="deepseek"、gen_ai.request.model==config.model）
    - exporter 失败路径 0 次异常上抛（主流程不受影响）
  - DoD:
    - `tests/test_trace.py` 全部转绿 + span_id/trace_id 格式稳定（token_hex）
  - 最小验证: `uv run pytest tests/test_trace.py -q`

## Wave 3

- [x] Task 3: 实现四个内置工具（calculator/search/weather/todo）
  - complexity: 🟡
  - files: Create `src/harness/tools/calculator.py`、`search.py`、`weather.py`、`todo.py`、`tests/tools/test_builtin_tools.py`
  - RED:
    - `TestCalculator#testArithmeticPrecedence`（execute(expression="2+3*4") 返回 "14"）
    - `TestCalculator#testPower`（execute(expression="2**10") 返回 "1024"）
    - `TestCalculator#testDivisionByZero`（execute(expression="1/0") → 抛 ToolExecutionError 且消息含"除"字样）
    - `TestCalculator#testDangerousExpressionRejected`（execute(expression="__import__('os').system('dir')") → 抛 ToolExecutionError，不产生任何系统调用（基于 ast 白名单解析，mock os.system 0 次调用））
    - `TestSearch#testKeywordHit`（execute(query="公司愿景") 命中预置知识库 → 返回对应预置文本）
    - `TestSearch#testKeywordMiss`（execute(query="量子纠缠"（未预置）→ 返回含"未找到"的字符串，不抛异常）
    - `TestWeather#testPresetCity`（execute(city="北京") → 返回文本含数字温度与天气词）
    - `TestWeather#testUnknownCity`（execute(city="亚特兰蒂斯"（未预置）→ 返回含"暂无"的说明，不抛异常）
    - `TestTodo#testAddAndList`（add(todo="写周报") 返回含编号 1 的提示，list() 返回 1 条「写周报」）
    - `TestTodo#testSessionIsolation`（s1 添加 2 条后 s2.list()（同目录不同 session）→ 返回空列表）
    - `TestTodo#testPersistence`（添加 2 条后新建同目录 TodoTool 实例（模拟重启）→ list() 仍返回 2 条）
  - GREEN:
    - `uv run pytest tests/tools/test_builtin_tools.py -q`（全部转绿）
  - ASSERT:
    - calculator 危险表达式路径 os.system 0 次调用（ast 白名单外的节点直接拒绝）
    - todo 隔离断言 s2 条目数为 0（不是 None）
    - 四个工具的 parameters 均为合法 JSON Schema（jsonschema 结构自检：type=="object"）
  - DoD:
    - `tests/tools/test_builtin_tools.py` 全部转绿 + calculator 实现基于 ast 白名单（禁 eval/exec）+ 每个工具中文 description
  - 最小验证: `uv run pytest tests/tools/test_builtin_tools.py -q`

- [x] Task 6: 实现 parser（parse_response 与参数校验）
  - complexity: 🟡
  - files: Create `src/harness/parser.py`、`tests/test_parser.py`
  - RED:
    - `TestParser#testFinalAnswer`（入参 AIMessage(content="答案"、tool_calls=[]) → 返回 FinalAnswer 且 content=="答案"）
    - `TestParser#testToolCallBatch`（入参 AIMessage(tool_calls=[ToolCall(id,name,args={"a":1})]) → 返回 ToolCallBatch 且 calls[0].args=={"a":1}）
    - `TestParser#testEmptyContentNoToolsIsFinal`（入参 AIMessage(content=""、tool_calls=[]) → 返回 FinalAnswer（content 为空串），不抛异常）
    - `TestParser#testValidateMissingRequired`（schema {"required":["city"]} + args {} → validate_arguments 返回的错误列表含 "city"）
    - `TestParser#testValidateInvalidJSONRaw`（ToolCall(arguments_raw="oops"、args=None) → validate_arguments 返回的错误列表含"JSON"字样（非法 JSON 明确报错），不抛异常）
    - `TestParser#testValidateUnknownArgReported`（schema properties 仅含 "city" + args {"city":"北京","foo":1} → validate_arguments 返回的错误列表含 "foo"（未知字段））
    - `TestParser#testValidatePassReturnsEmpty`（合法 args → validate_arguments 返回 []（零错误））
  - GREEN:
    - `uv run pytest tests/test_parser.py -q`（全部转绿）
  - ASSERT:
    - 无 tool_calls 时恒返回 FinalAnswer（不构造 ToolCallBatch）
    - validate 对非法 JSON 返回错误列表而非抛异常（错误信息可回传 LLM）
  - DoD:
    - `tests/test_parser.py` 全部转绿 + AgentDecision 联合类型两分支均有用例覆盖
  - 最小验证: `uv run pytest tests/test_parser.py -q`

- [x] Task 12: 实现 MemoryStore 与 MemorySummarizer
  - complexity: 🔴
  - files: Create `src/harness/memory/__init__.py`、`store.py`、`summarizer.py`、`src/harness/prompts.py`（MEMORY_SUMMARY_PROMPT/MEMORY_MERGE_PROMPT）、`tests/memory/__init__.py`、`tests/memory/test_memory_store.py`、`tests/memory/test_summarizer.py`
  - RED:
    - `TestMemoryStore#testWriteCreatesEntryAndIndex`（write("s1","记忆A") 返回 True → entries 目录恰 1 个 md 文件，frontmatter 含 hash/created/tags，MEMORY.md 含该条目索引行）
    - `TestMemoryStore#testDedupSameContent`（write("s1","记忆A") 两次（同内容）→ 第二次返回 False 且 entries 文件数仍为 1）
    - `TestMemoryStore#testDifferentContentWrites`（write 两条不同内容 → 两次均返回 True，文件数为 2）
    - `TestMemoryStore#testRenderSummary`（先 write 两条 → render_summary("s1") 返回含两段记忆内容的字符串；对无记忆的 "s9" 返回 None）
    - `TestMemoryStore#testMergeAppliesActions`（mock llm.invoke 返回 '[{"action":"DELETE","hash":"<已有>"},{"action":"ADD","content":"新记忆"}]' → merge 后条目数 -1+1，MEMORY.md 被重写（不含被删条目））
    - `TestMemoryStore#testMergeInvalidLLMOutputKeepsOriginal`（mock llm.invoke 返回 '不是JSON' → merge 不抛异常、条目原样保留、返回失败标记）
    - `TestMemorySummarizer#testScanOnceSummarizesIdle`（config.idle_seconds=0，mock sessions（s1 的 last_modified 返回很旧时间戳、read_context 返回消息列表）+ mock llm → scan_once() 返回 ["s1"] 且 memory.write 被调用 1 次（verify），llm 收到的 prompt 含 MEMORY_SUMMARY_PROMPT 与会话内容）
    - `TestMemorySummarizer#testScanSkipsActive`（mock sessions：s2 的 last_modified 返回当前时间（活跃）→ scan_once() 返回列表不含 "s2"，memory.write 对 s2 0 次调用）
    - `TestMemorySummarizer#testScanSkipsAlreadySummarized`（mock memory：s1 目录已存在 MEMORY.md → scan_once() 跳过 s1（返回列表不含 s1））
    - `TestMemorySummarizer#testScanFailureRetriesNextRound`（mock llm.invoke 抛异常 → scan_once() 返回 [] 不抛异常；改 mock 正常后再次 scan_once() 能总结成功）
    - `TestMemorySummarizer#testStartStopThread`（mock config 返回 scan_interval_seconds=0.1 → start() 返回后线程 is_alive() 为 True，stop() 后 2 秒内 join 退出）
    - `TestMemorySummarizer#testScanEmitsIndependentTrace`（mock collector → scan_once() 总结时 start_trace 被调用恰 1 次（返回独立 trace_id）且 start_llm_span 入参 kind=="idle_summary"（后台总结 LLM 调用挂独立 trace））
  - GREEN:
    - `uv run pytest tests/memory/test_memory_store.py tests/memory/test_summarizer.py -q`（全部转绿）
  - ASSERT:
    - 去重路径第二次 write 的文件创建为 0 次（entries 计数不变）
    - scan_once 仅处理「闲置且未总结」会话（活跃/已总结的 write 均 0 次）
    - 合并输出非法时 MEMORY.md 内容逐字节不变（不应用半截动作）
  - DoD:
    - 两个测试文件全部转绿 + summarizer 线程为 daemon + 提示词模板位于 prompts.py（不在业务逻辑内）
  - 最小验证: `uv run pytest tests/memory -q`

## Wave 4

- [ ] Task 7: 实现 ContextBuilder、estimate_tokens 与 Middleware 基类
  - complexity: 🟡
  - files: Create `src/harness/context/__init__.py`、`builder.py`、`src/harness/middleware.py`、`tests/context/__init__.py`、`tests/context/test_builder.py`
  - RED:
    - `TestContextBuilder#testBuildBasicOrder`（mock sessions.read_context_messages 返回 [user1, assistant1]、mock memory.render_summary 返回 None → build("s1","今天天气") 输出 [system, user1, assistant1, {"role":"user","content":"今天天气"}]，system 为 SYSTEM_PROMPT）
    - `TestContextBuilder#testMemoryInjected`（mock memory.render_summary 返回 "用户偏好中文" → system 消息含"历史记忆"段且包含该内容）
    - `TestContextBuilder#testSummaryMessageKeptFirst`（mock read_context 返回 [__compaction_summary__ 消息, msg…] → build 输出中摘要消息位于历史消息之前、系统提示词之后）
    - `TestContextBuilder#testSummaryMessageHasExplicitMarker`（mock read_context 返回 summary="任务概览：…" 的摘要消息 → build 输出的该消息 content 首行含明文标记「以下为此前对话的压缩摘要」，且原文摘要紧随其后）
    - `TestContextBuilder#testToolResultTruncated`（mock read_context 返回 tool 消息 content 为 5000 字符 → build 后该消息 content ≤ 2000+尾注长度，前 2000 字符与原文一致，尾注含"全文见会话记录"）
    - `TestContextBuilder#testEmptyHistory`（mock read_context 返回 [] → build 输出恰 2 条 [system, user_input]）
    - `TestEstimateTokens#testDeterministicFormula`（构造固定 messages（总字符 100）→ estimate_tokens 返回值 == 手算期望值（ceil(100/2.5)+5×条数+10×工具块+8×工具结果块），同输入两次调用结果相同）
    - `TestMiddleware#testDefaultNoOp`（构造 LoopState + Middleware() 默认实例 → before_model/after_model/wrap_tool_call（传 fake execute 返回透传）均无异常、state 字段不变）
  - GREEN:
    - `uv run pytest tests/context/test_builder.py -q`（全部转绿）
  - ASSERT:
    - 无记忆时 system 消息不含"历史记忆"字样
    - 截断保留原文前 2000 字符（不补 null、不静默截错位置）
    - estimate_tokens 对同一输入幂等
  - DoD:
    - `tests/context/test_builder.py` 全部转绿 + Middleware 三钩子均有默认空实现（子类可只覆盖其一）
  - 最小验证: `uv run pytest tests/context/test_builder.py -q`

- [ ] Task 11: 实现流式 stream 与事件聚合
  - complexity: 🔴
  - files: Modify `src/harness/llm.py`（追加 stream/collect）、`tests/test_stream.py`
  - RED:
    - `TestLLMClientStream#testReasoningThenTextDeltas`（monkeypatch openai 返回 chunk 序列 [delta.reasoning_content="思"、"考"，delta.content="你"、"好"] → stream 产出 [ReasoningDelta("思"), ReasoningDelta("考"), TextDelta("你"), TextDelta("好")]，顺序保持）
    - `TestLLMClientStream#testToolCallDeltaAggregation`（mock chunk 序列返回：首片 delta.tool_calls=[{index:0,id:"call_1",type:"function",function:{name:"calc",arguments:'{"ex'}}]、后续片 arguments='pression":"1+1"}' → collect 聚合后 tool_calls[0].args=={"expression":"1+1"}）
    - `TestLLMClientStream#testUsageAndDoneEvents`（mock 末 chunk 返回 usage(312/47) + finish_reason="tool_calls" → 流末产出 UsageEvent(312/47) 与 DoneEvent("tool_calls")）
    - `TestLLMClientStream#testStreamOptionsSent`（mock 捕获 create 调用参数 → 断言含 stream=True 与 stream_options=={"include_usage": True}）
    - `TestLLMClientStream#testCollectFullAIMessage`（完整 chunk 序列（思考+正文+工具调用+usage）→ collect 返回 AIMessage：content=="你好"、reasoning_content=="思考"、tool_calls[0].args 解析成功、usage 填充）
    - `TestLLMClientStream#testEmptyStream`（mock 直接返回空迭代（无 chunk）→ collect 返回 content=="" 的 AIMessage，不抛异常）
  - GREEN:
    - `uv run pytest tests/test_stream.py -q`（全部转绿）
  - ASSERT:
    - 事件顺序与 chunk 顺序一致（思考先于正文）
    - tool arguments 分片拼接后 json.loads 成功（拼接无丢失）
    - 空流路径 0 次异常
  - DoD:
    - `tests/test_stream.py` 全部转绿 + stream 与 invoke 共享同一 client 构造（不重复建连）
  - 最小验证: `uv run pytest tests/test_stream.py -q`

## Wave 5

- [ ] Task 9: 实现 ReactLoop 主循环
  - complexity: 🔴
  - files: Create `src/harness/loop.py`、`tests/test_loop.py`；Modify `src/harness/prompts.py`（SYSTEM_PROMPT）
  - RED:
    - `TestReactLoop#testDirectAnswer`（FakeLLM 返回 AIMessage(content="你好") 无 tool_calls → run() 返回 LoopResult.answer=="你好"、rounds==1、tool_call_count==0、truncated==False）
    - `TestReactLoop#testSingleToolRound`（FakeLLM 依序返回 [带 calc("2+3*4") 的 tool_calls、content="14"]（注册真 calculator）→ answer=="14"、rounds==2、tool_call_count==1，session 存储中含 role=="tool" 且 content=="14" 的消息）
    - `TestReactLoop#testSequentialToolCalls`（FakeLLM 依序返回 [weather("北京") 工具调用、calc("温度*2") 工具调用、content="最终答案"]（两工具均真注册）→ answer=="最终答案"、rounds==3、tool_call_count==2，两次工具调用结果均落 session 存储）
    - `TestReactLoop#testMaxRoundsTruncation`（config.max_rounds=2，FakeLLM 每次返回 tool_calls → run() 返回 truncated==True 且 answer 含"最大轮次"，FakeLLM.invoke 调用次数恰为 2（verify，无第 3 次））
    - `TestReactLoop#testToolErrorStructuredReturn`（注册 fake 工具 execute 抛 RuntimeError("boom")，FakeLLM 第 2 轮返回答案并记录收到的消息 → 断言第 2 轮 messages 含 tool 消息，其 content json.loads 后 error.type=="ToolExecutionError" 且 error.message 含 "boom"）
    - `TestReactLoop#testToolErrorNoSideEffects`（注入 mock memory store，注册 fake 工具 execute 抛异常后 LLM 第 2 轮返回答案 → memory.write 0 次调用（verify）；session 新增记录仅 assistant + tool 两条（无额外 reflection 类记录）；LoopResult 正常返回非异常）
    - `TestReactLoop#testToolTimeoutStructuredReturn`（config.tool_timeout_seconds=0.01 + 注册 sleep(1) 的慢工具（mock 时间不等待，真实 sleep 1s 太慢 → 用 time.sleep(0.05) 慢于 0.01 超时即可）→ FakeLLM 第 2 轮收到的 tool 消息 error.type=="ToolTimeoutError"，主循环不中断）
    - `TestReactLoop#testInvalidArgumentsNotExecuted`（FakeLLM 返回 ToolCall(arguments_raw="bad-json"、args=None) → 该工具 execute 0 次调用（verify），LLM 下一轮收到 InvalidToolArguments 错误消息）
    - `TestReactLoop#testMiddlewareHooksInvoked`（mock 记录型 Middleware → 无工具轮 before_model 调用 1 次、after_model 1 次；带工具轮（2 轮）before_model 共 2 次、wrap_tool_call 1 次）
    - `TestReactLoop#testTraceSpansEmitted`（mock TraceCollector → 2 轮循环中 start_llm_span 恰 2 次、start_tool_span 恰 1 次且 parent 为对应 llm span（verify 入参））
    - `TestReactLoop#testTraceIdInjectedIntoLoopState`（mock TraceCollector.start_trace 返回 "tid-1" → 首个 middleware.before_model 收到的 state.trace_id=="tid-1"（压缩 middleware 挂 span 的前提））
    - `TestReactLoop#testReasoningContentPersisted`（FakeLLM 返回含 reasoning_content="思考" 的消息 → session 存储的 assistant 消息含 reasoning_content 字段（下一轮回传前提））
    - `TestReactLoop#testAssistantToolCallsStoredWithPair`（工具轮结束后 → session 存储中 assistant 消息保留 tool_calls 结构，且其后紧跟配对的 role=="tool" 消息（tool_call_id 一致））
  - GREEN:
    - `uv run pytest tests/test_loop.py -q`（全部转绿）
  - ASSERT:
    - FakeLLM.invoke 调用次数 == LoopResult.rounds（精确相等）
    - 非法参数路径真实工具 execute 0 次调用
    - 工具错误路径 memory.write 0 次调用（无反思派生写入）
    - truncated 路径不再发起超额 LLM 调用
  - DoD:
    - `tests/test_loop.py` 全部转绿 + 测试全用 FakeLLM/临时目录（无网络）+ loop.py 不直接 import openai（依赖注入）
  - 最小验证: `uv run pytest tests/test_loop.py -q`

## Wave 6

- [ ] Task 10: 实现 ContextCompressor 与 CompactionMiddleware
  - complexity: 🔴
  - files: Create `src/harness/context/compressor.py`、`tests/context/test_compressor.py`；Modify `src/harness/prompts.py`（COMPACTION_PROMPT）
  - RED:
    - `TestCompressor#testShouldCompactByRounds`（config.compact_rounds=5，mock sessions 未压缩窗口内 5 组 user/assistant 对 → should_compact() 为 True）
    - `TestCompressor#testNoCompactAfterCompaction`（mock sessions 已有 compaction 记录（compressed_up_to 覆盖前 10 轮）+ 窗口内仅 1 轮 → should_compact() 为 False（统计口径为未压缩窗口，压缩后回落））
    - `TestCompressor#testShouldCompactByTokens`（config.compact_tokens=100，mock message_rounds 返回 1、mock read_context 返回总字符 500 的消息（估算超 100）→ should_compact() 为 True）
    - `TestCompressor#testNoCompactWhenBelow`（rounds=4 且 token 低（两阈值均未达）→ should_compact() 为 False）
    - `TestCompressor#testCompactKeepsRecentRounds`（mock sessions 返回 30 条消息记录（ordinal 0-29，15 组 user/assistant 对），config.keep_recent_rounds=5 → compact() 后 append_compaction 恰调用 1 次（verify），保留最后 5 轮（ordinal 20-29 共 10 条）原文，compressed_up_to==19）
    - `TestCompressor#testKeepRecentRoundsCountsPairsNotMessages`（mock sessions 返回含工具调用的历史：前 10 轮为带工具轮（每轮 4 条）+ 末 5 轮纯对话（每轮 2 条），keep_recent_rounds=5 → 保留区间恰为末 5 轮的全部消息（含其中的工具配对消息），compressed_up_to 落在第 11 轮之前）
    - `TestCompressor#testCutPointNotSplitToolPair`（mock sessions 返回的消息序列使保留边界恰切在 assistant(tool_calls) 与其 tool 结果消息之间 → compressed_up_to 返回值回退到该轮起点（配对同侧完整））
    - `TestCompressor#testSummaryGeneratedViaPrompt`（mock llm.invoke 返回 content="任务概览：…" → compact() 调用 llm 时 messages 含 COMPACTION_PROMPT 特征文本与历史渲染，写入的 summary=="任务概览：…"）
    - `TestCompressor#testCompactChainIncludesOldSummary`（mock sessions.load_records 返回含旧 compaction 记录（summary="旧摘要"）→ 新压缩传给 llm 的输入含 "旧摘要" 文本（链式压缩））
    - `TestCompactionMiddleware#testFailureDegrades`（mock compressor.compact 抛异常 → CompactionMiddleware.before_model 不抛异常、logging.warning 记录、state.messages 原样（未被破坏））
    - `TestCompactionMiddleware#testCompactLlmCallTraced`（mock collector + mock llm.invoke 返回摘要 content → compact() 过程中 start_llm_span 被调用恰 1 次（verify）且入参 kind=="compaction"、trace_id==state.trace_id（压缩 LLM 调用挂 span））
  - GREEN:
    - `uv run pytest tests/context/test_compressor.py -q`（全部转绿）
  - ASSERT:
    - compact 触发路径 append_compaction 恰 1 次；未触发路径 0 次
    - 失败降级路径 session 文件 0 次写入
    - 切点回退后保留区间内 tool_call/tool 配对完整（逐条校验）
  - DoD:
    - `tests/context/test_compressor.py` 全部转绿 + 压缩输入渲染中 tool_result 超 500 字符截断（提示词模板内置）
  - 最小验证: `uv run pytest tests/context/test_compressor.py -q`

## Wave 7

- [ ] Task 13: 实现 CLI REPL 与 README
  - complexity: 🔴
  - files: Create `src/harness/__main__.py`、`tests/test_cli.py`、`README.md`
  - RED:
    - `TestCli#testCommandExit`（注入输入序列 ["/exit"] → run_repl 正常返回，mock loop.run 0 次调用）
    - `TestCli#testDefaultSessionIdAnnounced`（不指定 --session 启动（mock sessions 返回空历史）→ 启动输出含自动生成的会话 id 提示）
    - `TestCli#testResumeSessionAnnounced`（以 --session s1 启动，mock sessions 返回 6 条历史记录 → 启动输出含「已续接会话 s1」与消息数提示）
    - `TestCli#testCommandNew`（输入 ["/new", "/exit"] → 输出含新会话 id 提示，后续输入写入新会话（mock loop.run 的 session 参数变化））
    - `TestCli#testCommandSessions`（mock sessions.session_ids 返回 ["s1","s2"] → /sessions 输出同时含 "s1" 与 "s2"，loop.run 0 次调用）
    - `TestCli#testCommandHistory`（mock sessions：当前会话 3 轮 → /history 输出含轮次信息 "3"）
    - `TestCli#testUserInputRoutesToLoop`（输入 ["你好", "/exit"] → mock loop.run 恰调用 1 次且入参 user_input=="你好"）
    - `TestRender#testStreamEventRendering`（mock 终端捕获（capsys）→ render_event(ReasoningDelta("思")) 返回输出含"思考"前缀；render_event(TextDelta("答")) 返回输出不含"思考"前缀（两通道可区分））
    - `TestCli#testNoStreamFlag`（config.stream_enabled=False → mock loop.run 收到 on_event==None）
    - `TestCli#testApiKeyMissingStartup`（monkeypatch delenv DEEPSEEK_API_KEY → main() 输出含 "DEEPSEEK_API_KEY" 的指引并以非 0 状态结束，loop.run 0 次调用）
    - `TestCli#testLlmErrorRecoverable`（mock loop.run 抛 LLMError("超时") → REPL 输出错误提示后继续处理下一条输入（"/exit" 正常退出），不崩溃）
  - GREEN:
    - `uv run pytest tests/test_cli.py -q`（全部转绿）
  - ASSERT:
    - 全部 REPL 命令路径 loop.run 0 次调用（命令不进 LLM）
    - 流式渲染思考/正文前缀互斥（思考行必含前缀、正文行必不含）
    - LLM 错误后 REPL 存活（继续消费输入）
  - DoD:
    - `tests/test_cli.py` 全部转绿 + `PYTHONPATH=src uv run python -m harness --help` exit 0（src 布局需显式 PYTHONPATH，PowerShell 先 `$env:PYTHONPATH="src"`）+ README 含四节：运行方式 / 系统设计（模块图）/ memory 召回时机与放置方式 / AI Prompt 与问题解决记录
  - 最小验证: `uv run pytest tests/test_cli.py -q && PYTHONPATH=src uv run python -m harness --help`

## 收尾（随 Task 13 提交）

- README 手工验收清单执行（真实 API，不自动化）：双窗口隔离 / 续接追问 / 带工具追问 / 流式分通道 / trace 文件抽查——步骤与结果记录进 README「AI Prompt 与问题解决记录」节
- `uv run pytest`（全量）全绿作为 change 级验收
