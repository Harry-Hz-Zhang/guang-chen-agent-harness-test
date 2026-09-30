# Design — run-background-sessions

> 依据：proposal.md、specs/background-sessions/spec.md、根目录 last-exploration-handoff.md（D1-D15、C1-C3 已拍板）。
> 本文是给「审核架构」的人看的；人话版思路见 handoff Part 1。

## 0. 一句话架构

**主线程只读输入分拣，每个会话一个常驻 daemon worker 线程死循环消费自己的输入队列；输出全部过一把全局锁，后台会话输出带 `[xxxx]` 胸牌；`/exit` 不等任何人直接硬退。** 对标 Codex `submission_loop`（`codex-rs/core/src/session/handlers.rs:424-448`）的「会话循环 + 信箱」模型，用 Python 标准库 `threading` + `queue` 落地，不引入 asyncio。

## 1. 模块划分（文件结构）

| 文件 | 职责 | 关键导出 |
| --- | --- | --- |
| `src/harness/sessionworker.py`（新增） | 会话工人与路由器 | `class SessionWorker`、`class SessionRouter`、`@dataclass SubmitOutcome` |
| `src/harness/outputmux.py`（新增） | 输出复用器：全局出锁 + 前台判定 + 胸牌前缀 | `class OutputMux`、`@dataclass OutputLine` |
| `src/harness/trace.py`（加固） | 内存登记表加锁 + 回合清理 | `TraceCollector.end_trace(...)` 扩展（向后兼容） |
| `src/harness/__main__.py`（改造） | 主线程分拣循环、装配、硬退 | `run_repl` 签名不变或微调（见 §7） |
| `tests/test_sessionworker.py`（新增）、`tests/test_cli.py`（扩展） | 测试 | 见 §8 |

### 为什么拆两个新模块而不是塞进 `__main__.py`

- `__main__.py` 现有 255 行已含命令分拣与装配，再塞进两个线程类会突破单文件可读预算（项目内 `loop.py`/`runner.py` 均为「一个 concern 一个文件」惯例）；
- worker/路由与输出复用是**可独立单测的 concern**（假 loop 测路由语义、多线程测锁），塞进 CLI 文件会让测试必须经 `run_repl` 间接驱动；
- 命名对齐现有仓内先例：`runner.py`（批量并发）→ `sessionworker.py`（常驻并发），语义对仗。

## 2. 关键数据结构与接口

### 2.1 SessionWorker（每会话一个）

```python
@dataclass(frozen=True)
class SubmitOutcome:
    """submit 的同步回执：让主线程知道这条消息是入队了、还是被护栏拒了。"""

    accepted: bool  # True=已入队（含已建 worker）
    reason: str | None  # 拒因：「并发上限」等；accepted=True 时为 None


class SessionWorker:
    """会话专属工人：一个线程 + 一个信箱，终生死循环消费本会话输入。"""

    def __init__(
        self,
        session_id: str,
        runner: WorkerRunner,
        mux: OutputMux,
    ) -> None: ...
    def start(self) -> None: ...
    def submit(self, text: str) -> None: ...
    def close(self) -> None: ...  # 投 None sentinel：安全收尾（测试 teardown）
    @property
    def busy(self) -> bool: ...
    @property
    def alive(self) -> bool: ...
    @property
    def thread_name(
        self,
    ) -> str: ...  # f"harness-session-{session_id}"（daemon 断言用）


WorkerRunner = Callable[[str, str, Writer, Writer], None]
# 实现侧把「新建 StreamRenderer→loop.run→尾部输出/LLMError 捕获」整段下沉为
# __main__.py 里的一个模块级函数 _run_turn(text, session_id, writer, raw_writer，
# 外加 partial 绑定的 loop/config —— 模块级函数拿不到 run_repl 局部量，
# 由 __main__ 用 functools.partial 绑参后作为 runner 注入 SessionRouter 构造。
# 【2026-09-30 reverse-sync 裁定】runner 一律经 SessionRouter 构造注入，
# router 只做「runner 材料 + worker 注册表 + 护栏」，不在内部自造闭包 runner ——
# 消解原 §2.1 注释（_run_turn 注入）与 §2.2 签名（无 runner 参数）的矛盾。


WorkerRunner = Callable[[str, str, Writer, Writer], None]
# 实现侧把「新建 StreamRenderer→loop.run→尾部输出/LLMError 捕获」整段下沉为
# __main__.py 里的一个模块级函数 _run_turn(text, session_id, writer, raw_writer)
# （现状 __main__.py:155-171 的整段搬家，逻辑零改动，只是换了执行线程）。
```

- 信箱：`queue.Queue[str]`（无界、FIFO）——同会话串行由「单 worker 从队列逐条取」天然保证；
- 线程：`threading.Thread(target=self._mainloop, name=f"harness-session-{session_id}", daemon=True)`；命名模板抄 summarizer（`summarizer.py:24`）；
- 主循环骨架：

```python
def _mainloop(self) -> None:
    while True:
        text = self._inbox.get()  # 阻塞等下一单
        try:
            self._runner(text, self.session_id, ...)  # 跑完整整轮（含流式）
        except LLMError as exc:
            mux.line(f"出错了：{exc}", session_id)  # 与现状文案对齐
        except Exception as exc:  # 兜底：线程不死
            mux.line(f"该会话处理失败：{exc.__class__.__name__}: {exc}", session_id)
        finally:
            self._mux.session_idle_hint(self.session_id)  # 「[a1b2] 已完成」
```

- **busy/alive 语义**：`busy = 队列非空 or 正在跑`（给 `/sessions` 展示与护栏判定用）；`alive = 线程 is_alive`（诊断用，护栏判定以「worker 存在且 alive」为准）。

### 2.2 SessionRouter（会话路由器）

```python
class SessionRouter:
    """会话 → worker 的注册表 + 提交入口 + 并发护栏。"""

    def __init__(
        self,
        runner: WorkerRunner,
        config: RuntimeConfig,
        mux: OutputMux,
    ) -> None: ...
    def submit(self, text: str, session_id: str) -> SubmitOutcome: ...
    def switch(self, session_id: str) -> None: ...  # 仅改 mux 的前台指针
    def active_worker_count(self) -> int: ...
    def shutdown_nowait(self) -> None: ...  # 硬退：空操作（C2）


# 【2026-09-30 reverse-sync 裁定】构造签名改为收 runner（不收 loop/sessions）：
# runner 的装配（renderer 构造 + loop.run + 尾段）属 __main__ 职责（partial 绑定），
# router 不感知 loop/sessions —— 解耦后 T2 测试直接注入假 runner，T3 注入 partial(_run_turn)。
# 另：原 §2.1 SessionWorker 构造参数 lock_shared 语义未定义且与 router RLock 类型冲突，
# 裁定删除——worker 自持私有锁护 busy/running 状态，router 的 RLock 只护 worker 注册表 dict，
# 两锁无嵌套获取（router 锁内不调 worker 方法），无锁序风险。
```

- 内部 `dict[session_id, SessionWorker]` + 一把 `threading.RLock`（读 worker 映射的只有主线程，但 `/sessions` 等路径也读，一把锁换无脑安全）；
- **护栏判定要点**：`submit` 时若目标会话无 worker 且 `len(workers) >= max_concurrent_sessions` → `SubmitOutcome(False, "并发上限")`，同步返回（主线程据此打印护栏提示）。已有活 worker 的会话（含忙）不受上限约束——上限管的是「同时在跑的会话数」，不是「历史会话数」；
- **worker 死亡复活**：Cache 里的 worker 若 `alive=False`（异常打穿兜底外的极端路径），`submit` 时重建。兜底 `except Exception` 之后理论上不会死，但复活逻辑是廉价的保险。

### 2.3 OutputMux（输出复用器）

```python
@dataclass
class OutputLine:
    text: str
    session_id: str | None  # None=无会话归属（系统提示），用前台样式输出


class OutputMux:
    """全部终端出写的唯一关卡：一把锁 + 前台/后台判定 + 胸牌。"""

    def __init__(
        self, writer: Writer, raw_writer: Writer, background_prefix_len: int = 4
    ) -> None: ...
    def set_foreground(self, session_id: str | None) -> None: ...
    def line(self, text: str, session_id: str | None = None) -> void: ...
    def chunk(self, piece: str, session_id: str) -> None: ...
    def session_idle_hint(self, sid: str) -> None: ...
```

- **锁的范围**：一个 `threading.Lock` 同时护「写 stdout」与「前台指针」；`chunk`（流式分片）持锁写、**不持锁分批判断**（渲染器逐事件调 `chunk`，每次调用即取即放，避免长持锁阻塞其他会话）；
- **前缀规则**（C1/C3 已拍板）：`session_id == 前台指针` → 原样输出；否则行首加 `[a1b2] `（含 idle hint 行）。**流式正文分片同样带前缀**——分片无换行边界，实现按「该会话首片落屏前先打一次前缀、遇换行重置」的行同步逻辑（详见 §5.3 竞态 W3）；
- 前台指针在 `/switch` `/new` `/exit` 时由主线程 `set_foreground` 更新，新会话首条消息会把前台判定取到手前先入队（不追改已入队的消息归属——极小窗口语义见 §5.3 W5）。

### 2.4 `__main__.py` 改造后的数据流

```
主线程                          worker 线程(A)              worker 线程(B)
──────                          ──────────────              ──────────────
readline ──是命令──▶ 当场处理
readline ──普通消息──▶ router.submit(text, cur_sid)
                              inbox.get() ─▶ runner(text)   inbox.get() ─▶ runner(text)
                              │  _run_turn:                 │
                              │   新建 StreamRenderer        │  （各自实例，禁共享）
                              │   loop.run(on_event=mux.chunk 包装)
                              │   尾段: answer/finalize      │
                              └─▶ mux (全局锁+胸牌) ◀────────┘
```

### 2.5 trace.py 加固（最小手术）

- `TraceCollector` 现状两个内存 dict（`_spans`、`_trace_sessions`，`trace.py:154-161`）：加一把 `threading.Lock`，`start_trace`/`start_llm_span`/`end_llm_span`/`start_tool_span`/`end_tool_span`/`export` 的字典读写全部入锁；
- `end_trace` 新增「 从 `_spans`/`_trace_sessions` 移除该 trace 条目」；`ReactLoop._run` 的 `finally` 侧调用点不变（`loop.py` 不改——由 trace 模块内部在 `end_trace` 时顺带清理，或 loop 已有的 end 调用已覆盖。**此处为 loop.py 零改动的验证点**：核实 loop 是否已在回合终局调用 end_trace；若无，则在 middleware/`__main__` 收尾处由 runner 补一行调用。design 倾向前者——若实现时发现 loop 未调用 end_trace，触发 reverse-sync：回到本节补「loop.py 增加一行 end_trace 调用」的修订）。
- 落盘路径（`JsonlExporter`，已有内部锁，`trace.py:88`)不动。
- **2026-09-29 design review 已核实**：loop.py 现无 end_trace/回合终局清理调用（全仓 grep 0 hit），本节原「倾向 loop 已有 end 调用」的假设已被证伪——T4 按 reverse-sync 预案执行：在 log.md 记录后给 loop.py 回合终局补 end_trace 调用（≤5 行）。

## 3. 并发安全论证（逐条对应 explorer 报告 A 表）

| 共享组件 | 结论 | 论证要点（证据已核实） |
| --- | --- | --- |
| `ReactLoop`（单实例共享） | ✅ 零改造 | `run` 链路全部状态为方法局部变量，实例无可变属性（`loop.py:38-58`）；ConcurrentRunner 同构用法 + 并发测试背书。每次 run 入口 `CURRENT_SESSION_ID.set`（`loop.py:77-79`），线程天然各自绑定 |
| `SessionStore` | ✅ 零改造 | 按会话分文件（`store.py:20-24`）；唯一禁区「同会话并发写」由「每会话单 worker 」满足——worker 串行消费 = 同会话写入天然串行。跨会话 append 不同文件无共享句柄。读写并发（summarizer 读 + worker 写）半行自愈（`store.py:174-199`） |
| `LLMClient` | ✅ 零改造 | 底层 openai/httpx httpclient 线程安全官方保证；实例无可变状态（`llm.py:127-132`）；summarizer 已多线程共用 |
| `RuntimeState`（todos） | ✅ 零改造 | `ContextVar` 线程隔离 + 内部锁（`state.py:29-41`）+ 按会话键控；并发测试已覆盖（`test_runner.py:318-353`） |
| `ContextBuilder/ Compressor/ Middleware` | ✅ 零改造 | 实例均为纯依赖注入，无跨请求可变状态（`builder.py:73-75`、`compressor.py:35-40/244-247`）；`LoopState` 本来就是每请求新建 |
| `TraceCollector` | ⚠️ 本案加固 | `_spans`/_trace_sessions 无锁 + 只增不减 → 加锁 + 回合清理（§2.5）；`JsonlExporter` 已有锁不动 |
| `StreamRenderer` | ❌ 不共享即可 | 实例状态 `in_reasoning/streamed_any`（`renderer.py:25`）→ 每会话每请求在 worker 侧新建（§2.1 的 runner 下沉方案，现状每输入新建动作整体搬家） |
| `MemoryStore` | ✅ 零改造（前提） | 写者仍只有 summarizer 单线程；本案不给 REPL 前台/worker 新增任何 memory 写路径 |
| `sys.stdout` | ⚠️ 本案治理 | 现状多写者无锁 → OutputMux 全局锁统一关卡（§2.3） |

## 4. 被否决的备选方案

### 备选 ①：asyncio 全异步改造 ❌

- **内容**：loop/llm/工具全部 async 化，主循环 `asyncio.gather` 并行会话；
- **否决原因**：现有 11 个源文件全为同步代码，openai SDK 同步客户端在用（`llm.py:127`），改造面 = 全仓库重写，与「最小实现」直接冲突；AGENTS.md 无异步要求；测试基建（test_runner 的 ThreadSafeFakeLLM）也要重做。用户已拍板「多增加 100 行不够」档位讨论中的最小实现路线。

### 备选 ②：ThreadPoolExecutor 复用 ConcurrentRunner 模式 ❌

- **内容**：每次输入临时向线程池提交一个 task，不维护常驻 worker；
- **否决原因**：**无法保证同会话串行**——两次 submit 之间调度器可能并发执行同会话两条输入，直接违反 `SessionStore` 同会话禁并发写约束（`store.py:20-24`）；要补串行就得每会话加锁/队列，绕一圈回到常驻 worker 结构，且失去「信箱缓冲在跑期间的后续输入」能力（池模式下第二条输入要么丢要么自旋等待）。ConcurrentRunner 自身也是靠「批内去重 session_id」回避了这个问题（`runner.py:96-101`），但它是摄一次批模型、不能常驻。

### 备选 ③：插件式实现 reader 线程 + 单 executor ❌

- **内容**：主线程专职 stdin 读取，另设一个 dispatch 线程管路由，业务在 executor；
- **否决原因**：比方案 ② 多一层线程，dispatch 与 router 的同步问题反而引入新竞态；对本案的「1 主线 + N worker」模型没有增加任何能力，纯粹层级膨胀。

### 备选 ④：输出按会话分目录/分文件缓冲、切换时回放 ❌

- **内容**（应对 C1）：后台会话输出先写入内存 buffer，用户切回时回放；
- **否决原因**：用户已拍板实时混排（C1）；buffer 回放是静默落盘备选（未选）的变体，且回放需要二次 UI 状态管理，违反最小实现。

> 备选 ② 的「每会话串行」命题由常驻 worker 优雅解决（单线程串行消费），这是本案选它而不是线程池的根本原因；④ 直接被用户拍板否决。

## 5. 竞态与边界分析

### W1：新 worker 创建与限流判定的 TOCTOU
- **窗口**：主线程 `submit`（读 count → 决定建 worker）是主线程唯二写入路径之一，且 submit 只在主线程发生（普通消息、`/new`）；`switch` 不建 worker。
- **结论**：submit 串行执行于主线程 → 无并发 TOCTOU。router 内部锁仍保留（防御 `/sessions` 读路径与未来扩展）。

### W2：worker 兜底异常后队列堆积
- **场景**：某会话连发 10 条、前 3 条全失败 → 用户看到 3 条失败提示 + 7 条继续逐条处理；
- **设计取舍**：不做「失败后清空队列」（用户可能正想重试）；spec 已定「继续消费」语义。

### W3：流式分片的行同步（前缀与换行）
- **问题**：`chunk` 收到的分片可能是行中段（无换行），两个会话的分片交错会在行内互插；
- **方案**：OutputMux 按 `session_id` 维护「上一个分片是否已打前缀、行尾是否已换行」的游标。同一会话连续分片之间**不释放行所有权**——具体实现：mux 内部为每个活跃后台会话维护 pending 行缓冲，遇到 `\n` 或该会话轮次结束（`finalize`/idle hint）才整行落屏并归还锁。**代价**：后台会话流式失去「逐字」效果，改为「逐行」出现——前台会话仍逐字。这是 C1「实时混排」下最小复杂度的正确取舍（一行内混两会话的字是 worse alternative）。
- **已在 spec 中体现**：Scenario「两会话同时流式输出 THEN 每一行完整归属单一会话」。

### W4：前台判定与正在跑的回合
- **场景**：A 前台跑着、用户 /switch B 后 A 继续吐流式 → A 的后续 chunk 需要带胸前牌；mux 的 `set_foreground(B)` 立即生效，A 的下一个 chunk 立刻识别自已是后台；
- **换行游标兼容**：切换瞬间 A 若有半行缓冲，在其落屏时打前缀（半行归属于 A 的会话，以落屏时前台状态判定前缀有无——半行落屏时 A 已是后台 → 带牌。**微小语义**：切走前极短窗口内 A 已落的部分行无牌，语义可接受，写入 spec 的「切换提示行」之后再落屏的行才开始带牌）。

### W5：入队消息的「前台归属」快照
- **场景**：用户 /switch B 后**立刻**向 B 输入消息，B worker 起跑等 LLM 首片期间用户又 /switch A → B 的输出落屏时前台是 A，B 的行带 B 胸牌（正确的，落屏归属按输出发生时前台判定——语义即「当前不是我在说话，就带牌」）。
- **结论**：归属实时判定，不快照。简单、可预期。

### W6：daemon 线程 + 硬退的半行损坏
- **场景**（C2 拍板结果）：/exit 时 A 的 worker 正在写会话 JSONL → 进程死、半行留盘；
- **现状兼容**：读取器 `_read_valid_records` 跳过非法行并 warning（`store.py:174-199`），下次 append 继续（ordinal 由存活记录 max 求得）——**已验证自愈**。spec 已定「无新增要求」。

### W7：summarizer 与 worker 的文件并发
- 场景：summarizer 扫 session_ids/last_modified/load_records（只读）与 worker append 并发——半行自愈，同 W6；`mark_summarized` 只写 memory 目录（不与 worker 冲突）；**后台会话 mtime 持续刷新 → 永不判闲置**（`summarizer.py:132-138`），不会对在跑会话做总结。

### W8：trace 清理与在跑 trace
- `end_trace` 清理由回合终局触发（同一线程）→ 无跨线程清理竞态；加锁后 dict 操作原子化，多会话并发登记不串键（span_id 唯一性 `trace.py:170`）。

## 6. 命名表（explore D 类委托 propose 的完整决定）

| 实体 | 名称 | 理由 |
| --- | --- | --- |
| 模块 | `sessionworker.py` | 对仗 `runner.py`（一次性批量）vs session worker（常驻）；「会话工人」人话直译 |
| 工人类 | `SessionWorker` | 对齐 `ConcurrentRunner` 的 PascalCase 名词风格 |
| 路由类 | `SessionRouter` | 职责即路由：submit + switch 指针 |
| 提交回执 | `SubmitOutcome` | dataclass，语义明确优于裸 bool |
| 输出模块/类 | `outputmux.py` / `OutputMux` | mux = 复用器，业界通用词；避免「Printer/Writer」与现有 `Writer` 类型撞名 |
| runner 函数（`__main__.py`） | `_run_turn` | 「跑一轮」原语义，私有（仅 worker 回调用） |
| worker 线程名 | `harness-session-<sid>` | 抄 `harness-memory-summarizer` 模板（`summarizer.py:24`） |
| 输出方法 | `line` / `chunk` / `session_idle_hint` | 行=整行输出；分片=流式；提示=「已完成」 |
| 前缀格式 | `[<sid 前 4 位>] ` | C3 拍板 |
| 护栏提示文案 | 「会话并发已达上限（N），请先等某个会话完成或 /switch 到已活跃会话」 | proposal 验收 7 |
| 失败提示文案 | 「`[a1b2] 出错了：……`」（LLMError）/「`[a1b2] 该会话处理失败：……`」（其他） | 对齐现状 LLMError 文案 + spec |
| 完成提示文案 | 「`[a1b2] 已完成`」 | spec idle hint |

## 7. `run_repl` 接口影响（reverse-sync 预案）

现有签名 `run_repl(loop, sessions, config, lines, writer, session_id=None, raw_writer=None)`（`__main__.py:67-77`）：
- **计划**：骨架保留（命令分拣、提示文案不变），内部普通输入分支替换为 `router.submit`；renderer/run/尾部输出段整体搬入 `_run_turn`；
- **测试兼容**：`test_cli.py` 现有 26 用例中普通输入路径的断言（「loop.run 被以某 session_id 调用」）需改为「router.submit 入队 + worker 线程最终以该 session_id 调 loop.run」。**这是 apply 阶段最大的既有测试改造面**，tasks.md 已为之单列 Task 4；
- **退出路径二分（implementer 必读，design review 补充）**：「flush 语义」仅适用于 lines 自然耗尽（EOF / 测试注入行耗尽）——run_repl 返回前等待所有已入队消息跑完，供测试断言最终一致性；`/exit` 是硬退路径：**不 flush、不 join、不等待**（C2）。两条路径互斥独立，禁止实现成「返回前无条件等待所有 worker」。
- **idle hint 前台抑制（语义裁定）**：前台会话的 session_idle_hint **不输出任何行**（用户正盯着它跑完，与现状一致——现状本无提示行）；「已完成」提示仅用于后台会话。tasks T1 第 4 条 RED 即此语义。
- **命令语义补充（/new 与 /sessions）**：`/new` = 新建会话并立即 set_foreground(新 id)，旧会话若在跑转为后台（后续输出带胸牌，半行语义同 W4）；`/sessions` 输出语法保持现状不变——busy 标志仅作内部护栏判定材料，不进展示文案（避免既有 test_cli 断言回归）。
- 若实现时发现改造会连带 26 用例大面积重写 → 反向同步：止步，回 proposal 补充「测试迁移策略」小节后再继续（reverse-sync 流程）。

## 8. 测试策略

| 测试文件 | 模式 | 覆盖 |
| --- | --- | --- driver |
| `tests/test_sessionworker.py`（新增） | 线程安全假模型（`ThreadSafeFakeLLM` 移植） + 真实 SessionStore(tmp_path) + 假 loop | worker 生命周期 / 同会话串行 / 异会话并行 / 异常兜底 / Ctrl 护栏 / idle hint |
| `tests/test_cli.py`（扩展） | MagicMock loop（现状模式） | 命令路由 / switch 不打断 / submit 入队语义 / 硬退 |
| `tests/test_trace.py`（扩展） | 现状模式 | end_trace 清理 / 加锁并发 |

- 全部零网络、零真实 API；`uv run pytest` 全量绿作为每个 task 的 DoD 基线；
- 并发断言参照 `runner` 测试的 `max_active >= 2` 峰值法（`test_runner.py:215-217`）——验证「真并行」而非「完成顺序」。

## 9. 实现顺序与依赖（tasks.md 的骨架预告）

```
Task 1 输出复用器（无依赖，纯函数级单测）
Task 2 worker+路由（依赖 T1 的 mux 注入）
Task 3 REPL 接线 + 既有测试迁移（依赖 T2）
Task 4 trace 加固（独立，可并行 T1-T3）
```

推荐串行 T1→T2→T3（共享接口契约：mux 的 line/chunk 签名被 2/3 依赖）；T4 独立可并行。Task 内 TDD（先 RED 再 GREEN，每 task 原子 commit，format: `feat: <中文描述>`）。
