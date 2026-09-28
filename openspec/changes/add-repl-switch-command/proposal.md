# Proposal — add-repl-switch-command

## Why

REPL 当前只能在**启动时**通过 `--session <id>` 续接已有会话；进入交互循环后仅有 `/new`（新建会话），无法在已有会话之间自由切换。用户想回到早前会话只能退出进程后带 `--session` 重启，交互体验割裂——这是 CLI 交互层的真实缺陷（exploration 结论：`run_repl` 的命令分发仅有 `/exit` `/new` `/sessions` `/history` 四个精确匹配分支，切换所需的 `SessionStore.session_ids()` 与 `on_session_change` 换绑回调机制均已存在，可直接复用）。

## What Changes

- 修改 `src/harness/__main__.py`：
  - 新增 REPL 命令 `/switch <session_id>`：分词解析（`text.split()`，命令名 + 恰好一个参数）；
  - 切换成功（目标在 `sessions.session_ids()` 中）：更新当前会话 id、调用 `on_session_change` 回调（换绑 todo 工具）、输出「已切换会话 <id>（N 条消息）」（N 为目标会话 message 记录数，N==0 时输出不带计数的简式）；
  - 目标不存在：输出「会话 <id> 不存在，可用 /sessions 查看全部会话」，当前会话保持不变，REPL 继续可用；
  - 参数个数不为 1（缺参/多参）：输出用法提示，不进入 LLM；
  - 切换到当前会话视为幂等（走正常切换流程）；
  - 模块 docstring 与启动提示文案的命令列表同步补充 `/switch 切换会话`。
- 修改 `tests/test_cli.py`：新增 `TestSessionSwitch` 测试类（8 用例，全部 mock loop/sessions，零网络）。

## Impact

- 文件：`src/harness/__main__.py`、`tests/test_cli.py`（README 未列举 REPL 命令清单，AGENTS.md 亦未登记命令细节，均无需同步）
- 依赖：零新增
- 不动：`session/store.py`（复用现有 `session_ids` / `load_records`）、`loop.py`、`renderer.py`
- 与 `build-minimal-agent-runtime`、`refactor-stream-renderer` 两个 change 相互独立，无文件级冲突（后者的 `[x]` 状态不受影响）

## 非目标（Non-goals）

- 不做交互式会话选择器（列表 + 序号两步交互）
- 不做会话重命名 / 删除命令
- 不做会话 id 模糊匹配 / 前缀补全
- 不改变 `--session` 启动参数语义
- 不引入多进程会话锁（沿用现有决策：同 session 不支持多进程并发写，REPL 内切换不改变该前提）
- 不做切换历史栈（/switch 不记忆上一个会话）

## 验收标准（可验证）

1. `uv run pytest tests/test_cli.py -q` 全绿（含新增 8 条用例），全量 `uv run pytest` 全绿且无真实网络调用
2. REPL 内输入 `/switch <已有id>`：后续普通输入路由到目标会话（loop.run 收到的 session_id 为目标 id），输出含「已切换会话 <id>」与目标会话消息数
3. `/switch` 成功后 `on_session_change` 以目标 id 被调用恰 1 次（todo 工具换绑依据）
4. `/switch <不存在的id>`：输出含「不存在」提示、loop.run 收到的 session_id 仍为原会话、REPL 继续消费后续输入不崩溃
5. `/switch`（缺参）与 `/switch a b`（多参）：输出含用法的提示、loop.run 0 次调用
6. 切换命令路径 loop.run 0 次调用（命令不进 LLM）
7. `PYTHONPATH=src uv run python -m harness --help` exit 0（回归不受影响）
