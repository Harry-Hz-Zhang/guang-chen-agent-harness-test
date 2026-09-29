# Design — write_todos 工具：RuntimeState 会话隔离公共状态 + ContextVar 并发绑定

## 1. 变更全景

```
src/harness/state.py              [ADD]  CURRENT_SESSION_ID(ContextVar) + RuntimeState(键控+锁)
src/harness/tools/todo.py         [ADD]  WriteTodosTool（读上下文会话 → 校验 → 全量替换 → 渲染）
src/harness/tools/__init__.py     [MOD]  导出 WriteTodosTool
src/harness/loop.py               [MOD]  run() 入口绑定 CURRENT_SESSION_ID、finally 复位（+3 行）
src/harness/__main__.py           [MOD]  _build_registry(memory, state)；main() 创建 RuntimeState()
src/harness/prompts.py            [MOD]  SYSTEM_PROMPT 追加工具约定第 4 条
tests/tools/test_builtin_tools.py [MOD] 新增 TestWriteTodos（7 用例）
tests/test_loop.py                [MOD] 新增 2 用例（上下文绑定与复位 / 双线程并发隔离）
tests/test_cli.py                 [MOD] testRegistryIncludesReadMemory 适配
README.md / CODEGRAPH.md / AGENTS.md [MOD] 文档同步
```

## 2. src/harness/state.py

```python
CURRENT_SESSION_ID: ContextVar[str] = ContextVar("harness_current_session_id", default="")
```

- 由 `ReactLoop.run()` 入口 `set(session_id)`、`finally` `reset(token)`。
- **并发语义**：asyncio 每个Task 拷贝创建时上下文、线程各自独立上下文 → 多会话同时执行时，每个执行流读到的都是自己的 session_id（"一个 agent 一个协程/线程"的标准载体）。

```python
@dataclass
class RuntimeState:
    """运行时公共状态（纯内存、按会话隔离、线程安全）。"""
    todos_by_session: dict[str, list[dict]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_todos(self, session_id: str, todos: list[dict]) -> None:
        """全量替换指定会话的待办列表（线程安全）。"""
        with self._lock:
            self.todos_by_session[session_id] = todos

    def todos(self, session_id: str) -> list[dict]:
        """读取指定会话的待办列表副本（线程安全；无记录时空列表）。"""
        with self._lock:
            return list(self.todos_by_session.get(session_id, []))
```

## 3. WriteTodosTool（src/harness/tools/todo.py）

- `__init__(state: RuntimeState)`：注入公共状态；工具自身无状态，**单实例可被多会话并发共享**。
- `execute(**kwargs)`：
  1. `session_id = CURRENT_SESSION_ID.get()`；空 → `ToolExecutionError("write_todos 需要会话上下文…")`（结构化，含 tool/tool_args）。
  2. 校验（下表）→ `state.set_todos(session_id, checked)`（全量替换，持锁）。
  3. 返回渲染文本（下 §3.3）。

### 3.1 校验表（不信任 LLM 输出；错误标注第几项，1 基）

| 输入情形 | 行为 |
| --- | --- |
| 上下文无会话 | ToolExecutionError：需要会话上下文 |
| `todos` 缺失 / 非 list | ToolExecutionError：todos 必须为数组 |
| 长度 > 100 | ToolExecutionError：待办数量超上限 |
| 某项非 dict | 第 N 项必须是对象 |
| `content` 非字符串 / strip 后空 | 第 N 项 content 必须为非空字符串 |
| `status` 不在 {pending, in_progress, completed} | 第 N 项 status 非法 |

规范化为 `{"content": str, "status": str}` 后存储。

### 3.2 parameters schema

`todos: array of {content: string, status: enum[pending, in_progress, completed]}`，`required: ["todos"]`；description 写明"全量替换、多步任务先写计划、随进度更新状态"。

### 3.3 返回渲染

```
已写入 2 条待办：
1. [进行中] 调研业界实现
2. [待办] 写 proposal
```

status→中文：pending→待办、in_progress→进行中、completed→已完成；空列表返回 `已写入 0 条待办（清空）`。

## 4. ReactLoop 绑定（loop.py，最小改动）

```python
def run(self, user_input, session_id, on_event=None) -> LoopResult:
    token = CURRENT_SESSION_ID.set(session_id)
    try:
        ...（原方法体整体不变，仅缩进进 try）...
    finally:
        CURRENT_SESSION_ID.reset(token)
```

- 绑定在 `run()`（agent 执行单元）而非 REPL：并发执行的最小单位是一次 run；多线程各自 run 不同会话即天然隔离。
- `finally` 复位防止上下文泄漏到执行流后续操作（如压缩中间件、summarizer 线程）。
- 旧机制（RebindableTodoTool / on_session_change / session_ref）零复活。

## 5. 接线与提示词

- `__main__.py`：`state = RuntimeState()`；`_build_registry(memory, state)` 注册 calculator / search / weather / read_memory / **write_todos** 五工具。
- `prompts.py` SYSTEM_PROMPT 工具约定追加：
  > 4. 执行包含多个步骤的任务时，先用 write_todos 工具写入完整待办计划（全量替换，status 用 pending / in_progress / completed），随执行进度更新状态，再继续调用其他工具。

## 6. 测试设计（RED 先行）

### 6.1 TestWriteTodos（tests/tools/test_builtin_tools.py）

模块内加 `_session_context` contextmanager 辅助（set/reset ContextVar）。

| 用例 | 断言 |
| --- | --- |
| `testSessionIsolatedState` | ctx s1 写 2 条、ctx s2 写 1 条 → `state.todos("s1")` 2 条 / `state.todos("s2")` 1 条，内容各归各 |
| `testFullReplaceOverwrites` | 同会话先写 3 条再写 2 条 → `state.todos("s1")` 恰为新 2 条 |
| `testMissingSessionContextRejected` | 不绑定上下文直接执行 → ToolExecutionError，match "会话" |
| `testStatusEnumRejected` | status="done" → match "status" |
| `testEmptyContentRejected` | content="  " → match "content" |
| `testNonListTodosRejected` | todos="abc" → match "数组" |
| `testEmptyListClears` | 写 2 条再写 `[]` → `state.todos("s1") == []`，返回含 "清空" |
| `testResultRendersStatus` | 三种 status → 返回含 "1. [进行中]" "2. [已完成]" "3. [待办]" |
| `testConcurrentSessionsIsolated` | 双线程各自绑定会话并发各写 20 条 → 两会话各 20 条、首条内容各归各（同时执行隔离） |

（注：工具单元用例 9 个，其中并发 1 个；proposal 计数以实际为准 +9/+2。）

### 6.2 test_loop.py 新增

| 用例 | 断言 |
| --- | --- |
| `testSessionContextBoundDuringToolExecution` | 注册"捕获工具"（execute 里记录 `CURRENT_SESSION_ID.get()`）+ FakeLLM 脚本一次工具调用 + 最终答案；`loop.run(..., "s-ctx")` 后捕获值 == "s-ctx"，且 run 返回后 `CURRENT_SESSION_ID.get() == ""`（复位） |
| `testConcurrentRunsIsolated` | 同一 loop 实例 + 共享 RuntimeState + WriteTodosTool；两个线程分别 `loop.run` 不同会话（各自 FakeLLM 脚本 write_todos→答案）；join 后两会话 todos 各归各、结果正确（同时执行证明） |

线程内各自构造 FakeLLM 与脚本（避免共享可变列表），session 文件天然分会话隔离。

### 6.3 test_cli.py 适配

`testRegistryIncludesReadMemory` → `_build_registry(MemoryStore(tmp_path), RuntimeState())`；断言 5 工具（含 `"write_todos" in names`）。

## 7. 设计决策记录

| # | 决策 | 依据 |
| --- | --- | --- |
| D1 | ContextVar 绑定会话，不改 BaseTool 签名 | 协程/线程原生隔离；LangGraph 工具经注入拿 thread 级 state 同思路；改动面最小 |
| D2 | todos 按 session_id 键控于单一 RuntimeState + 锁 | 工具单例服务全会话，免换绑；并发写安全 |
| D3 | 全量替换、无 add/list | deepagents / Claude TodoWrite / s05 三方一致 |
| D4 | 内存态、不落盘、不随 /new /switch 清理 | 用户明示；旧版根因；进程生命周期内量级可控 |
| D5 | SYSTEM_PROMPT 一句话引导 | 不加则工具大概率不被调用 |
| D6 | status 英文枚举、渲染中文 | schema 对 LLM 稳定；展示本地化 |
