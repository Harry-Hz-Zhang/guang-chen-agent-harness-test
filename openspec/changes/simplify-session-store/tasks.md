# Tasks — simplify-session-store

> complexity: 🟢 standard | phase: apply

## 任务依赖关系

```
T1（格式瘦身 + 派生序号 + 测试）   单 task 闭环：RED → GREEN → ASSERT → DoD
```

- 单模块小改动（store 写读路径 + 1 处调用点），无跨模块依赖，不拆 Wave
- 前置事实：append-only 文件行序即稳定序号；`summary_model` 无消费方；compressor / `/history` 均经 `record["ordinal"]` 消费，派生注入后零改动

## Task 1

- [x] Task 1: 会话记录去 ordinal 落盘改为读取时派生并删除无消费方字段
  - complexity: 🟢
  - files: Modify `src/harness/session/store.py`、`src/harness/context/compressor.py`、`tests/session/test_session_store.py`、`tests/context/test_compressor.py`
  - RED:
    - `TestLeanFormat#testWrittenLinesAreLeanFormat`（对 tmp store 追加 2 条 message + 1 条 compaction → 逐行 json.loads：message 行键集合恰为 {kind,message}，compaction 行键集合恰为 {kind,compressed_up_to,summary}，全文件无 ordinal/summary_model/ts 键；load_records 派生 ordinal == [0,1,2]）
    - `TestLeanFormat#testAppendPerformsNoFileRead`（monkeypatch store._read_valid_records 为调用即 raise → 连续 3 次 append_message + 1 次 append_compaction 全部不触发 → 恢复后 load_records 返回 4 条、派生 ordinal [0,1,2,3]）
    - `TestLeanFormat#testCompactionCutCoversAllMessages`（4 条 message + compaction(compressed_up_to=3) → read_context_messages 恰为 [1 条 __compaction_summary__ 摘要消息]，无原始消息残留）
    - `TestLeanFormat#testCorruptTailThenAppendStaysConsistent`（3 条 message 后手工追加损坏行，再 append_message 1 条 → read_context_messages 返回 4 条原始消息按序，load_records 派生 ordinal [0,1,2,3]（损坏行不计入））
    - `TestLeanFormat#testLegacyPersistedOrdinalOverriddenByDerived`（手写旧格式文件：2 条带落盘 ordinal 的 message 行 + 1 条带 summary_model 的 compaction 行 → load_records 每条 ordinal 等于行序派生值，read_context_messages 返回 [摘要] + cut 之后消息）（兼容回归用例，改动前后均应通过）
    - 既有用例签名跟进：`TestSessionStore#testAppendCompactionAndReadContext` / `#testMessageRoundsCountsAllHistory` / `#testMultipleCompactionsLastWins`（append_compaction 去掉 model 实参）；`TestCompressor#testCompactKeepsRecentRounds`（断言 append_compaction 以 3 参被调用，不再含 config.model）
  - GREEN:
    - `uv run pytest tests/session/test_session_store.py tests/context/test_compressor.py -q`（全部转绿）
  - ASSERT:
    - 写路径：append_message / append_compaction 执行期间 `_read_valid_records` 0 次调用（store 无任何预读）
    - 读路径：派生 ordinal 从 0 连续且损坏行不计入；compaction 记录同样获得派生 ordinal
    - `read_context_messages` 单趟遍历后按 compressed_up_to 截取，多条压缩记录只认最后一条（回归 testMultipleCompactionsLastWins）
    - compressor 调用 `append_compaction(session_id, cut, summary)` 恰 3 个实参
  - DoD:
    - `uv run pytest` 全量全绿 + 新写文件行为符合验收标准 2/3/4/5 + `codegraph sync` 执行成功
  - 最小验证: `uv run pytest tests/session/test_session_store.py tests/context/test_compressor.py -q`
