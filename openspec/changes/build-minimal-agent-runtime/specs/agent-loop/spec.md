# Spec — agent-loop

## Purpose

定义 Agent 的核心 ReAct 决策循环：接收用户输入后由 LLM 决策是直接回复还是调用工具，工具结果回传后继续循环，直到给出最终答案或达到最大轮次；含工具错误的处理契约。

## ADDED Requirements

### Requirement: ReAct 决策循环

系统 SHALL 对每次用户输入执行「LLM 决策 → （可选）工具执行 → 结果回传 → 再决策」的循环，直到 LLM 产出不含工具调用的最终回复为止。工具调用采用 LLM 提供方的官方 function calling 协议（结构化 tool_calls 字段），不使用自定义文本协议解析。

#### Scenario: 直接回复不经过工具

- **WHEN** 用户输入无需工具的问候语
- **THEN** 系统仅进行一次 LLM 调用即返回最终文本回复
- **AND** 全程工具执行次数为 0

#### Scenario: 单工具调用链

- **WHEN** 用户输入「计算 2+3*4」
- **THEN** LLM 决策调用 calculator 工具，工具结果以 role=tool 消息回传后，LLM 返回最终答案「14」

#### Scenario: 连续多轮工具调用

- **WHEN** 任务需要两次先后工具调用（如先查天气再计算）
- **THEN** 循环执行两轮「LLM 决策 → 工具执行 → 回传」，第二轮后返回最终答案

#### Scenario: 最终答案保留思考过程

- **WHEN** LLM 在思考模式下返回 reasoning_content（思考内容）与 content（正文）
- **THEN** 两者均被持久化到会话存储，且思考内容在下一次 LLM 请求时按协议完整回传

### Requirement: 最大轮次限制

单次用户请求内的 LLM 决策轮次 SHALL 不超过配置上限（默认 15）。达到上限时系统 SHALL 终止本次请求并向用户返回明确的「已达最大轮次」提示，不得无限循环。

#### Scenario: 达到轮次上限即终止

- **WHEN** 最大轮次配置为 2，且 LLM 每轮都请求工具调用
- **THEN** 第 2 轮结束后系统返回最大轮次提示
- **AND** 不发起第 3 次 LLM 调用

### Requirement: 工具错误结构化回传

工具执行失败（异常或超时）时，系统 SHALL 把错误信息封装为结构化 JSON（含错误类型、消息、工具名、入参）并以工具结果消息回传 LLM，由 LLM 决定重试或向用户说明。系统 MUST NOT 静默吞掉任何工具错误。

#### Scenario: 工具抛出异常

- **WHEN** 被调用工具内部抛出异常
- **THEN** LLM 收到形如 {"error": {"type": ..., "message": ..., "tool": ..., "args": ...}} 的工具结果消息
- **AND** 该错误同时被记入 trace（status=error）

#### Scenario: 工具执行超时

- **WHEN** 工具执行超过 30 秒未返回
- **THEN** 视为超时错误，按结构化错误回传 LLM，主循环不中断

### Requirement: 工具错误回传无副作用

工具错误回传 SHALL 是一次性、当场生效的：错误信息仅作为工具结果消息进入本次请求的消息序列，供 LLM 当场决策（重试 / 换工具 / 向用户说明）。系统 MUST NOT 让工具错误产生超出该次请求的后续影响：① SHALL NOT 写入长期记忆或任何「反思」类条目；② SHALL NOT 改变后续请求的上下文组装结构；③ SHALL NOT 中断会话。trace 中的错误事件不在此限（错误仍 SHALL 被记录，见 trace spec）。

#### Scenario: 错误不写长期记忆

- **WHEN** 工具执行抛异常且错误已结构化回传 LLM
- **THEN** 该会话的长期记忆目录不因该错误新增任何条目

#### Scenario: 错误不改变后续请求结构

- **WHEN** 上一轮发生过工具错误，用户发起新的一轮对话
- **THEN** 新一轮请求的消息组装结构与未发生过错误时一致（仅历史消息内容不同）

#### Scenario: 错误不中断会话

- **WHEN** 工具错误回传后 LLM 仍给出最终答案
- **THEN** 本次请求正常结束，REPL 继续接受下一条输入

### Requirement: 工具参数显式校验

LLM 返回的工具参数（arguments）为非法 JSON 或不符合工具参数 Schema 时，系统 SHALL 拒绝执行该工具，并把校验错误结构化回传 LLM 重试，MUST NOT 把未校验的参数直接传给工具。

#### Scenario: arguments 非法 JSON

- **WHEN** LLM 返回的 arguments 字符串不是合法 JSON
- **THEN** 该工具不被执行，LLM 收到参数校验错误的结构化消息

#### Scenario: 参数不符合 Schema

- **WHEN** arguments 缺少 Schema 要求的必填字段
- **THEN** 该工具不被执行，错误消息中指明缺失字段
