# Spec — cli

## Purpose

定义命令行交互入口：REPL 多轮对话、会话管理命令、流式渲染（思考/正文分离）与运行期异常的用户体验。

## ADDED Requirements

### Requirement: REPL 交互

`python -m harness` SHALL 进入交互式多轮对话（REPL）；`--session &lt;id&gt;` SHALL 以指定会话启动（已存在则续接历史）；未指定时自动生成新会话 id 并提示。

#### Scenario: 默认启动

- **WHEN** 不带参数运行
- **THEN** 进入 REPL 并提示自动生成的会话 id

#### Scenario: 续接指定会话

- **WHEN** 以 --session s1 启动且 s1 存在历史
- **THEN** REPL 加载历史并提示「已续接会话 s1（N 条消息）」

### Requirement: 流式渲染与通道分离

默认 SHALL 流式输出：思考内容（reasoning_content）与正文（content）分通道渲染——思考内容带「思考」前缀与弱化样式逐字流出，正文随后正常流出。`--no-stream` SHALL 改为整段输出；`--no-thinking` SHALL 关闭思考模式（请求不再携带/返回思考内容）。

#### Scenario: 思考与正文分通道

- **WHEN** 思考模式下模型先后产出思考内容与正文
- **THEN** 终端先逐字输出带「思考」前缀的内容，再输出正文，两者视觉可区分

#### Scenario: 关闭流式

- **WHEN** 以 --no-stream 启动
- **THEN** 每轮回复在完成后一次性输出

### Requirement: 会话管理命令

REPL SHALL 支持以下内置命令：/exit（退出）、/new（切换到新会话）、/sessions（列出全部会话）、/history（显示当前会话概览：消息数、轮次、最近压缩状态）。命令 MUST NOT 被当作用户消息发给 LLM。

#### Scenario: /new 切换会话

- **WHEN** 输入 /new
- **THEN** 生成新会话 id 并提示，后续消息写入新会话

#### Scenario: 命令不进 LLM

- **WHEN** 输入 /sessions
- **THEN** 直接输出会话列表，LLM 调用次数为 0

### Requirement: 运行期异常处理

LLM 调用失败（网络错误 / 超时 / 鉴权失败）SHALL 向用户输出可读的错误提示（含错误类别），REPL 保持可用允许继续输入；MUST NOT 因单次失败崩溃退出。API key 未配置时启动 SHALL 给出明确指引。

#### Scenario: 网络失败可恢复

- **WHEN** 某轮 LLM 调用网络超时
- **THEN** 终端显示超时错误提示，用户可继续下一轮输入

#### Scenario: 缺少 API key

- **WHEN** 环境变量 DEEPSEEK_API_KEY 未设置时启动
- **THEN** 启动即提示需要设置该环境变量，不发起任何 API 调用
