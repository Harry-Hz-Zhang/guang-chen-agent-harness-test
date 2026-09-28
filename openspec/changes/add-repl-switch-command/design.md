# Design — /switch 会话切换命令

## 1. 模块划分与职责

```text
src/harness/
  __main__.py        [MOD] run_repl 命令分发新增 /switch 分支；docstring 与启动提示文案同步
tests/
  test_cli.py        [ADD] 新增 TestSessionSwitch 测试类（不动既有用例）
```

改动收敛在 CLI 表现层（`run_repl`），不触碰 session 存储、loop、renderer 等核心模块——切换语义 = 换当前 `session_id` 变量 + 通知换绑回调，与 `/new` 的既有机制完全同构。

## 2. 关键设计：命令解析与分发

```python
_SWITCH_COMMAND = "/switch"

# run_repl 循环内、_HISTORY_COMMAND 分支之后、普通输入之前：
if text.split()[0] == _SWITCH_COMMAND:      # 分词匹配，/switchxxx 不误命中
    parts = text.split()
    if len(parts) != 2:                      # 显式校验参数个数
        writer("用法：/switch <会话 id>（可用 /sessions 查看全部会话）")
        continue
    target = parts[1]
    if target not in sessions.session_ids(): # 存在性校验（复用 SessionStore）
        writer(f"会话 {target} 不存在，可用 /sessions 查看全部会话")
        continue                             # 当前会话不变，REPL 继续
    session_id = target
    if on_session_change is not None:
        on_session_change(target)            # RebindableTodoTool 换绑
    message_count = sum(
        1 for r in sessions.load_records(target) if r.get("kind") == "message"
    )
    if message_count > 0:
        writer(f"已切换会话 {target}（{message_count} 条消息）")
    else:
        writer(f"已切换会话 {target}")
    continue
```

要点：

- **分词匹配而非前缀匹配**：`text.split()` 后比较 `parts[0]`，避免 `/switchxxx` 这类输入误入命令分支（与既有精确匹配分支的语义一致性）。
- **存在性校验以 `session_ids()` 为准**：会话文件是唯一真源，`<data_dir>/sessions/<id>.jsonl` 存在即视为已有会话（含空会话文件）。
- **消息计数提示与启动续接提示同构**：`「已切换会话 <id>（N 条消息）」` 与启动时的 `「已续接会话 <id>（N 条消息）」` 风格统一；N==0 输出简式，避免「0 条消息」的噪声。
- **失败路径全部可恢复**：缺参、多参、目标不存在均只输出提示并 `continue`，不抛异常、不切换、不影响后续输入（符合公约「异常不静默、REPL 存活」）。
- **幂等**：切到当前会话走正常流程（校验存在 → 回调 → 提示），不特判。

## 3. 关键数据流（切换后）

```text
/switch s2
  → session_ids() 校验通过
  → on_session_change("s2") → session_ref["session_id"]="s2" → RebindableTodoTool 委托 s2 的 TodoTool
  → 局部 session_id="s2" → 后续 loop.run(text, "s2") 读写 s2 的会话文件 / 记忆 / trace
```

与 `/new` 的唯一差异：`/new` 用 `_generate_session_id()` 造全新 id（文件尚不存在），`/switch` 用用户指定且已存在的 id——下游链路（context builder / compressor / memory）全部以 session_id 为寻址键，无需任何感知。

## 4. 被否决的备选方案与原因

### 备选方案 1：退出 REPL 后用 `--session <id>` 重启实现切换

- **做法**：不加命令，用户想切换就退出重启。
- **否决原因**：
  1. 交互上下文丢失，体验割裂（这正是本次要修的缺陷本身）；
  2. 频繁多会话往返时启动成本高（装配 LLM client / summarizer 线程等全部重来）；
  3. `--session` 语义是「启动时选定」，无法覆盖运行中切换诉求。

### 备选方案 2：`/switch` 无参数时进入两步交互（列出会话 + 输入序号选择）

- **做法**：`/switch` 先打印带序号的会话列表，等用户输入序号再切换。
- **否决原因**：
  1. REPL 主循环是单行消费模型（`for line in lines`），两步交互需要引入「待选状态」跨行状态机，显著增加复杂度；
  2. 与既有命令的一步式语义（`/new`、`/sessions`）不一致；
  3. `/sessions` 已能列 id，`/switch <id>` 一步到位，组合使用即可覆盖需求。

### 备选方案 3：用 `text.startswith("/switch")` 做前缀匹配

- **做法**：直接对原始输入做前缀判断后手动剥参数。
- **否决原因**：
  1. `/switchxxx` 会误命中命令分支；
  2. 手工剥参数要处理多余空白等边界，不如 `split()` 分词后统一校验 `len(parts)` 来得直接、可测。
