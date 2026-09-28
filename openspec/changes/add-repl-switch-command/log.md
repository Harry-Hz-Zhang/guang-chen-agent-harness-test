# Log — add-repl-switch-command

## 2026-09-27 explore

| 事件 | 说明 |
| --- | --- |
| 探索启动 | 用户指出 REPL 缺陷：已有会话无法在交互中切换，仅能启动时 `--session <id>` 续接；要求新增 `/switch <id>` 命令并走 VSDD 流程建独立 change |
| codegraph status | 索引最新（51 文件 / 756 节点 / 2219 边），可直接分析 |
| codegraph explore/context | 定位 `run_repl`（`src/harness/__main__.py:91`）：命令分发仅 `/exit` `/new` `/sessions` `/history` 四个精确匹配分支，`/new` 只能造新 id——缺陷确认 |
| 复用设施核实 | `SessionStore.session_ids()`（store.py:120，列出全部会话 id）与 `on_session_change` 回调（__main__.py:233，驱动 `RebindableTodoTool` 换绑）均已存在且有测试覆盖，切换语义与 `/new` 同构 |
| 影响面分析 | codegraph blast radius：改动收敛于 `__main__.py` + `tests/test_cli.py`；session store / loop / renderer / summarizer 均以 session_id 寻址，无需感知切换 |
| 文档核实 | README 与 AGENTS.md 均未列举 REPL 命令清单，无文档同步负担 |
| 阻塞 | 无（用户已在指令中给出方案方向：新增 `/switch <id>`，并授权 plan 后直接修改） |

## 2026-09-27 propose

| 事件 | 说明 |
| --- | --- |
| change 创建 | `openspec/changes/add-repl-switch-command/`（与 build-minimal-agent-runtime、refactor-stream-renderer 相互独立，无文件级冲突） |
| 复杂度判定 | 🟢 standard（单模块、单 task、纯表现层小改动；但按公约仍走 RED/GREEN/ASSERT/DoD 完整循环） |
| artifacts | proposal.md（非目标 6 条 + 可验证验收 7 条）→ design.md（命令解析伪码 + 数据流 + 3 处被否决备选：退出重启 / 两步交互 / 前缀匹配）→ tasks.md（单 task，RED 8 条，覆盖 happy / 异常（不存在）/ 边界（缺参、多参、幂等、空会话）/ 回调 / 调用次数） |
| 关键决策 | ①分词匹配（split 后比 parts[0]）防 `/switchxxx` 误命中；②恰好 1 个参数否则用法提示；③存在性以 session_ids() 为准；④N==0 时提示不带「条消息」计数；⑤切当前会话幂等处理；⑥文案「已切换会话 <id>（N 条消息）」与启动续接提示同构 |
| HARD-GATE | 用户指令已含「plan 后进行修改」的明确授权，propose 后直接进入 apply（无需二次确认） |

## 2026-09-27 apply

| 事件 | 说明 |
| --- | --- |
| RED | `tests/test_cli.py` 新增 `TestSessionSwitch`（8 用例），运行确认 8/8 失败（/switch 被当普通输入路由进 mock loop），既有用例 0 回归 |
| GREEN | `src/harness/__main__.py` 新增 `_SWITCH_COMMAND` 常量与命令分支（分词解析 + 恰一参数校验 + session_ids 存在性校验 + on_session_change 换绑 + 消息数提示），模块 docstring / run_repl docstring / 两处启动提示文案同步 |
| sync | `codegraph sync` 成功（3 changed files / 89 nodes / 94ms） |
| verify | `uv run pytest tests/test_cli.py -q` 26/26 全绿；全量 `uv run pytest` 165/165 全绿；`PYTHONPATH=src uv run python -m harness --help` exit 0；lint 0 错误 |
| 结果 | 验收标准 1-7 全部满足；未 commit（遵循「提交由人工确认」公约） |
