# Tasks — add-repl-switch-command

> complexity: 🟢 standard | phase: propose

## 任务依赖关系

```
T1（/switch 命令 + 测试）   单 task 闭环：RED → GREEN → ASSERT → DoD
```

- 单模块小改动（`__main__.py` 命令分发层），无跨模块依赖，不拆 Wave
- 前置事实：`session_ids()` / `load_records` / `on_session_change` 均已存在且被既有测试覆盖，本 task 仅组合复用

## Task 1

- [x] Task 1: 实现 /switch 会话切换命令与 REPL 集成
  - complexity: 🟢
  - files: Modify `src/harness/__main__.py`、`tests/test_cli.py`
  - RED:
    - `TestSessionSwitch#testCommandSwitchToExistingSession`（mock sessions 返回 session_ids==["s1","s2"]、load_records 返回 6 条 message 记录 → 输入 ["/switch s2","你好","/exit"]（初始 session_id="s1"）→ loop.run 第二次调用收到 session_id=="s2"，输出含「已切换会话 s2」与「6」）
    - `TestSessionSwitch#testCommandSwitchUnknownSessionKeepsCurrent`（mock sessions 返回 session_ids==["s1"] → 输入 ["/switch nope","你好","/exit"] → 输出含「不存在」与「nope」，loop.run 收到 session_id=="s1"（未切换），REPL 继续消费「你好」不崩溃）
    - `TestSessionSwitch#testCommandSwitchMissingArgUsage`（输入 ["/switch","你好","/exit"] → 输出含「用法」与「/switch」，当前会话不变，loop.run 收到 session_id=="s1"，命令行本身 0 次进入 LLM）
    - `TestSessionSwitch#testCommandSwitchExtraArgUsage`（输入 ["/switch a b","/exit"] → 输出含「用法」提示，loop.run 0 次调用，当前会话不变）
    - `TestSessionSwitch#testCommandSwitchInvokesCallback`（mock sessions 返回 session_ids==["s1","s2"]，注入 mock on_session_change → 输入 ["/switch s2","你好","/exit"] → 回调恰以 "s2" 调用 1 次，loop.run 收到 session_id=="s2"）
    - `TestSessionSwitch#testCommandSwitchCurrentSessionIdempotent`（mock sessions 返回 session_ids==["s1"] → 输入 ["/switch s1","你好","/exit"] → 输出含「已切换会话 s1」，on_session_change 以 "s1" 调用 1 次，loop.run 收到 session_id=="s1"）
    - `TestSessionSwitch#testCommandSwitchNotRoutedToLoop`（mock sessions 返回 session_ids==["s1","s2"] → 输入 ["/switch s2","/exit"] → loop.run 0 次调用（命令不进 LLM））
    - `TestSessionSwitch#testCommandSwitchEmptySessionNoMessageCount`（mock sessions 返回 session_ids==["s1","s2"]、load_records 返回 [] → 输入 ["/switch s2","/exit"] → 输出含「已切换会话 s2」且该行不含「条消息」计数）
  - GREEN:
    - `uv run pytest tests/test_cli.py -q`（全部转绿）
  - ASSERT:
    - 切换成功路径 loop.run 收到的 session_id 精确等于目标 id（不是新生成 id）
    - 全部 /switch 失败路径（缺参/多参/不存在）当前会话 id 不变、0 次异常上抛
    - 命令路径（含失败路径）loop.run 0 次调用
    - on_session_change 在成功切换时恰调用 1 次、失败路径 0 次
  - DoD:
    - `tests/test_cli.py` 全部转绿（含既有 20+ 用例回归）+ `uv run pytest` 全量全绿 + `__main__.py` 模块 docstring 与启动提示文案含 /switch + `codegraph sync` 执行成功
  - 最小验证: `uv run pytest tests/test_cli.py -q && PYTHONPATH=src uv run python -m harness --help`
