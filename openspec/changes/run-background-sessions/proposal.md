# Proposal — run-background-sessions

## Why

REPL 当前是**单线程同步阻塞式**：主线程读完一行输入后调用 `loop.run` 阻塞至整个 ReAct 回合跑完，期间无法读取任何新输入。由此产生两个真实使用缺陷：

1. **切走即丢**：会话 A 正在回答（LLM 仍在生成 / 多工具轮执行中）时，用户无法切到会话 B——唯一路径是等 A 跑完或放弃整轮；
2. **切来须等**：即便用户切到会话 B，任何新输入都必须排在 A 的回合之后被处理。

目标能力（对标 Codex 的「会话收发循环 + 信箱」模型，claude code 闭源无码可参考、仅行为对照）：会话 A 运行中输入 `/switch B`，`A` **继续在后台跑完**；B **立即可对话**；同会话内新输入**排队串行**（不并行、不插队），满足 `SessionStore` 同会话禁并发写的既有约束（`src/harness/session/store.py:20-24`）。

前置探索见根目录 `last-exploration-handoff.md`（2026-09-29）：决策树 19 节点全部 resolved（D1-D15 + C1-C3 已由用户拍板），无客观阻塞。C2 用户选定**立刻硬退**：不等待在跑回合、worker 一律 daemon 线程随进程同死；在跑回答可能丢失，会话文件若有半行损坏由读取器自愈跳过（`store.py:174-199`）。

## What Changes

- **新增 `src/harness/sessionworker.py`**（或 `runner.py` 邻位新模块，命名在 design 定）：
  - `SessionWorker`：每会话一个常驻 worker 线程 + 专属 `queue.Queue` 输入信箱；线程从队列取用户输入 → 调 `loop.run` 跑完整体轮（含流式渲染）→ 取下一单；同会话天然串行；
  - `SessionRouter`：维护 session_id → worker 映射；`submit(text, session_id)` 把消息投进当前会话信箱；活跃 worker 数上限 = 现有 `max_concurrent_sessions`（默认 4），超限拒收并提示；
  - worker 内 any-exception → 结构化可读输出（「该会话处理失败：……」），线程不因单次失败死亡（LLMError 沿用现文案，其他异常归类为处理失败）。
- **新增 `src/harness/outputmux.py`**（输出复用器，命名在 design 定）：
  - 一把 `threading.Lock` 包住全部终端写入（含每写一次 stdout 的流式碎片），杜绝多会话同时输出粘行；
  - 非当前前台会话的输出行首缀 `[<session_id 前 4 位>]`（C3）；前台会话输出不带前缀（与现状一致）；
  - 实现 `writer` 协议（`Callable[[str], None]`）与 `raw_writer` 协议，`run_repl` / `StreamRenderer` 楼层全部无感切换。
- **改造 `src/harness/__main__.py`**：
  - 主线程只做「读输入 + 分拣」：命令（/exit /new /switch /sessions /history）仍当前台同步处理；
  - 普通消息不再直接调 `loop.run`，改投 `SessionRouter.submit`（会话忙则入该会话队列，立即可返回读下一行）；
  - `/exit` 为硬退（C2）：不等待任何在跑回合，不 join worker（daemon 线程），进程直接退；
  - 渲染器（`StreamRenderer`）新建/绑定动作整体下沉到 worker 侧（渲染器实例含可变状态，禁跨会话共享，`renderer.py:25`），前台/胸牌判定由 outputmux 完成；
  - worker 侧补「跑完提示」。前台会话跑完仍输出答案/结尾；后台会话跑完输出「`[a1b2] 已完成`」一行提示（C1 实时混排）。
- **加固 `src/harness/trace.py`**：`TraceCollector` 内存字典（`_spans` / `_trace_sessions`）加锁 + 回合终局清理（trace 已落盘，内存登记表不再无限增长）——多会话长驻进程的内存卫生。
- **测试**：
  - 新增/扩展 `tests/test_cli.py`：路由/队列/命令语义用假 loop（MagicMock，模式同现状 TestSessionSwitch）；
  - 新增 `tests/test_sessionworker.py`：真并发/隔离/worker 生命周期用线程安全假模型（模式同 `tests/test_runner.py` 的 `ThreadSafeFakeLLM` + 并发文文件断言）；
  - 全部零网络、零真实 API（AGENTS.md 禁令）。

## Impact

- 文件：新增 `src/harness/sessionworker.py`、`src/harness/outputmux.py`、`tests/test_sessionworker.py`；修改 `src/harness/__main__.py`、`src/harness/trace.py`、`tests/test_cli.py`
- 依赖：零新增（纯标准库 `threading` / `queue`）
- 不动：`loop.py`（无可变实例状态可直接多线程共享，`tool`/`session`/`context`/`memory` 组件全量复用）、`renderer.py` 实现体（只在 worker 侧实例化）、`runner.py`（ConcurrentRunner 一次性批量模型与本案常驻模型并存，互不影响）、`config.py`（复用 `max_concurrent_sessions`，不加新键）
- 与现有 7 个 active change 无文件级冲突（`add-todo-tool`/`simplify-*` 均已勾选完成）
- README 与 AGENTS.md 无命令清单章节需同步（现有约定：方案真源在 openspec/，不在 doc/）

## 非目标（Non-goals）

- ❌ 不做打断/中断正在跑的回合（Codex 的 interrupt/steer 机制；想停就等它跑完或关闭进程）
- ❌ 不做输入插队/转向（steer）：忙碌会话的新输入只排队、不进正在跑的这轮
- ❌ 不做会话删除/重命名/跨进程恢复
- ❌ 不做全局等待队列（超限即拒收并提示，不排队等待 worker 空闲）
- ❌ 不引入 asyncio 或任何 agent 框架（AGENTS.md 禁令；全同步线程模型）
- ❌ 不改 `ConcurrentRunner` 的批处理语义（一次性批量跑完，与常驻 worker 模型并存）
- ❌ 不做闲置会话「回锅提醒」注入（explore 中 D15 曾提及，划出最小范围：summarizer 写入的总结记录对 builder 可见性另行验证，且无用户明确诉求）

## 验收标准（可验证）

1. `uv run pytest` 全绿（现有 26 用例 test_cli + 新增 worker 测试），全量既有测试无回归、零真实网络调用
2. REPL 中会话 A 请求在途（LLM 未返回）时输入 `/switch B`：`loop.run` 所在线程仍在执行（假 LLM 阻塞型测试断言 worker 线程存活），会话 A 无「打断」日志/代码路径触发
3. 切换后立刻向 B 输入新消息：B 的 worker 线程启动或复用，A 的回合**未完成前** B 已开始产出（双线程并行测试断言，假 LLM 各自 hold 0.2s 验证真并发）
4. 同会话连发两条消息：第二条入队等待，第一条 `loop.run` 返回后第二条才开始（队列串行断言：串行执行、先完成先出队）；不同会话各自并行互不阻塞
5. 非当前会话的输出带 `[xxxx]` 前缀且落屏无粘行（多线程同刻写 stdout 的锁测试断言）
6. worker 内抛非 LLMError 异常（假 loop 抛 RuntimeError）：输出「该会话处理失败」类可读提示，worker 线程存活可继续处理下一条输入
7. 活跃 worker 数达到 `max_concurrent_sessions` 后再提交新会话：输出护栏提示，消息不入队（不阻塞主线程）
8. `/exit` 时不 join 不等待：main 返回立即结束（现有调用点 `__main__.py:245-250` finally 只 stop summarizer，行为对齐 C2 硬退）
