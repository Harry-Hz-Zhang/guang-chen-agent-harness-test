# Tasks — refactor-global-memory

> complexity: 🟢 standard | phase: propose

## 任务依赖关系

```text
T1（全局存储层 + 注入切换） → T2（后台增量提取） → T3（read_memory 工具 + 提示词收尾 + 全量回归）
```

- 串行执行：T2 依赖 T1 的 store 新 API（`summarized_ordinal` / `mark_summarized` / `append`）；T3 依赖 T1 的 `read` 与 T2 落定的提示词体系；
- 每个 task 一次原子 commit（`<type>: <中文描述>`），完成即 `codegraph sync`；
- 测试方法名按下方 RED 条目原名（camelCase）执行，保证与需求逐条可追溯。

## Task 1

- [ ] Task 1: 全局 MEMORY 目录存储层与上下文注入切换
  - complexity: 🟢
  - files: Rewrite `src/harness/memory/store.py`、`tests/memory/test_memory_store.py`；Modify `src/harness/context/builder.py`、`src/harness/__main__.py`（仅 `MemoryStore(config.data_dir)` 一行构造）、`tests/context/test_builder.py`
  - 说明: `MemoryStore` 重写为 `<data_dir>/MEMORY/` 唯一管理者（`append` / `render_index` / `read` / `summarized_ordinal` / `mark_summarized`，构造不再接 llm）；`ContextBuilder` 记忆段改为全局索引注入（`render_index()` 无 session 参数；`MEMORY_SECTION_HEADER` 不变；本 task 尚不加 read_memory 提示行——那是 T3 的产物）。注：T1 完成到 T2 完成之间，旧 `summarizer.py` 调用的 `render_summary`/`write` 已不存在，后台线程按会话捕获异常仅告警，不影响测试套件全绿。
  - RED:
    - `tests/memory/test_memory_store.py` 全量重写（`class TestMemoryStore`，tmp_path 落盘断言）:
      - `testAppendWritesEntryFileAndIndexLine`（`append(["用户偏好简洁回复", "正在开发 demo"], ["偏好", "项目"])` → 返回非 None 文件名；`data/MEMORY/<文件名>.md` 存在，内容含「日期：」行与两条记忆；`MEMORY.md` 存在且唯一一行含文件名、「用户偏好简洁回复」（60 字内简述）与「tags: 偏好, 项目」）
      - `testAppendBriefTruncatedAndCountSuffix`（首条记忆 100 字 → 索引行简述 ≤ 60 字；两条记忆 → 含「等 2 条」）
      - `testAppendEmptyTagsOmitTagSection`（tags 为空 → 索引行不含「tags:」）
      - `testAppendEmptyMemoriesNoop`（`append([], [])` → 返回 None，MEMORY 目录无 md 文件、`MEMORY.md` 不存在）
      - `testAppendFilenameCollisionSuffix`（预置同名文件后再 append → 新文件以 `-2` 后缀，两者共存，内容互不覆盖）
      - `testRenderIndexNoneWhenAbsent`（无 MEMORY 目录 → `render_index()` 为 None）
      - `testRenderIndexReturnsIndexLines`（append 两条不同记忆 → `render_index()` 为两行索引行按追加序拼接的文本）
      - `testReadEntryContent`（append 后 `read(文件名)` 返回文件全文，含「日期：」）
      - `testReadMissingReturnsNone`（`read("nonexistent.md")` → None）
      - `testSummarizedOrdinalDefaultsAndPersists`（缺省 `-1`；`mark_summarized("s1", 5)` 后新实例 `summarized_ordinal("s1") == 5`）
      - `testCorruptStateTreatedAsEmpty`（state.json 写入非法 JSON → `summarized_ordinal` 返回 `-1` 且有 warning 日志，不抛异常）
    - `tests/context/test_builder.py` 适配:
      - `testMemoryInjected`（mock `memory.render_index` 返回索引行 → system 含「## 历史记忆」与索引行；`render_index` 以无参被调用恰好 1 次；`render_summary` 不再被调用）
      - `testBuildBasicOrder` / `testEmptyHistory`（mock `render_index` 返回 None → system 为 `SYSTEM_PROMPT` 原文，不含「历史记忆」——既有断言在新 mock 基建下保持）
  - GREEN:
    - `uv run pytest tests/memory/test_memory_store.py tests/context/test_builder.py -q`（全部转绿）
  - ASSERT:
    - `grep -rn "render_summary\|def merge\|_content_hash" src/` 无结果（旧 API 与合并/去重逻辑清零）
    - `uv run pytest`（全量回归全绿：test_cli 的 main 装配用例以新构造签名 `MemoryStore(config.data_dir)` 真实执行不报错）
    - append 两次后 `MEMORY.md` 恰好两行、且第一行先于第二行（追加序）
  - DoD:
    - 上述断言全绿 + `codegraph sync` 执行成功 + 原子 commit `refactor: 记忆存储重写为全局 MEMORY 目录（索引追加 + 状态文件）`
  - 最小验证: `uv run pytest tests/memory/test_memory_store.py tests/context/test_builder.py tests/test_cli.py -q`

## Task 2

- [ ] Task 2: 后台闲置增量提取（JSON 提取 + 追加写入 + 进度推进）
  - complexity: 🟢
  - files: Rewrite `src/harness/memory/summarizer.py`、`tests/memory/test_summarizer.py`；Modify `src/harness/prompts.py`（`MEMORY_SUMMARY_PROMPT` → `MEMORY_EXTRACT_PROMPT` 重写；删除 `MEMORY_MERGE_PROMPT`）
  - 说明: 保留线程模型（`start` / `stop` / `_run` / `_is_idle`）与 trace 行为（`start_trace` + `start_llm_span(kind="idle_summary")` + `end_llm_span` 含 usage）；`scan_once` 改为「闲置 → 取进度之后的消息 → LLM JSON 提取 → 校验 → `append` → `mark_summarized`」；`_parse_extraction` 对 LLM 输出显式校验（非法 → 异常 → 本轮跳过、进度不推进）。
  - RED:
    - `tests/memory/test_summarizer.py` 全量重写（`class TestMemorySummarizer`，sessions/memory/llm/trace 全 mock，llm 返回 `AIMessage`；session 记录用带 ordinal 的 message 记录形态）:
      - `testScanOnceExtractsIdleSessionNewMessages`（s1 闲置、3 条消息 ordinal 0-2、进度缺省 → `scan_once() == ["s1"]`；`llm.invoke` 恰 1 次且 prompt 以 `MEMORY_EXTRACT_PROMPT` 开头、含全部 3 条消息内容；`memory.append` 以（memories 列表, tags 列表）被调用 1 次，内容来自 LLM 的 JSON；`memory.mark_summarized("s1", 2)` 恰 1 次）
      - `testScanOnceOnlySendsMessagesAfterOrdinal`（进度 = 1、消息 ordinal 0-2 → prompt 仅含 ordinal 2 的消息内容，不含 ordinal 0/1 的内容）
      - `testScanOnceSkipsWhenNoNewMessages`（进度 = 2 且无新消息 → `llm.invoke` 0 次、`memory.append` 0 次、返回 `[]`）
      - `testScanOnceSkipsActiveSession`（`last_modified` 为当前时间 → 跳过：0 次 LLM、返回 `[]`）
      - `testScanOnceInvalidJsonSkipsAndKeepsProgress`（llm 返回非 JSON 文本 → `scan_once() == []`、`memory.append` 0 次、`memory.mark_summarized` 0 次、无异常上抛）
      - `testScanOnceNonObjectJsonSkips`（llm 返回 `[1,2]` → 同上：跳过且进度不推进）
      - `testScanOnceEmptyMemoriesAdvancesProgressOnly`（llm 返回 `{"memories": [], "tags": []}` → `append` 0 次、`mark_summarized("s1", 2)` 1 次、返回 `["s1"]`）
      - `testScanOnceMissingTagsToleratedAsEmpty`（llm 返回 `{"memories": ["一条"]}` → `append` 以 tags==`[]` 被调用）
      - `testScanOnceLlmFailureLoggedAndSkipped`（`llm.invoke` 抛异常 → 返回 `[]`、进度不推进、无异常上抛；trace 以 error 结束 span）
      - `testScanOnceMultipleSessionsIndependent`（s1 有新消息、s2 无 → 仅 s1 被提取，返回 `["s1"]`，s2 零 LLM 调用）
      - `testStartStopThreadLifecycle`（start 幂等、stop 安全——既有生命周期用例保留适配）
  - GREEN:
    - `uv run pytest tests/memory/test_summarizer.py -q`（全部转绿）
  - ASSERT:
    - `grep -rn "MEMORY_SUMMARY_PROMPT\|MEMORY_MERGE_PROMPT" src/ tests/` 无结果（旧提示词引用清零）
    - `uv run pytest` 全量回归全绿
    - prompt 断言确认提取输入**不含**已提取过的消息（增量语义，防"每次全量重发"）
  - DoD:
    - 上述断言全绿 + `codegraph sync` 执行成功 + 原子 commit `refactor: 后台记忆改为闲置增量提取并追加写入全局 MEMORY`
  - 最小验证: `uv run pytest tests/memory/test_summarizer.py -q`

## Task 3

- [ ] Task 3: read_memory 工具、注册与提示词收尾
  - complexity: 🟢
  - files: Add `src/harness/tools/read_memory.py`；Modify `src/harness/__main__.py`（`_build_registry` 增加 memory 参数并注册 `ReadMemoryTool`）、`src/harness/context/builder.py`（新增 `MEMORY_TOOL_HINT` 常量并在记忆段末尾追加提示行）、`src/harness/prompts.py`（`SYSTEM_PROMPT` 末段改为索引 + read_memory 表述）、`tests/tools/test_builtin_tools.py`（新增 `TestReadMemoryTool`）、`tests/context/test_builder.py`（`testMemoryInjected` 追加提示行断言）、`tests/test_cli.py`（如有 `_build_registry` 签名相关用例则适配）
  - RED:
    - `tests/tools/test_builtin_tools.py` 新增 `class TestReadMemoryTool`（注入真实 `MemoryStore`（tmp_path），预置 `data/MEMORY/20260928-143005.md`）:
      - `testReadExistingMemoryFile`（`execute(file="20260928-143005.md")` → 返回文件全文，含「日期：」）
      - `testReadMissingMemoryFileFriendlyMessage`（`execute(file="nope.md")` → 返回含「未找到记忆文件」与「nope.md」的字符串，不抛异常）
      - `testInvalidFileParamRaises`（`execute(file="")` → `ToolExecutionError`）
      - `testNonMdExtensionRaises`（`execute(file="MEMORY.md.txt")` → `ToolExecutionError`）
      - `testTraversalPathRejected`（`execute(file="../state.json")` 与 `execute(file="a/b.md")` → `ToolExecutionError`，且 data_dir 外无读取副作用）
      - `testNonStringParamRaises`（`execute(file=None)` → `ToolExecutionError`）
    - `tests/context/test_builder.py`:
      - `testMemoryInjected` 更新（有索引 → system 记忆段末尾含 read_memory 提示行文案「（如需某条记忆的完整内容，用 read_memory 工具按文件名读取）」；无索引（`render_index` → None）→ system 为 `SYSTEM_PROMPT` 原文，不含提示行）
    - `tests/test_cli.py`:
      - `testRegistryIncludesReadMemory`（如存在可直接断言的装配入口；否则经 main 冒烟路径验证——registry 中可按名取到 `read_memory` 工具）
  - GREEN:
    - `uv run pytest tests/tools/test_builtin_tools.py tests/context/test_builder.py tests/test_cli.py -q`（全部转绿）
  - ASSERT:
    - `uv run pytest` 全量全绿、无真实网络调用
    - `SYSTEM_PROMPT` 含「read_memory」（LLM 被明确告知可用该工具读取记忆详情）
    - 装配验证：`_build_registry(config, session_ref, memory)` 注册后 `read_memory` 可用（其余四个内置工具不受影响）
  - DoD:
    - 上述断言全绿 + proposal 验收标准 1-8 逐条核对 + `codegraph sync` 执行成功 + 原子 commit `feat: 新增 read_memory 工具并完成全局记忆接线`
  - 最小验证: `uv run pytest tests/tools/test_builtin_tools.py tests/context/test_builder.py -q && uv run pytest -q`
