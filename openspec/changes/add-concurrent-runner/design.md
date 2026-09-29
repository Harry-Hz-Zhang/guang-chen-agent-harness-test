# Design — ConcurrentRunner：线程池批量执行多会话 ReAct 循环

## 1. 变更全景

```
src/harness/runner.py              [ADD]  SessionJob / JobResult / ConcurrentRunner（≤ 130 行）
src/harness/config.py              [MOD]  max_concurrent_sessions: int = 4 + env 覆盖注册
src/harness/__main__.py            [MOD]  --concurrent 参数、spec 解析、互斥校验、run_concurrent 渲染
src/harness/__init__.py            [MOD]  导出 ConcurrentRunner（如现有导出惯例需要）
tests/test_runner.py               [ADD]  TestRunner（10 用例，含线程安全 FakeLLM 辅助）
tests/test_cli.py                  [MOD]  新增 TestConcurrentCli（4 用例）
README.md / CODEGRAPH.md / AGENTS.md [MOD] 文档同步
```

不改：loop / parser / session / memory / trace / renderer / tools / prompts（零侵入）。

## 2. src/harness/runner.py

### 2.1 数据结构

```python
@dataclass(frozen=True)
class SessionJob:
    """一个并发执行单元：目标会话 id 与一次用户输入。"""
    session_id: str
    user_input: str


@dataclass
class JobResult:
    """单个并发任务的执行结果（LoopResult 平铺 + 失败信息）。"""
    session_id: str
    answer: str            # 失败时为空串
    rounds: int
    tool_call_count: int
    truncated: bool
    error: str | None      # 成功为 None；失败为异常可读文本（含 LLMError）
```

### 2.2 ConcurrentRunner

```python
class ConcurrentRunner:
    """以线程池并发执行多个会话的 ReAct 循环（一个 agent 一个线程）。"""

    def __init__(self, loop: Any, config: RuntimeConfig) -> None:
        """注入共享的 ReactLoop 实例与运行配置。"""

    def run_batch(self, jobs: list[SessionJob]) -> list[JobResult]:
        """校验后并发执行全部任务，按提交顺序返回结果。"""
        self._validate(jobs)
        workers = min(len(jobs), self._config.max_concurrent_sessions)
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="harness-session") as pool:
            return list(pool.map(self._execute, jobs))

    def _execute(self, job: SessionJob) -> JobResult:
        """执行单任务：任何异常结构化为 error 字段，不上抛、不倒批。"""
        try:
            result = self._loop.run(job.user_input, job.session_id)
        except Exception as exc:  # LLMError 及一切意外失败
            return JobResult(job.session_id, "", 0, 0, False, str(exc))
        return JobResult(job.session_id, result.answer, result.rounds,
                         result.tool_call_count, result.truncated, None)
```

要点：

* **共享一个 ReactLoop**：run 的全部状态是参数/局部变量（carried、rounds 等均局部），
  实例无并发可变状态；LLMClient / Registry / TraceCollector / SessionStore /
  RuntimeState 全部随之共享（提案 §2 事实清单）。

* **`pool.map`** **保序**：结果顺序 = 提交顺序，渲染无需二次排序。

* **`CURRENT_SESSION_ID`** **无需 runner 干预**：`loop.run` 自带绑定/复位，线程上下文
  天然隔离（add-todo-tool 已固化）。

* **on\_event 恒为 None**：并发模式不流式（多线程写 stdout 交错乱码）。

### 2.3 校验（\_validate，显式拒绝、ValueError 中文消息）

| 输入情形                  | 行为                                  |
| --------------------- | ----------------------------------- |
| jobs 为空列表             | ValueError：任务列表不能为空                 |
| session\_id strip 后为空 | ValueError：第 N 个任务 session\_id 不能为空 |
| user\_input strip 后为空 | ValueError：会话 X 的输入不能为空             |
| session\_id 重复        | ValueError：会话 X 在批次内重复（同一会话不支持并发执行） |

重复拒绝的依据：`SessionStore._prepare_append` 先读后写无锁，同一 session 并发写
会产生 ordinal 竞态与交错行（store docstring 既有约束的进程内延伸）。

## 3. 配置（config.py）

* 字段：`max_concurrent_sessions: int = 4`（docstring 补充含义：并发批次的线程池上限）。

* `_HARNESS_ENV_NUMERIC_FIELDS` 追加 `("max_concurrent_sessions", int)` →
  env `HARNESS_MAX_CONCURRENT_SESSIONS` 可覆盖，非法值回退默认（既有机制）。

## 4. CLI 接线（__main__.py）

### 4.1 参数与互斥

```python
parser.add_argument("--concurrent", nargs="+", metavar="SESSION:输入",
                    help="并发执行多个会话任务后退出（例：--concurrent s1:查天气 s2:写周报）")
```

* `--concurrent` 与 `--session` 同时给出：输出用法提示并返回退出码 2
  （校验放在 LLMClient 构造**之前**，缺 API key 也能得到明确报错）。

### 4.2 spec 解析（模块级函数 `_parse_concurrent_specs`）

```
"s1:查天气记待办"     → ("s1", "查天气记待办")
"s1:输入含:冒号"      → ("s1", "输入含:冒号")     # 只按首个冒号切
"s1:" / ":输入" / "s1" → ValueError（session/输入为空、缺分隔符）
```

session\_id 内不含冒号（hex 生成）；strip 两侧空白。格式错误统一 ValueError，
main 捕获后输出 `--concurrent 参数格式错误：<消息>` 并返回 2。

### 4.3 run\_concurrent（渲染层，注入 writer 可测）

```python
def run_concurrent(runner, jobs, writer) -> None:
    """执行并发批次并按提交顺序渲染每个会话结果与汇总行。"""
```

输出形如（整批完成后一次性打印，无流式）：

```
── 会话 s1（2 轮决策，1 次工具调用）──
<答案正文>
── 会话 s2（出错）──
出错了：<error 文本>
并发批次完成：2 个会话（1 成功 / 1 失败）
```

* 成功头部带轮次与工具调用数；truncated 结果照常打印答案（答案本身即截断提示）。

* 汇总行按 error 是否为 None 统计成败。

* main 装配：复用现有全部组件（sessions/memory/builder/state/registry/trace/
  compressor/middlewares/loop），`ConcurrentRunner(loop, config)` 一样吃下；
  summarizer 照常 start/stop（它本就是既有 daemon 线程，idle\_seconds 会跳过
  正在活跃写入的会话）。

## 5. 并发正确性论证（为什么共享是安全的）

| 共享物                  | 论证                                                                               |
| -------------------- | -------------------------------------------------------------------------------- |
| loop 实例              | 无实例可变状态；每次 run 的会话归属由参数显式传递                                                      |
| CURRENT\_SESSION\_ID | ContextVar 线程隔离 + run 入口绑定/出口复位（既有测试 testSessionContextBoundDuringToolExecution） |
| session 文件           | 批内 session\_id 唯一 → 各写各文件；O\_APPEND 单行写                                          |
| todos（RuntimeState）  | 键控 + 既有锁；键即 session\_id，批次内无同键竞争                                                 |
| trace                | span\_id/trace\_id 随机唯一键各线程自持；CPython GIL 下 dict 单键操作原子；写盘锁已存在                   |
| LLM 客户端              | openai SDK 线程安全；现状 summarizer 已并发复用                                              |
| memory               | 批内零写入（loop 不写 memory），仅 summarizer 单写者                                           |

## 6. 测试设计（RED 先行）

### 6.1 tests/test\_runner.py（新建，10 用例）

辅助：`ThreadSafeFakeLLM`——invoke/stream 以 `threading.Lock` 保护 calls 记录与
responses 出队（共享单实例被多线程并发调用的测试替身）；另配 `_active` 计数器 +
`_max_active` 峰值记录（invoke 内 sleep 0.05s 放大重叠窗口）。

| 用例                                       | 断言                                                                                |
| ---------------------------------------- | --------------------------------------------------------------------------------- |
| `testBatchRunsAllJobsInOrder`            | 双任务各自答案 → 返回按提交顺序、answer/rounds 各归各                                               |
| `testBatchSessionsActuallyConcurrent`    | 双任务（脚本各 1 次答案）→ FakeLLM 峰值并发 ≥ 2（串行必然失败）                                          |
| `testBatchRejectsEmptyJobs`              | `[]` → ValueError match "不能为空"                                                    |
| `testBatchRejectsBlankSessionId`         | session\_id="  " → ValueError match "session\_id"                                 |
| `testBatchRejectsBlankInput`             | user\_input="" → ValueError match "输入"                                            |
| `testBatchRejectsDuplicateSessions`      | 同 session\_id 两任务 → ValueError match "重复"                                         |
| `testLlmErrorCapturedPerJob`             | 脚本：job1 抛 LLMError、job2 正常 → job1.error 非空且 answer 空、job2.answer 正常、run\_batch 不抛 |
| `testBatchSessionFilesIsolated`          | 双任务（各含一次工具调用）→ 两 session 文件各自 user/assistant/tool 序列完整、无交叉内容                      |
| `testTodosIsolatedAcrossConcurrentBatch` | 双任务脚本 write\_todos →答案 → RuntimeState 两会话 todos 各归各（端到端并发隔离）                      |
| `testMaxWorkersRespectsConfig`           | config.max\_concurrent\_sessions=2 + 6 任务 → FakeLLM 峰值并发 ≤ 2                      |

fixture 复用 test\_loop 的装配思路：真实 SessionStore/ContextBuilder/Registry（注册
write\_todos）+ ThreadSafeFakeLLM 脚本按任务构造（共享单实例、脚本队列按提交顺序
全局排布，锁保证出队原子）。

### 6.2 tests/test\_cli.py（新增 TestConcurrentCli，4 用例）

| 用例                                   | 断言                                                                                            |
| ------------------------------------ | --------------------------------------------------------------------------------------------- |
| `testParseConcurrentSpecs`           | `"s1:查天气"` → ("s1","查天气")；`"s1:含:冒号"` 保冒号；`"s1"` / `"s1:"` / `":x"` → ValueError              |
| `testRunConcurrentRendersPerSession` | FakeRunner（返回固定 JobResult×2）→ 输出含两会话头、答案、汇总行"2 成功"                                            |
| `testRunConcurrentRendersError`      | 一成一败 → 失败段含"出错了"与错误文本、汇总"1 成功 / 1 失败"                                                         |
| `testConcurrentExcludesSession`      | `main(["--concurrent","s1:hi","--session","s1"], lines=[], writer)` → 返回 2、输出含互斥提示、未触达 LLM 构造 |

### 6.3 既有测试

零改动（runner 为纯增量；config 加字段不破坏既有构造断言）。

## 7. 设计决策记录

| #  | 决策                                     | 依据                                                                        |
| -- | -------------------------------------- | ------------------------------------------------------------------------- |
| D1 | 线程池（ThreadPoolExecutor）承载并发            | 全栈同步；asyncio 需重写 llm/loop；ContextVar 线程隔离已被 testConcurrentRunsIsolated 固化 |
| D2 | 共享单 loop/LLMClient/Registry 等全部组件      | 实例无并发可变状态（提案 §2 清单）；连接复用、装配一次                                             |
| D3 | 批内 session\_id 唯一性校验，不做同 session 并发    | SessionStore ordinal 先读后写无锁（既有声明）；同 session 并发写无合理语义                      |
| D4 | 单任务任何异常 → JobResult.error，不上抛          | 对齐 REPL 可恢复哲学与 D23 结构化回传契约；单点失败不倒批                                        |
| D5 | 并发模式不流式、批后按序渲染                         | 多线程写 stdout 交错乱码；保序渲染确定可读                                                 |
| D6 | max\_concurrent\_sessions 默认 4、env 可覆盖 | 演示够用、防打爆；既有 HARNESS\_ 覆盖机制零成本复用                                           |
| D7 | TraceCollector 不加锁                     | 随机唯一键各线程自持，CPython GIL 下无同键竞争；写盘锁已存在                                      |
| D8 | `pool.map` 保序返回                        | 结果序 = 提交序，渲染零排序逻辑                                                         |
| D9 | 互斥/格式校验先于 LLMClient 构造                 | 缺 API key 时也能得到精准的参数错误提示（main 可测性）                                        |

