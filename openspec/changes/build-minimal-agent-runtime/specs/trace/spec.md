# Spec — trace

## Purpose

定义 LLM 调用与工具调用的追踪记录行为：span 树结构、字段命名（对齐 OpenTelemetry GenAI 语义约定）、JSONL 落盘、错误记录与故障隔离。

## ADDED Requirements

### Requirement: span 覆盖与树结构

每次用户请求产生一个 trace（trace_id），其内每次 LLM 调用（含上下文压缩的摘要生成调用）与每次工具执行 SHALL 各产生一条 span 事件，事件含 trace_id / span_id / parent_span_id 三件套，工具 span 以其所属 LLM 调用 span 为父。压缩摘要 span 与主循环 LLM span 同属该请求的 trace（平级），并以 `harness.span.kind="compaction"` 属性标识。后台闲置总结的 LLM 调用不属于任何用户请求，SHALL 产生独立 trace，并以 `harness.span.kind="idle_summary"` 属性标识（gen_ai.conversation.id 仍为对应会话 id）。

#### Scenario: 一次请求的事件树

- **WHEN** 一次请求经历 2 轮 LLM 调用与 1 次工具执行
- **THEN** trace 文件新增 3 条事件：2 条 LLM span + 1 条工具 span，工具 span 的 parent_span_id 等于第 1 条 LLM span 的 span_id

#### Scenario: 压缩摘要调用产生 span

- **WHEN** 某次用户请求触发上下文压缩且压缩完成
- **THEN** 该请求的 trace 内新增一条压缩摘要 LLM span（harness.span.kind="compaction"），与主循环 LLM span 平级

#### Scenario: 后台总结产生独立 trace

- **WHEN** 后台闲置总结对某会话完成一次 LLM 总结调用
- **THEN** trace 文件新增一个新 trace_id 下的 LLM span，attributes 含 harness.span.kind="idle_summary" 且 gen_ai.conversation.id 为该会话 id

### Requirement: OTel GenAI 字段命名

span 事件的属性字段 SHALL 对齐 OpenTelemetry GenAI 语义约定命名：LLM span 含 gen_ai.operation.name="chat"、gen_ai.provider.name="deepseek"、gen_ai.request.model、gen_ai.usage.input_tokens、gen_ai.usage.output_tokens、gen_ai.usage.reasoning_tokens（思考模式用量，无则为 0）、gen_ai.conversation.id（= 会话 id）、gen_ai.input.messages 与 gen_ai.output.messages（正文记录，截断规则见下）；工具 span 含 gen_ai.operation.name="execute_tool"、gen_ai.tool.name、gen_ai.tool.call.id、gen_ai.tool.call.arguments、gen_ai.tool.call.result（正文记录，同截断规则）。

正文字段（gen_ai.input.messages / gen_ai.output.messages / gen_ai.tool.call.result）SHALL 记录实际内容但每项截断至 2000 字符，截断时在内容尾部附「已截断，全文见会话记录」说明；全量正文以会话文件为真源，trace 不重复存全量。

#### Scenario: LLM span 字段齐全

- **WHEN** 读取任一 LLM span 事件
- **THEN** attributes 中含上述 gen_ai.* 字段且值为实际调用参数与用量

#### Scenario: 正文截断

- **WHEN** LLM 输出正文为 5000 字符
- **THEN** 事件中 gen_ai.output.messages 记录的内容长度不超过 2000 字符加截断说明尾注

#### Scenario: 思考量记入事件

- **WHEN** LLM 调用返回 usage 且思考用量为 128 token
- **THEN** 事件 attributes 中 gen_ai.usage.reasoning_tokens==128

### Requirement: JSONL 落盘与会话隔离

trace 事件 SHALL 按会话追加写入 data/traces/&lt;session_id&gt;.jsonl，每行一个可独立解析的 JSON 对象。同一会话文件聚合该会话全部 trace 的事件（一次用户请求一个 trace_id，同会话多次请求产生多个平级 trace，由 gen_ai.conversation.id 关联），还原调用树时 MUST 先按 trace_id 分组、再按 parent_span_id 组装。trace 写入失败 SHALL 仅记录告警，MUST NOT 影响主对话流程。

#### Scenario: 落盘可解析

- **WHEN** 对任意 trace 文件逐行执行 json.loads
- **THEN** 全部成功解析，行内含 schema_version 与事件类型

#### Scenario: 写盘失败不崩主流程

- **WHEN** trace 文件路径不可写（如目录被锁）
- **THEN** 对话请求仍正常完成，仅日志出现告警

### Requirement: 错误与用量记录

工具执行失败与 LLM 调用失败 SHALL 在对应 span 事件中记录 status="error" 与结构化 error 对象（错误类型 + 消息）；LLM span SHALL 记录 API 返回的真实 token 用量（流式时从最终 chunk 的 usage 获取）。

#### Scenario: 工具失败的事件

- **WHEN** 工具执行抛出异常
- **THEN** 对应工具 span 事件 status 为 error，error.message 含异常信息

#### Scenario: token 用量入事件

- **WHEN** LLM 调用成功返回 usage（prompt_tokens=312, completion_tokens=47）
- **THEN** 事件 attributes 中 gen_ai.usage.input_tokens=312、gen_ai.usage.output_tokens=47

### Requirement: 事件时间与 ID 规格

每条 span 事件 SHALL 含 start_time / end_time（ISO8601 带时区）与 duration_ms（整数毫秒）；trace_id SHALL 为 32 位小写十六进制串，span_id SHALL 为 16 位小写十六进制串。

#### Scenario: ID 格式

- **WHEN** 生成任一 trace_id 与 span_id
- **THEN** trace_id 匹配 32 位小写 hex 格式，span_id 匹配 16 位小写 hex 格式，同 trace 内 span_id 互不相同

#### Scenario: 时长字段

- **WHEN** 任一 span 事件落盘
- **THEN** 事件含 start_time / end_time / duration_ms，且 duration_ms 等于起止时间差的毫秒数
