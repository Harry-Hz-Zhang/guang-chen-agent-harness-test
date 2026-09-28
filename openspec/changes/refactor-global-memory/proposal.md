# Proposal — refactor-global-memory

## Why

记忆板块存在三个设计错误（用户反馈 2026-09-28）：

1. **记忆不是跨会话的**：`MemoryStore` 按 `data/memory/<session_id>/` 隔离存储，一个会话总结出的记忆对其他会话不可见，不构成"长期记忆"；
2. **LLM 合并机制是坏设计**：`merge()` 每次只发送部分记忆条目，却期望 LLM 输出 ADD/UPDATE/DELETE 维护全局一致性——LLM 看不到全局状态，这种"增量更新"根本不可行，用户明确指出"这是不可能的吧"；
3. **缺少读取记忆详细内容的工具**：目前把记忆全文（截断 2000 字）直接塞进 system prompt，LLM 没有任何按需读取某条记忆全文的手段。

用户设定的新方式（本 change 严格照做）：**一个全局 MEMORY 目录 + 后台提取后直接追加（不去重、不合并，demo 从简）+ read_memory 工具读取详情 + 记忆文件内容只要日期**。

## What Changes

- **重写 `src/harness/memory/store.py`**：`MemoryStore` 变为全局 MEMORY 目录的管理者——
  - 目录 `<data_dir>/MEMORY/`：`MEMORY.md`（索引，一行一条：文件名 + 大致内容 + tags）+ 若干时间戳命名的记忆 md 文件（内容只有日期 + 记忆条目）+ `state.json`（各会话提取进度）；
  - 新 API：`append(memories, tags)`（写新 md 文件 + 追加索引行，**不做哈希去重、不做 LLM 合并**）、`render_index()`（供上下文注入）、`read(filename)`（供工具读取）、`summarized_ordinal(session_id)` / `mark_summarized(session_id, ordinal)`（提取进度）；
  - 删除：`merge()`、内容哈希去重、按会话隔离目录、LLM 注入（构造签名变为 `MemoryStore(data_dir)`）。
- **重写 `src/harness/memory/summarizer.py`**：后台线程保留闲置扫描机制，但改为**增量提取**——闲置会话中 ordinal 超过已提取进度的消息送去 LLM，LLM 按 JSON 输出 `{memories, tags}`（显式校验，非法则本轮跳过、下轮重试），有效记忆追加到全局 MEMORY 并推进进度；无新消息的会话不再调用 LLM。
- **新增 `src/harness/tools/read_memory.py`**：`read_memory` 工具，按索引中的文件名读取记忆全文；文件不存在返回友好提示（不抛异常），非法文件名（空 / 非 .md / 带路径）抛 `ToolExecutionError` 结构化回传；在 `_build_registry` 注册。
- **修改 `src/harness/context/builder.py`**：记忆段注入改为全局索引（`render_index()`，无 session 参数），注入格式为「文件名 + 简述 + tags」索引行，并附 read_memory 工具使用提示。
- **修改 `src/harness/prompts.py`**：`MEMORY_SUMMARY_PROMPT` 重写为 `MEMORY_EXTRACT_PROMPT`（JSON 输出）；删除 `MEMORY_MERGE_PROMPT`；`SYSTEM_PROMPT` 末段同步为"记忆索引 + read_memory 读取详情"。
- **修改 `src/harness/__main__.py`**：`MemoryStore(config.data_dir)` 去掉 llm 参数；`_build_registry` 注册 `ReadMemoryTool`。
- **测试**：重写 `tests/memory/test_memory_store.py`、`tests/memory/test_summarizer.py`；`tests/tools/test_builtin_tools.py` 新增 `TestReadMemoryTool`；`tests/context/test_builder.py` 适配 `render_index` 与提示行断言。

## Impact

- 文件：`src/harness/memory/store.py`、`src/harness/memory/summarizer.py`、`src/harness/tools/read_memory.py`（新增）、`src/harness/context/builder.py`、`src/harness/prompts.py`、`src/harness/__main__.py`，及上述四个测试文件
- 依赖：零新增（复用 BaseTool / ToolRegistry / 现有线程与 trace 机制）
- 不动：`session/store.py`（复用 `session_ids` / `load_records` / `last_modified`，ordinal 语义不变）、`loop.py`、`llm.py`、`trace.py`（`idle_summary` span kind 沿用）、`config.py`（`idle_seconds` / `scan_interval_seconds` 语义不变）
- 兼容性：旧 `data/memory/<session_id>/` 产物**不迁移**（demo，直接废弃）；`MemoryStore` 构造签名与全部公开方法均为破坏性变更，调用方只有 `__main__.py` / `builder.py` / `summarizer.py` 与对应测试，全部在本 change 内同步更新
- AGENTS.md 目录约定中 `memory/store.py`（存储）与 `memory/summarizer.py`（闲置总结）的条目职责不变，无需登记变更；`tools/` 下新文件属于"其余工具"范畴

## 非目标（Non-goals）

- 不做记忆去重、语义合并、embedding / 向量检索（用户明确：demo 只要有"提取 + 追加"即可）
- 不做记忆删除 / 编辑 / 过期清理（追加式，只增不改）
- 不做索引分页或召回筛选（索引全文注入 system prompt，见 design 的取舍说明）
- 不迁移旧 `data/memory/` 数据，不提供迁移脚本
- 不改变闲置判定（mtime）与后台线程模型（daemon 线程 + 可配置周期）
- 不做跨进程写锁（沿用现有"同目录无锁"决策）

## 验收标准（可验证）

1. `uv run pytest` 全量全绿，无真实网络调用（全部 mock LLM）
2. 闲置会话被后台提取后：`data/MEMORY/` 出现 `MEMORY.md`、时间戳命名的 md 文件（内容含「日期：」行与记忆条目）与 `state.json`；索引行含文件名、简述、tags
3. **跨会话共享**：会话 s1 提取的记忆，在会话 s2 的 system prompt 记忆段中出现（全局索引注入）
4. **增量提取**：同一会话再次闲置但无新消息时不再调 LLM；有新消息时仅把进度之后的消息送入提取 prompt
5. `read_memory` 工具：按索引文件名可读到记忆全文；文件不存在返回友好提示不抛异常；空 / 带 `/` 或 `..` / 非 .md 文件名抛 `ToolExecutionError`（结构化回传 LLM）
6. LLM 提取输出非法 JSON 时本轮跳过（进度不推进、下轮重试），不崩溃、不写入半成品
7. 代码中无合并 / 去重残留：`grep -rn "MEMORY_MERGE_PROMPT\|def merge" src/` 无结果
8. 同一秒内多次 `append` 生成的文件名不冲突、互不覆盖
