# Spec — session

## Purpose

定义会话的隔离与持久化行为：同一用户可开多个窗口（会话）并行对话互不影响，且可随时退出后续接历史继续聊。

## ADDED Requirements

### Requirement: 会话隔离

不同 session id 的消息历史 SHALL 完全隔离，任一会话的读写 MUST NOT 影响其他会话的可见内容。

#### Scenario: 双窗口互不串扰

- **WHEN** 会话 s1 记录了消息 A，会话 s2 记录了消息 B
- **THEN** 读取 s1 历史只含 A 系列，读取 s2 历史只含 B 系列

### Requirement: 会话持久化

会话消息 SHALL 以 append-only 方式实时落盘：每条消息追加写入即持久化，进程崩溃或退出后不丢失已确认的消息。

#### Scenario: 写入即可恢复

- **WHEN** 会话写入 3 条消息后模拟进程重启（重新加载存储）
- **THEN** 3 条消息全部恢复且顺序不变

### Requirement: 存储格式

每个会话 SHALL 对应一个 JSONL 文件（每行一个独立 JSON 对象）：首行为会话元数据（含 session id 与创建时间），后续每行含时间戳、单调递增序号与消息体。assistant 消息行 SHALL 保留思考内容（reasoning_content）字段供下轮请求回传。

#### Scenario: 文件结构可解析

- **WHEN** 读取任一会话文件
- **THEN** 每行均可被独立 json.loads，首行为元数据，序号从 0 连续递增

### Requirement: 会话续接

CLI 以相同 session id 再次启动时 SHALL 自动加载该会话全部历史作为上下文，用户可直接就早期内容追问。

#### Scenario: 重启后追问早期内容

- **WHEN** 会话第一轮聊了「我的猫叫小花」，退出后以同一 session id 重启并问「我的猫叫什么」
- **THEN** 系统能基于历史正确回答

### Requirement: 压缩记录与会话存储共存

上下文压缩产生的摘要 SHALL 作为一类记录写入同一会话文件（含被压缩区间与摘要正文），原始消息行 MUST NOT 被删除或改写。

#### Scenario: 压缩不删原始数据

- **WHEN** 会话触发一次压缩
- **THEN** 会话文件中压缩区间之前的原始消息行仍完整存在
