# Proposal — simplify-session-store

## Why

会话 JSONL 每行落盘 `ordinal` 序号，但文件是 append-only、永不改写——行序天然就是稳定标识，落盘冗余。其真实成本在写路径：`_prepare_append` 在**每次追加前全文件重扫求 max ordinal**，ReAct 每轮至少写 2~3 条（loop.py:94/104/275），写放大 O(n)。另外 compaction 记录的 `summary_model` 字段无任何消费方（全仓 grep 仅 store.py 写入点，无读取方）。

## What Changes

- 修改 `src/harness/session/store.py`：
  - 记录格式瘦身：message 行写 `{"kind","message"}`，compaction 行写 `{"kind","compressed_up_to","summary"}`；不再落盘 `ordinal` 与 `summary_model`；
  - 删除 `_prepare_append`（写路径不再预读文件，追加变纯 O(1)）；
  - `_read_valid_records` 读取时按行序**派生** ordinal（损坏行不计入），下游 `read_context_messages` / compressor / `/history` 对 `record["ordinal"]` 的消费零改动；
  - `append_compaction` 去掉 `model` 参数；
  - `read_context_messages` 两趟循环合并为单趟收集 + 截取，删除对 ordinal 的 isinstance 校验（派生值恒为 int）。
- 修改 `src/harness/context/compressor.py`：`append_compaction` 调用去掉第 4 个实参（`config.model` 仅保留用于压缩 LLM span）。
- 修改测试：`tests/session/test_session_store.py`（新增 5 条用例 + 3 处既有调用签名跟进）、`tests/context/test_compressor.py`（1 处断言跟进）。

## Impact

- 文件：`store.py`、`compressor.py`、两个测试文件
- 兼容性：旧数据文件无需迁移——旧文件落盘 ordinal 从 0 连续递增，与读取时派生值一致；`compressed_up_to` 的相对比较语义不变
- 已知取舍：旧文件若存在**损坏行**，其后派生序号相对落盘值整体偏移，`compressed_up_to` 切分可能出现 ±偏移——文件已处异常态，demo 量级接受
- 不动：`loop.py`（append_message 签名不变）、`builder.py`（read_context_messages 返回形状不变）、`__main__.py` / `tests/test_cli.py`（属进行中的 add-repl-switch-command，且其 mock 不走真实 store）

## 非目标（Non-goals）

- 不改压缩语义（keep_recent_rounds 切点、链式摘要、tool 配对回退均不动）
- 不删除 compressor `_retreat_for_tool_pairs` 防御性回退（另行评估）
- 不做 MemoryStore 简化（哈希去重 / frontmatter / 四动作 merge 属另一 change）
- 不做多进程并发写保护（沿用决策 6 前提）
- 不做旧文件批量迁移 / 重写工具

## 验收标准（可验证）

1. `uv run pytest` 全量全绿，无真实网络调用
2. 写入路径零文件读取：连续 append_message / append_compaction 期间 `_read_valid_records` 调用次数为 0
3. 新写文件每行 JSON 不含 `ordinal` / `summary_model` / `ts` 键
4. 读取时记录携带派生 ordinal（从 0 连续，损坏行不计入）；`read_context_messages` 切分行为与改动前一致
5. 旧格式文件（含落盘 ordinal / summary_model）可正常读取且切分正确
