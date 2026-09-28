# Spec Delta — memory（refactor-global-memory）

> 基线：`openspec/changes/build-minimal-agent-runtime/specs/memory/spec.md`
> 本 delta 为记忆板块重构后的权威规格：跨会话全局 MEMORY 目录、追加式写入（无去重 / 无合并）、增量提取、read_memory 工具。

## MODIFIED Requirements

### Requirement: 记忆存储形态

长期记忆 SHALL 全局共享存储于 `<data_dir>/MEMORY/` 目录（跨全部会话），包含三类文件：`MEMORY.md` 索引（一行一条：记忆文件名 + 大致内容简述 + tags，只追加不重写）、若干以时间戳命名的记忆 md 文件（一次提取一个文件，正文仅含「日期：」行与记忆条目，无 frontmatter / 哈希等元数据）、`state.json`（各会话已提取进度）。同一秒内多次写入 MUST NOT 相互覆盖（文件名追加数字后缀）。

#### Scenario: 追加写入产物

- **WHEN** 一次提取产出记忆 `["用户偏好简洁回复", "正在开发 demo"]` 与 tags `["偏好", "项目"]`
- **THEN** `MEMORY/` 下新增一个时间戳命名的 md 文件（正文含「日期：」与两条记忆），`MEMORY.md` 追加一行含文件名、简述（首条记忆截断 60 字，多条含「等 N 条」）与「tags: 偏好, 项目」

#### Scenario: 同秒冲突不覆盖

- **WHEN** 同一秒内发生两次追加且时间戳文件名相同
- **THEN** 第二个文件以数字后缀命名，两个文件与两条索引行均保留

#### Scenario: 跨会话共享

- **WHEN** 会话 s1 的记忆已写入全局 MEMORY
- **THEN** 任意其他会话 s2 组装上下文时读到同一份索引（记忆不按会话隔离）

### Requirement: 闲置会话后台增量提取

后台任务 SHALL 周期性扫描全部会话（周期与闲置阈值沿用现有配置）：对闲置会话，SHALL 仅把「消息 ordinal 超过该会话已提取进度」的消息送入 LLM 提取；LLM SHALL 按约定输出 JSON 对象 `{"memories": [...], "tags": [...]}`（系统对其显式校验）。有效记忆 SHALL 追加写入全局 MEMORY（新 md 文件 + 索引行）并推进该会话进度；空 memories 合法（仅推进进度、不写文件）。提取输出非法（非 JSON / 非对象 / memories 非列表）时本轮 SHALL 跳过该会话且不推进进度（下轮自然重试），不崩溃、不落半成品。已提取过的消息 MUST NOT 重复进入提取 prompt。MUST NOT 对记忆做去重、语义合并或改写。

#### Scenario: 闲置且有新消息

- **WHEN** 会话闲置、存在进度之后的新消息
- **THEN** LLM 以新消息为输入被调用，产物追加到全局 MEMORY，进度推进到最新消息 ordinal

#### Scenario: 无新消息不重复提取

- **WHEN** 会话闲置但全部消息均已提取过
- **THEN** 本轮不调用 LLM、不产生任何写入

#### Scenario: 活跃会话不被提取

- **WHEN** 会话最后修改时间早于闲置阈值
- **THEN** 扫描跳过该会话

#### Scenario: 非法输出跳过重试

- **WHEN** LLM 提取输出不是合法 JSON 对象
- **THEN** 该会话本轮被跳过（告警日志），进度不推进，下轮扫描重试

### Requirement: 记忆召回时机与放置方式

记忆召回时机为「组装上下文时」：全局索引存在时，系统 SHALL 把索引（文件名 + 简述 + tags，一行一条）渲染为系统提示词的「## 历史记忆」附加段注入，段末附 read_memory 工具使用提示；无索引时系统提示词 MUST 保持原文。详情召回由 LLM 通过 read_memory 工具按文件名按需完成。

#### Scenario: 索引注入系统提示词

- **WHEN** 全局 MEMORY 已有记忆，任一会话新开一轮对话
- **THEN** 发送给 LLM 的系统提示词含「## 历史记忆」段：索引行 + 「（如需某条记忆的完整内容，用 read_memory 工具按文件名读取）」

#### Scenario: 无记忆不注入

- **WHEN** 全局 MEMORY 不存在或索引为空
- **THEN** 系统提示词为原文，不含记忆段与工具提示

## REMOVED Requirements

### Requirement: 写入哈希去重

- **理由**：用户明确"不用去重以及 merge 之类的，因为只是一个 demo"；进度型增量提取已保证同一段消息不会重复进入提取。

### Requirement: 低频 LLM 合并

- **理由**：用户判定该机制不可行（每次仅发送部分记忆却期望 LLM 维护全局一致性）；新方案为追加式，永不改写既有记忆。

## ADDED Requirements

### Requirement: read_memory 记忆读取工具

系统 SHALL 提供 `read_memory` 工具：入参为记忆文件名（来自注入的记忆索引），返回对应记忆文件全文；工具 MUST 与全局 MEMORY 共享同一存储实例（目录布局单一真源）。文件名非法（非字符串 / 空 / 非 .md 后缀 / 含路径分隔符或路径穿越形态）SHALL 抛 `ToolExecutionError` 结构化回传；文件不存在 SHALL 返回友好提示字符串（不抛异常）。工具 SHALL 注册进工具注册表供 LLM 自主决策调用。

#### Scenario: 读取存在的记忆

- **WHEN** LLM 调用 `read_memory(file="20260928-143005.md")` 且该文件存在
- **THEN** 返回该文件全文（含「日期：」行与记忆条目）

#### Scenario: 文件不存在

- **WHEN** 调用的文件名不在 MEMORY 目录中
- **THEN** 返回含「未找到记忆文件」与该文件名的提示字符串，不抛异常

#### Scenario: 非法文件名被拒绝

- **WHEN** 入参为 `""`、`"a/b.md"`、`"../state.json"` 或非字符串
- **THEN** 抛 `ToolExecutionError`，不读取任何文件
