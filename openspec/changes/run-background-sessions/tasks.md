# Tasks — run-background-sessions

<!-- 进度 MUST 用行首 Markdown checkbox，与 OpenSpec apply 一致。禁止仅用 ### Task 或行内 完成状态：`[ ]` -->

> complexity: 🔴 standard | phase: propose

<!--
  本 change 的 4 个 task 按 design §9 依赖图排序：T1（outputmux）→ T2（sessionworker）→ T3（REPL 接线 + 测试迁移）串行；
  T4（trace 加固）独立可与 T1-T3 并行。RED 均已穷举到方法名 + mock 输入 + 断言点。
  测试类命名等 camelCase 枚举沿用 openspec/changes/*/tasks.md 既有 RED 原名约定（AGENTS.md 命名规范例外条款）。
-->

## Wave 1

- [x] Task 1: 实现输出复用器 OutputMux
  - complexity: 🟡
  - files:
    - Create: `src/harness/outputmux.py`
    - Test: `tests/test_outputmux.py`
  - RED:
    - `TestOutputMux#shouldPassthroughForegroundSessionLine`（前台会话 line("你好", sid="a1b2c3d4") 且 set_foreground("a1b2c3d4") → writer 收到原文本行，不含 `[` 前缀）
    - `TestOutputMux#shouldPrefixBackgroundSessionLine`（set_foreground("ffffffff") 后 line("你好", sid="a1b2c3d4") → writer 收到 `[a1b2] 你好`，前缀为 8 位 id 的前 4 位）
    - `TestOutputMux#shouldEmitIdleHintForBackgroundSession`（session_idle_hint("a1b2c3d4") 且非前台 → writer 收到一行 `[a1b2] 已完成`；前台会话的 idle hint 不输出）
    - `TestOutputMux#shouldEmitIdleHintForForegroundSession`（前台会话 session_idle_hint → 不产生任何 writer/raw_writer 调用——design §7 idle hint 前台抑制；原描述「输出无前缀已完成行」与 design §7 裁定冲突，apply Task 1 时 reverse-sync 对齐）
    - `TestOutputMux#shouldBufferBackgroundChunkUntilNewline`（后台会话连续 chunk("思")、chunk("考")、chunk("\n") → writer 收到单次调用 `[a1b2] 思考\n` 整行，无行内碎片落屏——W3 行同步语义）
    - `TestOutputMux#shouldStreamForegroundChunkPiecewise`（前台会话 chunk("思")、chunk("考") → raw_writer 依序收到 "思"、"考" 两个分片各自独立调用，不缓冲——前台逐字流式保持现状）
    - `TestOutputMux#shouldFlushPendingBufferOnIdle`（后台会话 chunk("半行") 后未换行即 session_idle_hint → writer 收到 `[a1b2] 半行` 完整落屏，不丢缓冲）
    - `TestOutputMux#shouldSuffixExceptionOnGuard`（mock writer 抛 RuntimeError → mux.line 不上抛（吞异常仅记录 log），主循环不死——输出层永不成为崩溃源）
    - `TestOutputMux#shouldHoldLineOwnershipAcrossSessions`（两会话 A/B 交替 chunk 且都无换行、然后各自 "\n" → A 的行含 "[aaaa] " 前缀与 A 的内容、B 的行含 "[bbbb] " 与 B 的内容，两行各自完整，无交错——多线程锁语义用线程组交替驱动模拟）
    - `TestOutputMux#shouldTreatNullSessionAsForegroundStyle`（line("系统提示", session_id=None) → 无前缀原样输出——系统提示不带胸牌）
    - `TestOutputMux#shouldFlushFinalNewlineThenIdleHintAsTwoLines`（后台会话 chunk("半行")、chunk("\n")、session_idle_hint → writer 恰收到两次调用：`[a1b2] 半行` 与 `[a1b2] 已完成` 两行——finalize 换行与 idle hint 尾行组合，无粘连、无多余空行；design review 补条）
  - GREEN:
    - `uv run pytest tests/test_outputmux.py -q`（全部转绿）
  - ASSERT:
    - writer/raw_writer 的每次调用入参与期望逐字符精确匹配（含前缀与换行符）
    - 前台路径：raw_writer 收到的分片序列与输入分片一一对应，无合并无拆分（verify 调用次数 == 分片数）
    - 后台路径：半行缓冲仅在换行或 idle hint 时整行 flush，flush 前 writer 0 次调用
    - writer 抛异常路径：mux.line 返回后线程存活、无未捕获异常逃逸（用 catch 断言无 raise）
    - session_id=None 的输出永远不带前缀
  - DoD:
    - `uv run pytest tests/test_outputmux.py -q` 全绿（≥10 条用例）
    - `uv run pytest` 全量既有测试无回归
    - `mypy`/类型注解完备（全 public 函数含返回值注解，中文 docstring）——以仓内无 mypy 配置为由跳过类型注解即违反 AGENTS.md §4
    - `codegraph sync` 执行成功（新增模块入索引）
  - 最小验证: REPL 之外单独实例化 mux，两个假线程各写 100 行，输出无行交错

- [x] Task 2: 实现会话工人 SessionWorker 与路由器 SessionRouter
  - complexity: 🔴
  - files:
    - Create: `src/harness/sessionworker.py`
    - Test: `tests/test_sessionworker.py`
  - RED:
    - `TestSessionWorker#shouldRunTurnInBackgroundThread`（假 runner 记录调用线程 id → submit("问题") 后 join 等待 → 断言 runner 被调用 1 次、正在调用的线程 != 主线程 id、入参 text 逐字匹配）
    - `TestSessionWorker#shouldSerializeInputsWithinSameSession`（假 runner 首次调用阻塞 0.2s 并记录 start/end 时间戳，同会话快速 submit 两条 → 断言第二条 start >= 第一条 end（无重叠区间），且完成顺序 = 提交顺序）
    - `TestSessionRouter#shouldRunDifferentSessionsInParallel`（两会话各 submit 一条、假 runner 0.2s hold → 断言两 runner 的 [start,end] 区间存在重叠（真并行），参照 test_runner.py:215-217 max_active>=2 峰值法）
    - `TestSessionRouter#shouldReturnImmediatelyOnSubmit`（假 runner 阻塞不返回 → submit 调用本身 < 0.1s 返回（time 减法），SubmitOutcome.accepted=True——主线程不阻塞语义）
    - `TestSessionWorker#shouldSurviveNonLLMException`（假 runner 前 2 条抛 RuntimeError("disk full") 后正常处理 → 提交 3 条 → 断言 mux 收到 2 条「该会话处理失败」提示（含 RuntimeError 字样）、第 3 条正常 runner 调用、worker.alive=True）
    - `TestSessionWorker#shouldCatchLLMErrorWithExistingWording`（假 runner 抛 LLMError("timeout") → 断言 mux 收到「出错了：timeout」而非「处理失败」——区分模型失败与处理失败文案）
    - `TestSessionRouter#shouldEnforceMaxConcurrentSessions`（config.max_concurrent_sessions=2，向 2 个新会话各 submit 1 条（runner 阻塞中）→ 第 3 个新会话 submit → SubmitOutcome.accepted=False、reason 含「并发」字样，且第 3 会话的 runner 0 次调用）
    - `TestSessionRouter#shouldNotCountBusyExistingSessionAgainstLimit`（上限 2、会话 A 已有 worker 在跑 → 再向 A submit 一条 → accepted=True（忙碌老会话不受上限约束，上限管的是会话数不是消息数））
    - `TestSessionRouter#shouldReuseWorkerForSameSession`（同会话 submit 两条 → router.active_worker_count()==1（同会话不重复建工人））
    - `TestSessionRouter#shouldEmitIdleHintAfterTurnEnds`（假 runner 返回后 → 断言 mux 收到该会话的 idle hint 调用恰 1 次（「已完成」），且在 runner 调用之后（顺序断言））
    - `TestSessionWorker#shouldKeepDaemonThread`（worker.start() 后 thread.daemon is True——C2 硬退前提）
    - `TestSessionRouter#shouldReviveDeadWorkerOnSubmit`（向 router 项置一个已自然死亡的 stub worker（线程函数立即 return 或以 None sentinel 结束，alive=False）后再 submit → 新 worker 的 runner 被调用、active_worker_count 不变（复活重建不重复计数）——保险路径；Python 无线程 kill 原语，以「预置死亡 stub」代替「手动杀死」，design review 修订）
    - `TestSessionWorker#shouldExitOnNoneSentinel`（worker.submit 一条 in-flight 后 worker.close()×2 投 None sentinel → 断言 runner 恰 1 次调用（FIFO 排空语义：sentinel 排在在途消息之后，先排空再退出）、worker.alive 最终转 False、close 可重入无副作用——测试 teardown 卫生的前提（apply 阶段核查第 5 项补条；2026-09-30 reverse-sync ⑤ 裁定 FIFO 排空语义，log.md 有备案））
  - GREEN:
    - `uv run pytest tests/test_sessionworker.py -q`（全部转绿）
  - ASSERT:
    - runner 调用次数/入参：同会话 N 条输入 → runner 恰 N 次按序调用，入参 text 逐字匹配
    - 并行/串行精确断言：同会话时间区间无交集；异会话时间区间必有交集（针对 0.2s hold 的确定性窗口）
    - 护栏短路：超限 submit 时新会话 runner 0 次调用、submit 同步返回 accepted=False
    - idle hint：每回合结束恰 1 次、顺序在 runner 之后、非前台会话由 mux 负责前缀（worker 只调 session_idle_hint(sid)，断言 sid 入参为该会话 id）
    - 异常路径：worker 线程在 N 次异常后仍 alive，失败输出文案含异常类名
    - daemon：所有 worker 线程 daemon=True（verify 线程属性，硬退不挂进程）
  - DoD:
    - `uv run pytest tests/test_sessionworker.py -q` 全绿（≥12 条用例）
    - `uv run pytest` 全量既有测试无回归
    - 同会话写文件 + 全量组件真装配下跑 3 会话并发（真 SessionStore(tmp_path) + MagicMock 其余依赖），各会话 JSONL 完整无交错半行（用 load_records 校验）
    - `codegraph sync` 执行成功
    - worker 支持 None sentinel 退出（inbox 收到 None 即结束循环），tests/test_sessionworker.py 用 fixture teardown 统一投递收尾，用例间无线程累积（design review Minor：daemon 线程卫生）
  - 最小验证: 手动 REPL 冒烟（--no-stream 避开渲染复杂度）：A 问慢问题 → /switch B → B 立即回答 → A 后台完成并打印 [前缀] 已完成

- [x] Task 3: 接线 REPL 主循环切线程分拣并迁移既有测试
  - complexity: 🔴
  - files:
    - Modify: `src/harness/__main__.py`
    - Modify: `tests/test_cli.py`
  - RED:
    - `TestReplRouting#shouldSubmitPlainInputToRouterWithoutBlocking`（MagicMock router（submit 返回 accepted=True）+ lines 两条普通输入 → run_repl 返回时 router.submit 被调用恰 2 次、入参 text 逐字匹配；loop 0 次直接调用——主线程不再碰 loop.run）
    - `TestReplRouting#shouldPrintGuardHintWhenSubmitRejected`（mock submit 返回 SubmitOutcome(False, "并发已达上限") → 输出含「并发」提示行，且下一条输入仍被处理（REPL 不退出））
    - `TestReplRouting#shouldSwitchForegroundWithoutTouchingWorkers`（A 有在跑 worker（假 router 记录调用）→ /switch B → mux.set_foreground("B") 被调、router 的任何 stop/join/interrupt 类方法 0 次调用——「切走不打断」核心断言）
    - `TestReplRouting#shouldRunNewMessagesViaWorkerThread`（真 SessionRouter + 假 runner 记录线程 id：普通输入 → run_repl 全部消费完后 runner 被调用 1 次且线程 != 主线程——run_repl 收尾需 flush 队列/等待 workers 完成后返回，断言最终一致性）
    - `TestReplRouting#shouldExitImmediatelyWithoutJoiningWorkers`（假 router 有在跑 runner（阻塞）→ /exit → run_repl 在 0.1s 内返回（不 join、不等待，C2 硬退））
    - `TestReplRouting#shouldRunTurnInWorkerWithSessionId`（真 router + MagicMock loop：输入 "你好"（当前会话 s1）→ flush 后 loop.run 被以（"你好", "s1") 精确入参调用——原语义保持）
    - `TestReplRouting#shouldPassStreamRendererPerTurnInWorker`（stream_enabled=True：flush 后每回合 loop.run 收到的 on_event 可调用且两次回合的 on_event 不是同一实例——渲染器每请求独立（禁共享））
    - `TestReplRouting#shouldKeepCommandSemanticsUnchanged`（/exit /new /sessions /history 与 /switch 用法错误、目标不存在等 7 条既有 TestSessionSwitch + 命令用例全部保留通过——行为不回归）
    - `TestReplRouting#shouldPromptSessionOnStart`（无 --session 启动 → 输出「已创建新会话」提示行（含命令列表），会话 id 为 8 位 hex——回归锚点）
    - `TestReplRouting#shouldWriteAnswerWhenStreamDisabled`（--no-stream + Magicloop 返回 result(answer="答案", truncated=False) → flush 后输出恰含「答案」整段——非流式输出路径不回归）
  - GREEN:
    - `uv run pytest tests/test_cli.py -q`（全部转绿，含保留的原 26 用例或其迁移版）
    - `uv run pytest` 全量（165+ 新增，全绿）
  - ASSERT:
    - router.submit 恰按输入顺序调用、text 精确匹配；loop.run 全部经 worker 线程（主线程 0 次直接调用，用调用方线程 id 断言）
    - /switch 路径：mux.set_foreground 被调用恰 1 次入参为目标会话；router 无 stop/join 系方法被调用（verify 0 次）
    - /exit 路径：无任何 join/wait（耗时断言 < 0.1s）
    - 命令路径 loop.run 0 次调用（延续既有语义）
    - flush 语义：run_repl 返回前所有已入队消息均完成（総 runner 调用数 == 普通输入数）
  - DoD:
    - `uv run pytest tests/test_cli.py tests/test_sessionworker.py tests/test_outputmux.py -q` 全绿
    - `uv run pytest` 全量全绿零网络
    - `PYTHONPATH=src uv run python -m harness --help` exit 0
    - 手工冒烟：真 CLI（DeepSeek key 在环境变量时）A 慢问题 → /switch B → B 即答 → A 完成 -- [-- 已完成，流式/非流式各一遍，输出含 [xxxx] 胸牌]
    - 若既有用例迁移超过 26 条中 6 条需改测试断言主体 → 触发 reverse-sync（design §7 预案）
    - `codegraph sync` 执行成功
  - 最小验证: 手工冒烟见 DoD；自动回归覆盖其余

- [x] Task 4: 加固 TraceCollector 内存登记表的锁与清理
  - complexity: 🟡
  - files:
    - Modify: `src/harness/trace.py`、（仅当 design §2.5 验证点成立时）`src/harness/loop.py`
    - Test: `tests/test_trace.py`
  - RED:
    - `TestTraceCollector#shouldReleaseTraceEntriesAfterEndTrace`（start_trace 后登记表非空 → end_trace 同 id → 内存登记 dict 不再含该 trace id——回合结束即清理）
    - `TestTraceCollector#shouldKeepExportedFileUnchangedByCleanup`（多 span 完整回合 → export 落盘 → end_trace 清理 → 再读盘文件内容与清理前逐字节一致——落盘不受清理影响）
    - `TestTraceCollector#shouldAcceptConcurrentStartEndAcrossThreads`（8 线程 × 各 50 次 start/end_llm_span 并发 → 无异常无死锁（超时保护 5s），登记中途条目数 <= 并发上限×span 数（无脏计数））
    - `TestTraceCollector#shouldExportMultiSessionTracesWithoutCrosstalk`（两会话 worker 并发完整回合 + export → 两份 jsonl 各自只含自身 conversation id 的条目（无串写）——spec「并发登记互不串扰」）
    - `TestTraceCollector#shouldNotGrowMemoryAcrossManyTurns`（同会话连续 100 回合的 start/end_trace → 清理后登记 dict 大小回到回合间基线（仅活跃 trace 存在）——不再无限增长）
    - `TestTraceCollector#shouldKeepLegacyApiCompatible`（现有 test_trace.py 全部用例不改仍全绿——向后兼容，export 时机语义不变）
  - GREEN:
    - `uv run pytest tests/test_trace.py -q`（全部转绿，含既有用例）
  - ASSERT:
    - end_trace 后 `_spans`/`_trace_sessions` 恰好不含该 trace 的全部条目（精确键集合断言）
    - 落盘文件在清理前后字节级一致（hash 或全文比较）
    - 并发 5s 超时内完成、无 exc；两会话文件内容互不包含对方 conversation id
    - 既有 test_trace.py 的调用方式（不传新参数）全部通过——API 向后兼容
  - DoD:
    - `uv run pytest tests/test_trace.py -q` 全绿（既有 + 新增 ≥6 条）
    - `uv run pytest` 全量全绿
    - 若需要 loop.py 增补 end_trace 调用点：先在 log.md 记录 reverse-sync（design §2.5 预案条款），修改行数 ≤ 5 行
    - `codegraph sync` 执行成功
  - 最小验证: 长跑冒烟——同一 REPL 60 连问后进程 RSS 不持续攀升（约断：mem trace dict 条目数稳定）

## 任务依赖关系

```
T1 OutputMux ──▶ T2 SessionWorker/Router ──▶ T3 REPL 接线 + 测试迁移
                     (注入 mux)                  (注入 router；既有用例迁移)
T4 Trace 加固（独立分支，无接口依赖，可与 T1-T3 并行）
```

- **串行链 T1→T2→T3**：T2 的 worker 依赖 T1 的 mux 接口（line/chunk/session_idle_hint 签名）；T3 的 REPL 依赖 T2 的 router 接口（submit/SubmitOutcome）。共享核心接口契约，不可并行（并行会互相等待接口定型，串行反而总耗时低）。
- **T4 可并行**：trace.py 与前三者零接口耦合（仅被 loop 间接调用，loop 在 T3 才接触）。
- **Git worktree 分组建议**：T1-T3 单分支串行提交（同链原子 commit）；T4 若并行走独立 worktree `run-background-sessions-trace`，合并点在 T3 完成后（汇合跑全量 pytest）。
- **阻塞点**：T3 的「既有 26 用例迁移」是全 change 最大风险面（design §7）——若发现迁移爆炸即触发 reverse-sync 回 design 补测试迁移策略，属全局阻塞点。

## Wave 2（收尾）

- 无独立收尾 task：README/AGENTS.md 无命令清单章节需同步（proposal Impact 已论证）；T3 的手工冒烟即最终验收出口。
