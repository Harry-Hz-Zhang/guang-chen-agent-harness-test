# Log — run-background-sessions

## 2026-09-29 propose

| 事件 | 说明 |
| --- | --- |
| Skills Loaded | superpowers:brainstorming, vsdd-workflow-router, openspec-explore, vsdd-workflow-explore（explore 阶段）；vsdd-workflow-router, vsdd-workflow-reverse-sync, vsdd-workflow-design-review, openspec-propose, superpowers:writing-plans（propose 阶段） |
| Handoff 承接 | 根目录 last-exploration-handoff.md（2026-09-29 版）：D1-D15 节点全部 resolve，C1/C2/C3 由用户经交互问答拍板（实时混排 / 硬退 / [a1b2] 前缀），已回写 handoff |
| 复杂度判定 | 🔴 standard：新增 2 模块（sessionworker/outputmux）跨 CLI×运行时×trace，4 task 均 🟡/🔴 standard（router 路由 skill 防偷懒条款：新建 Mapper/Service 级组件最低 🟡，跨模块 🔴） |
| Change 脚手架 | `openspec new change run-background-sessions`（CLI v1.8.0）成功 |
| Artifacts | proposal.md / specs/background-sessions/spec.md / design.md / tasks.md 全部创建 |
| tasks 自检（机械扫描） | task 行 4/4 ✓；RED 方法名 token 38 个 ✓（每条含 #methodName）；mock/返回/抛关键词 17 行 ✓；每 task RED ≥6 条且覆盖 happy/终止/异常/边界/调用次数 5 类 ✓ |
| 任务依赖图 | T1→T2→T3 串行（接口契约链：mux→worker router→REPL），T4 独立可并行；T3 既有 26 用例迁移为全局阻塞点（design §7 预案：超限触发 reverse-sync） |
| C2 硬退的代价记录 | 用户选「立刻硬退」：实现更简（不 join daemon 线程）；在跑回答可能丢失、会话 JSONL 半行由 store.py:174-199 自愈（已核实） |
| Reverse Sync 预案 | design §2.5：若实现时发现 loop.py 未在回合终局调 end_trace，则记 reverse_sync 并补 ≤5 行调用（T4 已内置该条款） |
| 探索阶段遗留的可能性声明 | proposal 非目标最后一条：闲置「回锅提醒」D15 划出范围（无用户诉求，summary 记录可见性另行验证） |

## 2026-09-29 design review

| 事件 | 说明 |
| --- | --- |
| 审查方式 | design-reviewer 只读子代理审 artifacts（proposal/specs/design/tasks 一致性与可执行性，最多 2 轮） |
| 审查结果 | **PASS**（0 Critical / 3 Important / 若干 Minor，维持 🔴 standard）：四件一致性成立（验收 1-8 ↔ 六 Requirement ↔ RED 方法名一一映射）；tasks 满足 config.yaml 全部 rules；代码事实 15+ 处基本属实 |
| Important 修订（已当日完成） | ① T2 `shouldReviveDeadWorkerOnSubmit` 的「手动杀死线程」不可测（Python 无 kill 原语）→ 改写为「预置已自然死亡 stub worker（alive=False）」；② flush 语义与 /exit 硬退二义性 → design §7 补「退出路径二分」；③ T1 后台 finalize("\n") + idle hint 尾行组合 RED 缺失 → tasks 补 `shouldFlushFinalNewlineThenIdleHintAsTwoLines` |
| 副产物 Minor 落实 | idle hint 前台抑制（design §7 补裁定）；/new、/sessions 语义补充（design §7）；§2.5 已写入「loop 现无 end_trace 调用已核实，T4 按 reverse-sync 预案补 ≤5 行」；T2 DoD 补 None sentinel 退出 + fixture teardown 线程卫生 |
| 证伪点 | loop.py 无 end_trace 终局调用（全仓 grep 0 hit）——已被 T4 reverse-sync 预案捕获，无需改架构 |
| 结论 | design_review_passed: true，rounds=1，进入 apply |

## 2026-09-29 apply

| 事件 | 说明 |
| --- | --- |
| Skills Loaded | vsdd-workflow-apply, vsdd-workflow-router, vsdd-workflow-reverse-sync, vsdd-workflow-git-discipline, vsdd-workflow-implement-task, vsdd-workflow-review-implementation, superpowers:test-driven-development, superpowers:subagent-driven-development |
| 分支策略 | 用户确认「接受默认，改动随行」：git checkout -b feature/run-background-sessions（未提交改动随行，auto_commit=false 下无需清理） |
| base_commit | 2df4ea9f9acda7ba6c6ff4c4c7c13a443ed38dcf (main) |
| runtime 配置 | auto_commit: false（commit 由用户人工决定，工作流不碰 git 提交）；commit_message_guidance: "" |
| 执行模式 | serial（T1→T2→T3 串行，T4 独立可并行但建议串行保简单） |

## 2026-09-30 apply Task 1（outputmux）

| 事件 | 说明 |
| --- | --- |
| RED 证据 | `uv run pytest tests/test_outputmux.py -q` → `ModuleNotFoundError: No module named 'harness.outputmux'`，exit 1（先写测试后写实现，Iron Law 满足） |
| GREEN 证据 | 同命令 → 11 passed（11 条 RED 原名用例全部转绿）；全量回归 `uv run pytest -q` → 210+ 全绿 exit 0（72×3 行无 failed） |
| codegraph sync | 成功：Synced 4 changed files / Added 42 nodes，exit 0 |
| Reverse Sync ① | 冲突：tasks T1 RED `shouldEmitIdleHintForForegroundSession` 原描述「输出无 [ 前缀的已完成行」vs design §7 裁定「前台抑制」；处理：以 design 为事实源改 RED 描述，测试按前台零输出断言（tasks.md 已同步修订） |
| Reverse Sync ② | 冲突：implementer prompt 对 `shouldBufferBackgroundChunkUntilNewline` 误写「raw_writer 调用 == 1」vs tasks.md 原文三处（writer 通道）；处理：以 tasks.md 为事实源，断言 writer 恰 1 次收到 `[a1b2] 思考\n` |
| 基建适配 | pytest 默认 `python_functions=test*` 收集不到 `should*` 原名 → pyproject.toml 增补 `python_functions = ["test*", "should*"]`（test* 保持默认，零既有用例影响，全量回归验证） |
| 字节约定 | mux 传 writer 的整行自带尾部 `\n`；`set_foreground` 落实 design W4：新前台残留后台期半行切指针时先无牌落屏，防输出丢失 |
| 实现规杇 | src/harness/outputmux.py（~125 行：一把锁 + `_pending` 行缓冲表 + 胸牌前缀 + 异常护栏全捕）；tests/test_outputmux.py（11 用例，含双线程交替行所有权验证） |
### Review Evidence Task 1（双审，独立子代理）

| 线 | 结论 | 要点 |
| --- | --- | --- |
| spec 合规审查 | **PASS**（0 Critical / 2 Important / 6 Minor） | 11/11 RED 原名对上；DoD 四子项满足；spec 3 Scenario 全覆盖；锁/前缀/W3/前台抑制条款均落实。Important：①4 条用例断言强度未达 ASSERT「逐字符精确匹配」；②set_foreground 新前台半行 flush 行为无测试锁定 |
| 代码质量审查 | **PASS（有条件）**（0 Critical / I1-I5 / M1-M6） | 并发正确性专节：锁覆盖完整无 TOCTOU、无死锁路径、Barrier 用法正确。I1 后台空缓冲孤 tag 行；I2 异常用例注释失实且路径错；I3 join 超时无存活断言；I4 W4/多换行零覆盖；I5 set_foreground(None) 语义不对称 |

### Review Fixes Task 1（Important 全修 + Minor 择修）

| 修复 | 内容 |
| --- | --- |
| 断言精确化 | 4 条用例升级 `==` 精确匹配（你好\n / [a1b2] 你好\n / [a1b2] 已完成\n / 系统提示\n） |
| I1 | chunk 空缓冲闭合段跳过孤胸牌行（if buffer: emit），验证用例 shouldSkipEmptyLineAfterTrailingNewline |
| I2 | shouldSuffixExceptionOnGuard 补 set_foreground("ffffffff") 真走后台整行路径；计数收紧（raw 为 2——前台「思」+补位「\n」各直通一次，实测修正） |
| I3 | join 后补 `not t.is_alive()` 死锁检出断言 |
| I4 | 补 shouldFlushPendingUntaggedOnForegroundSwitch（W4）+ shouldSplitMultiNewlineChunkIntoLines 两用例 |
| I5 | set_foreground(None) 全量带牌 flush 后清指针；docstring 明确契约 |
| M3/M4/M6 | line() 去 assert 窄化改显式条件；收敛 `_flush_pending_locked(sid, tagged)` 消两处重复样板；RecordingWriter 注释改为「由 mux 锁串行化」 |

### Build Evidence Task 1

| 验证 | 结果 |
| --- | --- |
| `uv run pytest tests/test_outputmux.py -q` | 15 passed（review 修复后从 11 → 15），exit 0 |
| `uv run pytest -q` 全量 | 215 passed，exit 0，无回归 |
| `codegraph sync` | Synced / Done，exit 0 |

> Task 1 完成。auto_commit=false：不提交，按用户人工决定。

## 2026-09-30 apply Task 2 前置 Reverse Sync（pre-implementation 审查触发）

| 事件 | 说明 |
| --- | --- |
| 审查方式 | implement 前 Explore 只读核查（7 项契约可行性 + 风险清单） |
| Reverse Sync ③ | 冲突：design §2.1 注释「runner 下沉 __main__._run_turn 注入 worker」vs §2.2 router 构造签名收 loop/sessions 却无 runner 参数——两处无法同时成立；处理：裁定 runner 一律经 SessionRouter 构造注入（router 不感知 loop/sessions），_run_turn 由 __main__ 用 partial 绑参后注入，T2 测试直接注入假 runner |
| Reverse Sync ④ | 冲突：SessionWorker 构造参数 lock_shared 在 design 全文语义未定义，且与 router RLock 类型自相矛盾；处理：删除该参数——worker 自持私有锁护 busy/running，router RLock 只护注册表 dict，两锁无嵌套 |
| RED 补条 | 核查第 5 项发现 sentinel 退出仅 DoD 隐含、无显式用例 → tasks.md 补 `TestSessionWorker#shouldExitOnNoneSentinel`（第 13 条） |
| 接口补充 | SessionWorker 增 close()（投 None sentinel，幂等）与 thread_name 属性（daemon 断言锚点）；SessionRouter 构造签名定稿 (runner, config, mux) |

## 2026-09-30 apply Task 2（SessionWorker / SessionRouter）

### RED Evidence Task 2

| 用例 | 验证 |
| --- | --- |
| 13 条 RED 原名（tasks.md Task 2 段）+ 第 13 条补条 shouldExitOnNoneSentinel | 先写测试后写实现，12 条直接 RED（ImportError/ModuleNotFoundError: harness.sessionworker）；逐一转 GREEN |
| RED 快照证据 | `uv run pytest tests/test_sessionworker.py -q` → ModuleNotFoundError exit 1（提交前先行验证测试失败） |

### GREEN Evidence Task 2

| 用例数 | 结果 |
| --- | --- |
| 13/13 | 13 passed，exit 0（21.06s——review 修复后 1.08s，见下） |
| 全量回归 | `uv run pytest -q` → 226 passed，exit 0 |

### Review Evidence Task 2

双 review 独立只读子代理（spec 合规 + 代码质量），结论：

| 审查线 | 结论 | 计数 |
| --- | --- | --- |
| spec 合规 | **PASS** | 0 Critical / 1 Important / 8 Minor |
| 代码质量 | **PASS（有条件→修复后达成）** | 0 Critical / I1-I3+I4+I5 Important / M1-M10 Minor |

spec 合规审查确认：13/13 RED 原名实在且断言语义一致；三个 Requirement（串行/并行/生命周期护栏/异常兜底） Scenario 全覆盖；接口与 design §2.1/§2.2（含 reverse-sync ③④ 裁定）对齐；无越界改动（__main__.py 零感知 SessionRouter）。值得肯定：峰值法并行断言、event 握手防 flake、真 OutputMux 集成高于 RED 最低线。

代码质量审查确认：无 Critical；并发正确性专节论证 close_all 快照-放锁-再 join 无死锁、锁序无环（router RLock 与 worker _state_lock 无嵌套获取）、sentinel FIFO 保序、daemon 语义符合 C2 硬退。FAIL 项集中在测试侧（I3 join 误用 4 处空转 ~20s、I4 裸 sleep 违纪、I5 固定 hold flake 面），生产码仅小补丁（I1 BaseException 未拦、I2 复活静默丢队列）。

### Review Fixes Task 2（2026-09-30 落盘）

| 编号 | 修复 | 文件 |
| --- | --- | --- |
| quality I1 | _mainloop 增 `except BaseException` 分支：输出失败提示 + logger.warning(exc_info) 后 re-raise（保留极端死亡语义，复活保险兜底） | sessionworker.py |
| quality I2 | submit 复活路径清扫死亡残骸时 `logger.info("worker 已死亡,弃置旧信箱（残余消息随队列丢弃）")`（同时让 logger 有用途） | sessionworker.py |
| quality I3 | 4 处 `worker.join(timeout=5)` 误用为「等工作」→ runner 内置 done Event + `assert done.wait(timeout=5)`；shouldEmitIdleHint 保留唯一有意 1s 窗口 | test_sessionworker.py |
| quality I4 | 删除 :119 裸 `time.sleep(0.05)`（put 同步返回即入队，worker 被 release 挡住，sleep 无同步作用） | test_sessionworker.py |
| quality I5 | shouldRunDifferentSessionsInParallel 固定 0.2s hold → 双进位事件闸门（A 等待 b_entered 才放手，峰值 ≥2 由结构保证，零时间依赖） | test_sessionworker.py |
| spec I1 | **Reverse Sync ⑤ 备案**：shouldExitOnNoneSentinel 断言语义与 RED 原文不一致（原文预设 drop-queue「两条排队后 close → runner 0 次」；实现 close() 是 FIFO 追加 sentinel，客观不可达）。裁定：采用 FIFO 排空语义（「一条 in-flight + close → runner 恰 1 次后退出」），理由：①对测试 teardown 更安全（排空在途再退出，无半途丢失）；②与 queue.Queue 全局 FIFO 保序一致；③死 worker 残余 sentinel 随对象 GC 无泄漏。修订 tasks.md 第 13 条描述与本语义对齐 | log.md + tasks.md |
| spec Minor-2 | shouldSerializeInputsWithinSameSession 补 `(first[0], second[0]) == (1, 2)` 完成顺序显式断言 | test_sessionworker.py |
| spec Minor-5 | shouldCatchLLMErrorWithExistingWording 补第二条 submit 续跑断言（LLMError 后继续消费，spec Scenario 后半句） | test_sessionworker.py |
| spec Minor-7 | teardown fixture join 后补 `not target.alive` 泄漏检出断言 | test_sessionworker.py |
| quality M1 | 删除 `_LOCAL` 死代码占位 lambda | sessionworker.py |
| quality M3 | 并发上限判定前遍历清扫死亡 worker 陈尸占位（死而未复活不再假拒新会话） | sessionworker.py |
| quality M4 | close() docstring「幂等」→「可重复调用，额外 sentinel 无害（随 GC 丢弃）」 | sessionworker.py |
| quality M5 | submit 空串防御拒绝（空白输入不入队） | sessionworker.py |
| quality M6 | close_all join 超时后 logger.warning（daemon 悬挂至进程结束可追溯） | sessionworker.py |
| quality M7 | （本批以代码注释与 log 备案替代 design 回写）锁内调用的 worker 三方法（alive/close/submit）均为 _state_lock-free，无锁序反转风险；若未来 worker 方法引入 state_lock 获取须回访 | sessionworker.py + 本 log |
| quality M8 | 删除 hint_received 死 Event（建后无人 wait） | test_sessionworker.py |
| quality M9 | 保留 reason 含「并发」字面断言（当作文案契约钉） | 不改 |
| quality M2/M10 | busy TOCTOU 属双信号设计固有代价且零调用方，docstring 已注明 advisory；teardown 对死 worker 二次 close 无堆积危害——均维持现状 | 不改 |

### Build Evidence Task 2

| 验证 | 结果 |
| --- | --- |
| `uv run pytest tests/test_sessionworker.py -q --durations=13` | 13 passed，exit 0，**1.08s（修复前 21.06s，提速 ~95%）** |
| `uv run pytest -q` 全量 | 226 passed，exit 0，无回归 |
| `codegraph sync` | Synced 2 changed files / 77 nodes / Done，exit 0 |
| DoD 真装配（apply 前置核查阶段已验） | 3 会话（sess0001/0002/0003）× 60 条消息，真 SessionStore append_message + load_records 全数回收，无丢失无交错，ALL OK |

> Task 2 完成。auto_commit=false：不提交，按用户人工决定。T3（REPL 接线）前的「手动 REPL 冒烟」须在 T3 接线后才可执行，顺延至 T3 一并覆盖。

## 2026-09-30 apply Task 3（REPL 接线 + 既有用例迁移）

### RED Evidence Task 3

| 用例 | 验证 |
| --- | --- |
| 10 条 RED 原名（TestReplRouting，tasks.md Task 3 段 camelCase）+ 12 条既有用例迁移（router= 注入 + records 断言改造） | 先写测试后写实现；RED 快照 `uv run pytest tests/test_cli.py -q` → 15 FAILED（TypeError: run_repl() got an unexpected keyword argument 'router'），exit 1 |

### GREEN Evidence Task 3

| 用例数 | 结果 |
| --- | --- |
| 35/35（25 既有 + 10 新增） | `uv run pytest tests/test_cli.py -q` → 35 passed，exit 0 |
| 全量回归 | `uv run pytest` → 236 passed in 3.52s，exit 0 |
| GREEN 迭代注记 | 首轮 15 FAILED 根因：turn 完成断言用例沿旧例尾部带 `/exit` → 硬退路径不 flush → 断言 0==1。按 design §7 退出路径二分修正用例：断言 turn 最终完成的 18 处改 lines 自然耗尽（EOF→close_all flush），仅命令语义/硬退速度用例保留 `/exit` |

### Review Evidence Task 3

双 review 独立只读子代理（spec 合规 + 代码质量），结论：

| 审查线 | 结论 | 计数 |
| --- | --- | --- |
| spec 合规 | **PASS（附条件）** | 0 Critical / 2 Important / 7 Minor |
| 代码质量 | **PASS** | 0 Critical / 2 Important / 9 Minor |

spec 合规审查确认：10/10 RED 原名实在；主循环仅 router.submit + close_all，无主线程直调 loop.run；flush/硬退二分与 design §7 一致；/new 立即 set_foreground、/sessions、/switch 校验链三项 §7 落实；trace/summarizer 未触碰（无越界）。代码质量审查确认：partial 绑定签名与 WorkerRunner 完全一致；close_all 有界等待（2s×N）+ 超时留痕，flush 语义不会挂死；/new 语义照 §2.2 拍板（旧 worker 占名额是护栏口径「管在跑会话数」）；fixture 无泄漏。

### Review Fixes Task 3（2026-09-30 落盘）

| 编号 | 修复 | 文件 |
| --- | --- | --- |
| quality I1 | 命令提示双通道写：run_repl 内全部提示行改走 `mux.line()`（经 `router.mux_line()` 取得，session_id=None 恒前台）——全部终端输出收敛到同一把 mux 锁 | __main__.py + sessionworker.py（新增 mux_line()） |
| quality I2 | 私有锚点 `router._mux` + 死方法 switch 零调用：run_repl 三处前台切换统一改 `router.switch(sid)`，删 SLF001 穿透 | __main__.py |
| spec I1 | shouldExitImmediatelyWithoutJoiningWorkers 补「在跑 runner」前提：gate 拦住 s1 worker 后再 /exit，断言 0.1s + worker.alive（区分度成立） | test_cli.py |
| spec I2 | shouldSwitchForegroundWithoutTouchingWorkers 补同一前提 + managed calls 计数断言「命令输入不进 LLM」（entered 闸门防竞态） | test_cli.py |
| quality M1 | 删 SubmitOutcome 未用导入 | __main__.py |
| quality M2 | 删 _HARD_EXIT_CONFIRM 死常量 | __main__.py |
| quality M3 | docstring 错字「兕底」→「兜底」 | __main__.py |
| quality M4/M5 | main 内 output 重复赋值删除；装配四行与 run_repl 兜底分支重复 → 抽 `_build_router()` 共用（漂移风险消除） | __main__.py |
| quality M6 | 删 rejected_once/reject_lock 死变量 | test_cli.py |
| quality M7 | teardown except pass → logger.warning(exc_info) 留痕 | test_cli.py |
| quality M8 | run_repl/_run_turn 的 loop: Any → ReactLoop | __main__.py |
| 测试侧联动 | _mux_of 适配层 strip 换行（mux.line 输出带 \n，既有精确断言不漂移）；testCommandSwitchEmptySessionNoMessageCount 断言按 mux 通道实收形收紧 | test_cli.py |
| 备案不修 | quality M9（Writer 类型别名两处定义——__main__ 侧留本地别名向后兼容，后续 change 收敛）；spec Minor 若干（元断言弱、design「26 用例」计数口径）——已记录，不阻断 | — |

### Build Evidence Task 3

| 验证 | 结果 |
| --- | --- |
| `uv run pytest tests/test_cli.py -q`（review 修复后终态） | 35 passed，exit 0 |
| `uv run pytest` 全量 | 236 passed in 3.52s，exit 0，无回归 |
| DoD 三套件联验 | `uv run pytest tests/test_cli.py tests/test_sessionworker.py tests/test_outputmux.py -q` → 62 passed，exit 0 |
| `codegraph sync` ×2（GREEN 后 + review 修复后） | 4 files/190 nodes → 3 files/151 nodes，Done，exit 0 |
| `PYTHONPATH=src uv run python -m harness --help` | usage 输出正常，exit 0 |
| 手工冒烟（真 DeepSeek） | **用户裁决跳过**（无 key 环境不硬造；静态链路已由 test_cli 35 条 + 全量 236 背书，顺延不阻断） |

> Task 3 完成。auto_commit=false：不提交，按用户人工决定。

## 2026-09-30 apply Task 4 前置 Reverse Sync（⑥，pre-implementation 备案）

| 事件 | 说明 |
| --- | --- |
| 触发 | design §2.5 预案条款兑现：loop.py 现无 end_trace 调用（apply 前 grep 0 hit 已核实） |
| Reverse Sync ⑥ | 冲突：trace.py 新增 end_trace(trace_id) 但 loop.py 回合终局无调用点 → 登记表随回合数无限增长；处理：loop._run 补 try/finally 调 end_trace（净增 3 行 + docstring 2 行，卡 ≤5 行上限），正常答案/截断/异常三路径统一收尾 |
| 后续发现 | summarizer._extract 同样 start_trace 无收尾（design §2.5 盲区，Review I1 抓出）→ 同模式补 finally end_trace（reverse-sync ⑥ 延伸，随 Task 4 review 一并落盘） |

## 2026-09-30 apply Task 4（TraceCollector 锁与清理加固）

### RED Evidence Task 4

| 用例 | 验证 |
| --- | --- |
| 6 条 RED 原名（TestTraceCollector，tasks.md Task 4 段 camelCase） | 先写测试后写实现 |

### GREEN Evidence Task 4

| 用例数 | 结果 |
| --- | --- |
| 19→20（13 既有 + 6 RED + 1 review 补条） | `uv run pytest tests/test_trace.py` → 20 passed，exit 0 |
| 全量回归 | `uv run pytest` → 243 passed in ~4s，exit 0 |
| 实现注记 | 中途发现 start_trace 双重定义事故（第一个加锁版被第二个无锁版遮蔽）——按用户「校验+清死代码」指令修复：删重复定义；start_*_span 的 _trace_sessions 读取移入锁内（消除 end_trace 清理窗口竞态） |

### Review Evidence Task 4

单 review 综合只读子代理（spec 合规 + 代码质量 + 用户点名的死代码/冗余专审三合一），结论：

| 审查线 | 结论 |
| --- | --- |
| 综合 | **PASS（附条件）**：0 Critical / 4 Important / 6 Minor |

确认项：六处 dict 读写全入锁且 _emit（exporter 调用）在锁外（无死锁/无锁内 IO）；end_trace 与 end_*_span 并发收尾同一 span 无双重 emit；loop.py ≤5 行 + reverse-sync ⑥ 先备案后改码合规；并发用例零 flake 面（8×400 span 毫秒级、GIL 原子 append、死锁检出断言到位）。

### Review Fixes Task 4（2026-09-30 落盘，含用户点名的全局清冗余）

| 编号 | 修复 | 文件 |
| --- | --- | --- |
| I1 | summarizer._extract 补 finally end_trace（闲置提取路径登记表不再无界泄漏，对称 loop._run） | summarizer.py |
| I2 | 补 shouldSweepStaleSpansForTraceOnEndTrace（残留 span 全量清扫 + 幂等 None 断言，兑现 ASSERT「精确键集合」） | test_trace.py |
| I3 | 删 render_event 未用导入；测试导入源改 harness.renderer（消 CLI re-export 污染） | __main__.py + test_cli.py |
| I4 | 删 stream_writer 死参数及 3 行自我消费兜底（mux 接管后遗留签名） | __main__.py |
| M1 | 删 main 内 output 重复赋值（Task 3 fix M4 漏网第二处） | __main__.py |
| M2 | 「兕底」错字终清（_run_turn docstring）；顺带全仓「兑」错字清零 | __main__.py 等 |
| M3 | busy 零调用裁决：接入 /sessions「跑着中」标记（兑现 design §2.1 承诺而非删能力），testCommandSessions 扩忙碌态断言 | __main__.py + sessionworker.py（busy_sessions）+ test_cli.py |
| M4 | 删 OutputLine dataclass（生产零使用的纯造型类），测试去保型构造 | outputmux.py + test_outputmux.py |
| M5 | 删 sha256 冗余断言（bytes== 已充分）及 hashlib 导入 | test_trace.py |
| 死代码扫描 | 全仓 sweep：shutdown_nowait 死方法已删（sessionworker.py）；确认干净面——trace/sessionworker/state/runner/middleware/tools/loop 及各测试导入；「文件内同名方法」经核实均为跨类正常同名 | 全仓 |

### Build Evidence Task 4

| 验证 | 结果 |
| --- | --- |
| `uv run pytest tests/test_trace.py` | 20 passed，exit 0 |
| `uv run pytest` 全量（清冗余终态） | 243 passed in 3.98s，exit 0，无回归 |
| `codegraph sync` ×4（GREEN 后 + 各批清理后） | 最后一次 Synced 5 changed files / 197 nodes / Done，exit 0 |
| 最小验证（长跑 RSS） | 由 shouldNotGrowMemoryAcrossManyTurns（100 回合登记表回基线）等价背书，真 CLI 长跑随 Task 3 冒烟一并顺延（用户已裁决跳过） |

> Task 4 完成。全部 4 task 收口：state → 4-done。auto_commit=false：不提交，按用户人工决定。verify/archive 为下一阶段（另行启动）。

## 2026-09-30 apply Task 4 前置 Reverse Sync（design §2.5 预案兑现）

| 事件 | 说明 |
| --- | --- |
| 验证点核实 | loop.py `_run`（:83-）开头 `start_trace(session_id)` 产生 trace_id，但全函数无 end_trace/清理调用（grep end_trace 全仓 0 hit）——design §2.5 预设的「loop 未在回合终局清理」情况**成立**，且 start_trace 里 `_trace_sessions[trace_id] = session_id` 永不过期，再叠加并发 worker 后每回合泄漏一条映射 |
| Reverse Sync ⑥ | 依 design §2.5 既有预案条款（propose 阶段 design review 已裁定，无需回改 artifacts）：TraceCollector 新增 `end_trace(trace_id)` 方法（锁内从 `_trace_sessions` 与 `_spans` 清除该 trace 的全部条目）；loop.py `_run` 的 `finally` 补 `self._trace.end_trace(trace_id)` 调用，修改 ≤5 行 |
| 契约兼容 | end_trace 幂等（不存在的 trace_id 静默返回）；span 通常已在 end_*_span 中被 pop，残留（异常路径）由 end_trace 一并收尾 |

## 2026-10-01 追记（后续减法裁决）

| 事件 | 说明 |
| --- | --- |
| runner.py 移除 | 用户于 2026-10-01 死代码审计后拍板：`ConcurrentRunner`（runner.py）生产零引用（并发入口已由 SessionRouter 兑现），整模块连同 tests/test_runner.py 删除。proposal「不改动 ConcurrentRunner」条款就此作废，本 log 备注替代。 |
| state.py 移除 | 同批裁决：`RuntimeState` / `CURRENT_SESSION_ID`（state.py）为「只写不读」断头路（write_todos 写入后无生产消费方），连同绑定机制删除；write_todos 改为无状态「校验+渲染」工具，待办经会话文件历史（role=tool 消息）由 LLM 读取。 |