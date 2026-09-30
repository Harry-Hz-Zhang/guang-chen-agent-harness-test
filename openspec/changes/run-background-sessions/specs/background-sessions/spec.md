# Spec Delta — background-sessions

## Purpose

定义 REPL 的后台会话并行能力：会话运行中切换、每会话专属 worker 串行消费、输出复用与前台/后台会话区分标记、worker 生命周期与并发上限、退出语义。

## ADDED Requirements

### Requirement: 切换不打断

用户在会话 A 的回合运行中输入 `/switch <B>` SHALL 完成切换且 MUST NOT 停止、阻塞或等待 A 的在跑回合；A 的 worker 线程 SHALL 继续执行至回合自然结束。切换后主线程 SHALL 立即可读下一行输入。

#### Scenario: A 跑着切到 B

- **WHEN** 会话 A 的 loop.run 尚未返回（LLM 仍在生成）
- **AND** 用户输入 /switch B（B 为已存在会话）
- **THEN** 输出「已切换会话 B」提示，A 的 worker 线程仍存活并在跑，主线程立即等待下一行输入

#### Scenario: 切回未完成的 A

- **WHEN** 会话 A 仍在后台跑、用户已切到 B
- **AND** 用户输入 /switch A
- **THEN** 切换成功，A 的 worker 不受影响继续跑；后续普通输入投进 A 的队列排在在跑回合之后

### Requirement: 同会话串行与异会话并行

同一会话的多个待处理输入 SHALL 由该会话唯一的 worker 按提交顺序串行消费（前一条 loop.run 返回后才取下一条）；不同会话的 worker SHALL 互相并行、互不阻塞。主线程 submit SHALL 立即返回，MUST NOT 阻塞等待回合完成。

#### Scenario: 同会话排队

- **WHEN** 会话 A 的 worker 正在跑第一条输入，用户向 A 再提交第二条
- **THEN** 第二条进入 A 的队列，待第一条回合结束后才开始处理

#### Scenario: 异会话并行

- **WHEN** 会话 A 的回合在跑，用户切到 B 并提交消息
- **THEN** B 的 worker 立即开始处理，无须等待 A；两会话的回合存在真实并行区间

#### Scenario: 主线程不阻塞

- **WHEN** 任一会话的回合在跑
- **THEN** 主线程持续可读新输入并分拣（命令/消息），不被任何 worker 阻塞

### Requirement: 输出复用与后台标记

全部终端输出（含流式分片）SHALL 经过同一把锁串行落屏，MUST NOT 出现多会话输出行内交错。当前前台会话的输出与现状一致（无前缀）；非当前前台会话的输出行 SHALL 以 `[<session_id 前 4 位>]` 为行首标记（如 `[a1b2]`）。后台会话回合自然结束时 SHALL 输出一行「`[<前缀>] 已完成`」提示。渲染器实例 MUST 为每会话每请求独享，MUST NOT 跨会话共享。

#### Scenario: 两会话同时流式输出

- **WHEN** A 与 B 的回合并行、均产出流式分片
- **THEN** 终端每一行完整归属于单一会话，行内无两会话字符交错

#### Scenario: 后台完成提示

- **WHEN** 后台会话 A 的回合自然结束
- **THEN** 输出一行「[a1b2] 已完成」，A 的后续输出恢复等待新输入状态

#### Scenario: 前台输出保持现状

- **WHEN** 当前前台会话的回合产出输出
- **THEN** 输出格式与无本 change 时一致（无会话前缀、「思考」前缀照旧）

### Requirement: worker 生命周期与并发上限

worker SHALL 随会话首次被提交普通消息而创建、终身绑定该会话（每会话至多 1 个 worker），SHALL 为 daemon 线程。活跃 worker 总数 SHALL NOT 超过 `max_concurrent_sessions`；达上限后再向新会话提交 SHALL 被拒收并输出护栏提示（消息不入队、主线程不阻塞）。拒收判定 SHALL 在提交时同步完成。

#### Scenario: 首次提交即建 worker

- **WHEN** 向尚无 worker 的会话 B 提交首条普通消息
- **THEN** 立即为 B 创建 worker 并开始处理该消息

#### Scenario: 达上限拒收

- **WHEN** 活跃 worker 数已达 max_concurrent_sessions
- **AND** 用户向新会话提交消息
- **THEN** 输出并发上限提示、消息不入队，主线程立即恢复读输入

#### Scenario: 同会话不重复建 worker

- **WHEN** 会话 A 已有 worker 且正在跑
- **AND** 向 A 再次提交
- **THEN** 复用既有 worker（提交仅入队），worker 总数不变

### Requirement: worker 异常兜底

worker 处理单条输入遭遇任何异常 SHALL 输出结构化可读提示后继续处理队列中后续输入，线程 MUST NOT 因单次失败死亡。LLM 调用失败 SHALL 沿用现有「出错了：……」文案语义；其他异常 SHALL 归类为「该会话处理失败」类提示并含异常类别信息。MUST NOT 静默吞异常。

#### Scenario: 非模型异常不杀线程

- **WHEN** worker 处理某条输入时 loop.run 抛出非 LLMError 的 RuntimeError（如写盘失败）
- **THEN** 输出「该会话处理失败：……」可读提示，worker 存活并继续消费队列中下一条输入

#### Scenario: 模型失败可继续

- **WHEN** 某轮 LLM 调用超时抛 LLMError
- **THEN** 输出「出错了：……」提示，该会话继续可对话（下一条输入正常进入队列被处理）

### Requirement: 硬退语义

`/exit` SHALL 立即退出进程：MUST NOT 等待或 join 任何在跑回合的 worker（daemon 线程随进程终止）；排队未开始的消息自然丢弃。退出后留在磁盘的会话记录 若因硬退出现半行损坏，SHALL 由既有读取器的容错机制跳过（现状已满足，无新增要求）。

#### Scenario: 硬退不等回合

- **WHEN** 会话 A 的回合在跑，用户输入 /exit
- **THEN** 进程立即退出，不出现对 A 的等待/join/中断处理

### Requirement: trace 内存登记表卫生

TraceCollector 的内存登记结构 SHALL 在锁保护下访问，SHALL NOT 随回合与多会话长驻运行无限增长；落盘内容与现状逐字节一致（仅内存管理变化）。

#### Scenario: 回合结束即清理

- **WHEN** 任一回合结束
- **THEN** 该回合在内存登记表中的条目被移除，落盘 trace 文件内容不受影响

#### Scenario: 并发登记互不串扰

- **WHEN** 多会话 worker 并发登记/导出 trace
- **THEN** 各会话 trace 文件内容完整、无跨会话条目串写
