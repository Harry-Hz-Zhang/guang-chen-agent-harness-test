# Spec — tools

## Purpose

定义工具注册机制（名称 + 描述 + 参数 JSON Schema，LLM 基于 Schema 自主决策调用）与四个内置工具（calculator / search / weather / todo）的行为契约。

## ADDED Requirements

### Requirement: 工具注册机制

系统 SHALL 提供工具注册表：每个工具注册时必须提供唯一名称、中文描述、参数 JSON Schema 与执行函数。注册表 SHALL 支持按名称查找工具、列出全部工具、并把工具导出为 LLM API 所需的 tools 参数格式。

#### Scenario: 注册后可按名查找

- **WHEN** 注册名为 calculator 的工具后按该名称查找
- **THEN** 返回该工具实例，其名称/描述/Schema 与注册时一致

#### Scenario: 查找未注册的工具

- **WHEN** 按未注册的名称查找工具
- **THEN** 返回明确的「工具不存在」错误信息（含该名称），不返回 None 之类的静默结果

#### Scenario: 导出为 API tools 格式

- **WHEN** 导出注册表中的全部工具
- **THEN** 得到形如 [{"type": "function", "function": {"name", "description", "parameters"}}] 的列表，参数为合法 JSON Schema

#### Scenario: 重复注册同名工具

- **WHEN** 以相同名称注册第二个工具
- **THEN** 返回明确的重复注册错误，原工具不被覆盖

### Requirement: calculator 工具

calculator SHALL 对四则运算与幂运算表达式安全求值（基于表达式解析，MUST NOT 使用任意代码执行），输入为表达式字符串，输出为计算结果。

#### Scenario: 常规运算

- **WHEN** 输入 "2+3*4"
- **THEN** 返回 14

#### Scenario: 非法表达式

- **WHEN** 输入 "2+__import__('os')" 或 "1/0" 等非法/危险表达式
- **THEN** 返回结构化错误，不执行任何系统调用

### Requirement: search 工具（mock）

search SHALL 基于预置知识库按关键词检索并返回模拟结果（本 change 不接真实搜索 API）。

#### Scenario: 关键词命中

- **WHEN** 查询命中预置知识库关键词
- **THEN** 返回对应的模拟检索结果文本

#### Scenario: 关键词未命中

- **WHEN** 查询未命中任何预置关键词
- **THEN** 返回明确的「未找到相关内容」结果（非报错）

### Requirement: weather 工具（mock）

weather SHALL 返回预置城市的模拟天气数据（温度/天气状况）；未预置的城市返回明确说明。

#### Scenario: 查询预置城市

- **WHEN** 查询「北京」
- **THEN** 返回包含温度与天气状况的模拟数据

#### Scenario: 查询未预置城市

- **WHEN** 查询一个未预置的城市
- **THEN** 返回「暂无该城市数据」类说明，不抛异常

### Requirement: todo 工具

todo SHALL 支持添加与列出待办事项，待办数据按会话隔离并持久化到本地文件。

#### Scenario: 添加待办

- **WHEN** 添加待办「写周报」
- **THEN** 返回添加成功提示（含编号）

#### Scenario: 列出待办

- **WHEN** 同一会话先添加两条待办再列出
- **THEN** 返回两条待办；其他会话列出时看不到这两条

#### Scenario: 待办持久化

- **WHEN** 添加待办后进程重启
- **THEN** 待办仍然存在
