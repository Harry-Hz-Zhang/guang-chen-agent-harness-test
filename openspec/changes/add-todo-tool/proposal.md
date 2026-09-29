# Proposal — 加回 todo 工具（write_todos：会话隔离 + 并发可同时执行的内存态公共状态）

## 1. 背景与动机

### 1.1 旧版 todo 为何被删（openspec/changes/simplify-tools）

旧版 `todo.py`（150 行）按会话持久化到 `data/todos/<session_id>.json`，并为此在 `__main__.py` 拖出 `RebindableTodoTool` 换绑类、`on_session_change` 回调、`session_ref` 共享字典——复杂度全部来自"per-session **文件**存储 + 换绑回调"，而非会话隔离本身。simplify-tools 已连根删除。

### 1.2 业界调研结论（2026-09）

| 实现 | 工具形态 | state 位置 | 持久化 | 会话隔离 | 更新语义 |
| --- | --- | --- | --- | --- | --- |
| LangChain deepagents（TodoListMiddleware） | 单工具 `write_todos`，入参整张列表 | graph state 的 `todos` 通道，工具经注入的运行时上下文访问 | checkpointer 决定 | 按 thread_id 隔离 | **全量替换** |
| Claude Code（TodoWrite） | 单工具，入参 `[{content, status}]` | 会话内存态 | 会话内、退出即丢 | 每会话独立 | **全量替换**，三态 status |
| OpenHands SDK | TaskCreate/Get/Update/List 四件套 | ConversationState 单一事实源 | event log | 每会话独立 | 增量 |

共识：单工具 + 全量替换 + status 三态（pending/in_progress/completed）+ 内存态 + 工具无状态、数据放公共 state、运行时把会话上下文注入工具。

### 1.3 本项目形态（用户修正后的需求）

要求：**尽可能简单、内存实现、工具形式、公共 state、必须会话隔离、多个 agent（每会话一个协程/线程）可同时执行**。

本项目 `BaseTool.execute(**kwargs)` 无会话参数，LLM 也不该传 session_id。标准解法是 `contextvars.ContextVar`：**协程（asyncio Task 各自拷贝上下文）与线程（各自独立上下文）天然隔离**，由 `ReactLoop.run()` 入口绑定、出口复位——这正是"一个 agent 一个协程/线程"的并发模型。`RuntimeState` 按 session_id 键控存 todos 并持锁，多线程并发写互不串扰。无任何文件 IO、无换绑回调。

## 2. 目标

1. **新增 `src/harness/state.py`**：`RuntimeState`（dataclass：`todos_by_session: dict[str, list[dict]]` + 内部 `threading.Lock`，提供 `set_todos` / `todos` 两个线程安全方法）与 `CURRENT_SESSION_ID: ContextVar[str]`（默认空串）。
2. **新增 `src/harness/tools/todo.py`**：`WriteTodosTool(BaseTool)`，注入 `RuntimeState`；execute 从 `CURRENT_SESSION_ID.get()` 取当前会话（未绑定→结构化报错），校验入参后**全量替换**该会话的 todos，返回编号+中文状态渲染。校验：list、≤100、每项 dict、content 非空字符串、status ∈ 三态枚举；失败抛 `ToolExecutionError`。
3. **`ReactLoop.run()` 绑定会话上下文**：入口 `CURRENT_SESSION_ID.set(session_id)`，`finally` 复位——同进程内多线程/协程各自执行不同会话时互不可见（同时执行的基础）。
4. **接线**：`_build_registry(memory, state)` 注册第 5 个工具；`main()` 创建 `RuntimeState()` 注入。
5. **提示词**：`SYSTEM_PROMPT` 工具约定追加第 4 条（多步任务先写 write_todos 计划）。
6. **测试先行**：`TestWriteTodos`（会话隔离、上下文缺失报错、校验、全量替换、清空、渲染、**双线程并发写隔离**）；`test_loop.py` 新增：loop.run 期间工具读到正确 session_id 且退出后复位 + **双线程并发 run 会话隔离**；`test_cli.py` 适配 5 工具断言。
7. **文档同步**：README / CODEGRAPH.md / AGENTS.md 目录树。

## 3. 非目标（Non-goals）

1. **不落盘**：todos 纯内存，进程退出即丢；旧版 `data/todos/` 文件机制不复活。
2. **不提供读取工具/REPL 命令**：write_todos 返回值自带完整渲染，会话历史天然可回看。
3. **不做 nag reminder、不做 Task 四件套**（Claude Code 新方向，更重）。
4. 不把同步 LLM 客户端改为 async（并发由线程承载即可；ContextVar 对协程同样成立，未来迁移零改动）。
5. 不改 `doc/PRD.md`；不动 parser / registry / session / memory / renderer 层（loop.py 仅入口加绑定三行）。

## 4. 验收标准

1. `uv run pytest` 全绿（169 → 180 项：+7 TestWriteTodos、+2 test_loop、test_cli 适配 1 项不增数）。
2. `_build_registry(memory, state)` 注册恰 5 个工具，含 `write_todos`。
3. **会话隔离可验证**：session A 写入不影响 session B；工具执行取的是上下文中绑定的会话。
4. **同时执行可验证**：双线程各自绑定会话并发写 todos / 并发 `loop.run()`，结果按会话正确隔离、无串扰（测试固化）。
5. **简单性预算**：`state.py` ≤ 60 行、`todo.py` ≤ 130 行；`grep -rn "RebindableTodoTool\|on_session_change\|session_ref" src/ tests/` 零命中。
6. 新文件无文件 IO、无 print、全函数类型注解 + 中文 docstring、入参显式校验且结构化报错。
7. 文档同步三处；`codegraph sync` 成功。

## 5. 风险与权衡

| 权衡 | 决策 | 理由 |
| --- | --- | --- |
| ContextVar vs 改 BaseTool 签名传 session | ContextVar | 不动全部工具与调用点；协程/线程语义原生正确（LangGraph 同思路：工具经运行时注入拿 thread 级 state） |
| 进程内 dict 键控 vs 每会话独立 state 实例 | 单实例键控 + 锁 | 工具单例注册即可服务所有会话；避免每会话重建 registry（旧版换绑的反面） |
| 全量替换 vs add/list | 全量替换 | 主流共识，无 ID 管理，校验简单 |
| 内存态 vs 文件持久化 | 内存 | 用户明示；文件持久化正是旧版根因 |
| SYSTEM_PROMPT 加不加引导 | 加一句 | 不加则 LLM 极可能从不调用；deepagents 官方同样注入 |
| 会话结束清理 todos 槽位 | 不清理 | 进程生命周期内量级可控；退出即丢 |
