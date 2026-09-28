# Design — simplify-session-store

## 模块划分

不改模块边界，仅改 `session/store.py` 的写读两条路径与 `context/compressor.py` 的一处调用点：

- 写路径（store）：`append_message` / `append_compaction` 直写精简记录，删除 `_prepare_append` 预读；
- 读路径（store）：`_read_valid_records` 解析每行时注入派生 ordinal（`parsed["ordinal"] = len(records)`），损坏行跳过、不计入序号；`read_context_messages` 改单趟收集 message 记录与最后一条压缩记录，再按 `compressed_up_to` 截取；
- 消费方（compressor 窗口/切点、builder、`/history`）：继续读 `record["ordinal"]`，对派生值零感知，不改。

## 关键数据结构

新格式（每行一个 JSON 对象）：

```json
{"kind": "message", "message": {"role": "user", "content": "..."}}
{"kind": "compaction", "compressed_up_to": 4, "summary": "..."}
```

读取时内存记录形如 `{"ordinal": <行序>, "kind": ..., ...}`：ordinal 仅为读取视图的一部分，不落盘；`compressed_up_to` 沿用既有语义（ordinal ≤ cut 的 message 被摘要替代）。

## 备选方案（被否决）

- **方案 B（否决）：彻底移除 ordinal 概念，压缩记录改存「被压消息条数」并按 index 切分。** 需同步改 compressor 窗口/切点/配对回退与全部 mock 测试的记录形状，改动面大；且丢失「内存记录带稳定 id」的调试便利。保留内存序号、只去掉落盘，是改动面与收益的最优平衡。
- **方案 C（否决）：物理反向读文件，读到第一条 compaction 即止。** 语义等价于现状「正向全量读 + 取最后一条 compaction」，但 Python 反向读需按块 seek 自管缓冲，复杂度反升；demo 量级正向一遍读是更简单的正确实现。

## 风险与取舍

- 旧文件含损坏行时，其后派生序号相对旧落盘值整体偏移，`compressed_up_to` 切分可能 ±1 漂移——文件已处异常态，demo 接受。
- 同 session 多进程并发写仍不支持（纯追加写下行间交错风险），决策 6 前提不变。
