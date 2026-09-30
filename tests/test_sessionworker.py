"""SessionWorker / SessionRouter 的单元测试。

覆盖：后台线程执行 / 同会话串行 / 异会话并行 / submit 即时返回 /
非 LLM 异常兜底 / LLMError 文案分流 / 并发上限护栏 / 忙碌老会话豁免 /
同会话复用 worker / idle hint 顺序 / daemon 线程 / 死亡复活 / sentinel
退出。方法名沿用 openspec/changes/run-background-sessions/tasks.md 的
RED 原名（camelCase，AGENTS.md 命名规范例外条款）。

并发测试纪律：同步点一律用 threading.Event 握手或完成等待，禁止裸 sleep；
等待完成用 done/barrier Event 断言，不依赖固定时距 hold（防 Windows 调度抖动）。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import pytest

from harness.config import RuntimeConfig
from harness.llm import LLMError
from harness.outputmux import OutputMux
from harness.sessionworker import SessionRouter, SessionWorker, WorkerRunner


class RecordingStub:
    """记录调用入参与线程 id 的假 runner 基座（各用例按需变形）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        text: str,
        session_id: str,
        writer: Callable[[str], None],
        raw_writer: Callable[[str], None],
    ) -> None:
        with self._lock:
            self.calls.append((text, session_id, threading.get_ident()))

    @property
    def call_count(self) -> int:
        """已记录的调用次数。"""
        return len(self.calls)


def _make_mux() -> tuple[OutputMux, list[str]]:
    """构造真 OutputMux + 记录 writer（后台化后断言可带前缀）。"""
    calls: list[str] = []

    def writer(text: str) -> None:
        calls.append(text)

    def raw_writer(piece: str) -> None:
        calls.append(piece)

    return OutputMux(writer=writer, raw_writer=raw_writer), calls


@pytest.fixture
def teardown_workers() -> Callable[[SessionRouter | SessionWorker], None]:
    """用例收尾：向 router/worker 投 None sentinel 并 join，防线程跨用例泄漏。"""
    targets: list[SessionRouter | SessionWorker] = []

    def register(target: SessionRouter | SessionWorker) -> None:
        targets.append(target)

    yield register

    leaked: list[str] = []
    for target in targets:
        if isinstance(target, SessionRouter):
            target.close_all()
        else:
            target.close()
            target.join(timeout=2)
            if target.alive:
                leaked.append(target.thread_name)
    assert not leaked, f"teardown 后仍有线程未退出: {leaked}"


class TestSessionWorker:
    def shouldRunTurnInBackgroundThread(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """提交的输入由 worker 线程执行 runner，而非主线程。"""
        stub = RecordingStub()
        done = threading.Event()

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            stub(text, session_id, writer, raw_writer)
            done.set()

        mux, _ = _make_mux()
        worker = SessionWorker(session_id="a1b2c3d4", runner=runner, mux=mux)
        teardown_workers(worker)
        worker.start()
        worker.submit("问题")
        assert done.wait(timeout=5), "runner 应在 5s 内完成"
        assert stub.call_count == 1
        text, sid, tid = stub.calls[0]
        assert text == "问题"
        assert sid == "a1b2c3d4"
        assert tid != threading.get_ident()

    def shouldSerializeInputsWithinSameSession(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """同会话多条输入严格串行：时间区间无重叠、完成顺序即提交顺序。"""
        intervals: list[tuple[int, float, float]] = []
        started = threading.Event()
        release = threading.Event()
        both_done = threading.Event()
        iv_lock = threading.Lock()

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            start = time.monotonic()
            if text == "1":
                started.set()
                release.wait(timeout=5)
            end = time.monotonic()
            with iv_lock:
                intervals.append((int(text), start, end))
                if len(intervals) == 2:
                    both_done.set()

        mux, _ = _make_mux()
        worker = SessionWorker(session_id="a1b2c3d4", runner=runner, mux=mux)
        teardown_workers(worker)
        worker.start()
        worker.submit("1")
        assert started.wait(timeout=5)  # 第一单已进入执行区（put 同步返回即已入队）
        worker.submit("2")
        release.set()
        assert both_done.wait(timeout=5), "两单应在 5s 内全部完成"
        assert len(intervals) == 2
        first, second = sorted(intervals, key=lambda t: t[0])
        assert (first[0], second[0]) == (1, 2), "完成顺序 = 提交顺序"
        assert second[1] >= first[2], "同会话两单时间区间不应重叠"

    def shouldSurviveNonLLMException(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """runner 抛非 LLM 异常：输出结构化提示，线程不死继续消费。"""
        attempt = {"n": 0}
        all_done = threading.Event()

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            attempt["n"] += 1
            if attempt["n"] <= 2:
                raise RuntimeError("disk full")
            if attempt["n"] == 3:
                all_done.set()

        mux, calls = _make_mux()
        mux.set_foreground("ffffffff")  # 后台化，让提示带胸牌可断言
        worker = SessionWorker(session_id="a1b2c3d4", runner=runner, mux=mux)
        teardown_workers(worker)
        worker.start()
        worker.submit("1")
        worker.submit("2")
        worker.submit("3")
        assert all_done.wait(timeout=5), "3 条输入都应被消费"
        assert attempt["n"] == 3
        failures = [c for c in calls if "该会话处理失败" in c]
        assert len(failures) == 2
        assert all("RuntimeError" in c for c in failures)
        assert worker.alive

    def shouldCatchLLMErrorWithExistingWording(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """runner 抛 LLMError：沿用「出错了：」文案，且会话继续可对话。"""
        calls_n = {"n": 0}
        second_done = threading.Event()

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            calls_n["n"] += 1
            if calls_n["n"] == 1:
                raise LLMError("timeout")
            if calls_n["n"] == 2:
                second_done.set()

        mux, calls = _make_mux()
        mux.set_foreground("ffffffff")
        worker = SessionWorker(session_id="a1b2c3d4", runner=runner, mux=mux)
        teardown_workers(worker)
        worker.start()
        worker.submit("hi")
        worker.submit("again")  # 模型失败后会话继续可对话
        assert second_done.wait(timeout=5), "第二条输入应被继续处理"
        assert any("出错了：timeout" in c for c in calls)
        assert not any("处理失败" in c for c in calls)

    def shouldKeepDaemonThread(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """worker 线程是 daemon：/exit 硬退不挂进程（C2 前提）。"""
        mux, _ = _make_mux()
        worker = SessionWorker(session_id="a1b2c3d4", runner=RecordingStub(), mux=mux)
        teardown_workers(worker)
        worker.start()
        thread = next(t for t in threading.enumerate() if t.name == worker.thread_name)
        assert thread.daemon is True

    def shouldExitOnNoneSentinel(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """close() 投递 None sentinel：先排空在途消息再退出，close 幂等。"""
        stub = RecordingStub()
        mux, _ = _make_mux()
        worker = SessionWorker(session_id="a1b2c3d4", runner=stub, mux=mux)
        teardown_workers(worker)
        worker.start()
        worker.submit("in-flight")
        worker.close()
        worker.close()  # 幂等
        worker.join(timeout=5)
        # sentinel 排在 in-flight 之后：runner 恰消费 1 条后退出
        assert stub.call_count == 1
        assert not worker.alive


class TestSessionRouter:
    def shouldRunDifferentSessionsInParallel(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """异会话两单真并行：双进位闸门保证峰值活跃数 >= 2（零时间依赖）。"""
        lock = threading.Lock()
        active = {"n": 0, "max": 0}
        release = threading.Event()
        b_entered = threading.Event()

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            with lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            if session_id == "a1b2c3d4":
                b_entered.wait(timeout=5)  # A 等 B 也进入 hold 区间才放手
            else:
                b_entered.set()
            with lock:
                active["n"] -= 1

        mux, _ = _make_mux()
        router = SessionRouter(runner=runner, config=RuntimeConfig(), mux=mux)
        teardown_workers(router)
        assert router.submit("a", session_id="a1b2c3d4").accepted
        assert router.submit("b", session_id="ff00ff00ff").accepted
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with lock:
                if active["max"] >= 2:
                    break
            time.sleep(0.01)
        release.set()
        assert active["max"] >= 2, "两会话 runner 应存在并发区间"

    def shouldReturnImmediatelyOnSubmit(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """runner 阻塞不返回时，submit 本身毫秒级返回（主线程不阻塞）。"""
        blocked = threading.Event()
        release = threading.Event()

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            blocked.set()
            release.wait(timeout=10)

        mux, _ = _make_mux()
        router = SessionRouter(runner=runner, config=RuntimeConfig(), mux=mux)
        teardown_workers(router)
        started = time.monotonic()
        outcome = router.submit("hi", session_id="a1b2c3d4")
        elapsed = time.monotonic() - started
        assert outcome.accepted
        assert elapsed < 0.5, f"submit 耗时 {elapsed:.3f}s 应即时返回"
        assert blocked.wait(timeout=5)  # worker 侧确实已在跑
        release.set()

    def shouldEnforceMaxConcurrentSessions(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """活跃 worker 达上限后，新会话 submit 被同步拒收。"""
        release = threading.Event()
        entered: list[threading.Event] = [threading.Event() for _ in range(3)]

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            index = int(text) - 1
            entered[index].set()
            release.wait(timeout=10)

        config = RuntimeConfig(max_concurrent_sessions=2)
        mux, _ = _make_mux()
        calls: list[str] = []

        call_lock = threading.Lock()

        def tracking_runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            with call_lock:
                calls.append(session_id)
            runner(text, session_id, writer, raw_writer)

        router = SessionRouter(runner=tracking_runner, config=config, mux=mux)
        teardown_workers(router)
        assert router.submit("1", session_id="s1aaaaaa").accepted
        assert entered[0].wait(timeout=5)
        assert router.submit("2", session_id="s2bbbbbb").accepted
        assert entered[1].wait(timeout=5)
        outcome = router.submit("3", session_id="s3cccccc")
        assert not outcome.accepted
        assert outcome.reason is not None and "并发" in outcome.reason
        assert "s3cccccc" not in calls, "被拒会话的 runner 应 0 次调用"
        release.set()

    def shouldNotCountBusyExistingSessionAgainstLimit(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """忙碌老会话再提交不受上限约束：上限管会话数，不管消息数。"""
        release = threading.Event()
        entered = threading.Event()

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            entered.set()
            release.wait(timeout=10)

        config = RuntimeConfig(max_concurrent_sessions=1)
        mux, _ = _make_mux()
        router = SessionRouter(runner=runner, config=config, mux=mux)
        teardown_workers(router)
        assert router.submit("a", session_id="s1aaaaaa").accepted
        assert entered.wait(timeout=5)
        outcome = router.submit("again", session_id="s1aaaaaa")
        assert outcome.accepted
        release.set()

    def shouldReuseWorkerForSameSession(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """同会话多次 submit 只复用一个 worker（不重复建工人）。"""
        entered = threading.Event()
        release = threading.Event()

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            entered.set()
            release.wait(timeout=10)

        mux, _ = _make_mux()
        router = SessionRouter(runner=runner, config=RuntimeConfig(), mux=mux)
        teardown_workers(router)
        assert router.submit("1", session_id="s1aaaaaa").accepted
        assert entered.wait(timeout=5), "第一单应已进入执行区"
        assert router.submit("2", session_id="s1aaaaaa").accepted
        assert router.active_worker_count() == 1
        release.set()

    def shouldEmitIdleHintAfterTurnEnds(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """回合自然结束输出 idle hint，且顺序在 runner 之后。"""
        events: list[str] = []
        ev_lock = threading.Lock()
        runner_started = threading.Event()
        done = threading.Event()

        def runner(
            text: str,
            session_id: str,
            writer: Callable[[str], None],
            raw_writer: Callable[[str], None],
        ) -> None:
            with ev_lock:
                events.append("runner")
            runner_started.set()
            done.wait(timeout=1)  # 确保hint 顺序断言窗口内 runner 待在执行区

        mux, calls = _make_mux()
        mux.set_foreground("ffffffff")  # 后台化使 idle hint 真正落屏
        router = SessionRouter(runner=runner, config=RuntimeConfig(), mux=mux)
        teardown_workers(router)
        assert router.submit("q", session_id="a1b2c3d4").accepted
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any("已完成" in c for c in calls):
            time.sleep(0.01)
        done.set()  # 释放 runner，避免 teardown 前线程悬留在 wait 上
        hint_rows = [c for c in calls if "已完成" in c]
        assert hint_rows, "回合结束后应收到 idle hint 行"
        assert any("[a1b2] 已完成" in c for c in hint_rows), (
            "hint 属于该会话且带前 4 位胸牌"
        )
        assert len(hint_rows) == 1
        assert events == ["runner"], "runner 在 hint 之前恰好执行 1 次"

    def shouldReviveDeadWorkerOnSubmit(
        self, teardown_workers: Callable[[SessionRouter | SessionWorker], None]
    ) -> None:
        """已死 worker 在下次 submit 时复活重建，计数不膨胀。"""
        stub = RecordingStub()
        mux, _ = _make_mux()
        router = SessionRouter(runner=stub, config=RuntimeConfig(), mux=mux)
        teardown_workers(router)
        assert router.submit("first", session_id="s1aaaaaa").accepted
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and stub.call_count < 1:
            time.sleep(0.01)
        assert stub.call_count == 1
        worker = router.get_worker("s1aaaaaa")
        assert worker is not None
        worker.close()
        worker.join(timeout=5)
        assert not worker.alive
        count_before = router.active_worker_count()
        assert router.submit("second", session_id="s1aaaaaa").accepted
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and stub.call_count < 2:
            time.sleep(0.01)
        assert stub.call_count == 2
        assert router.active_worker_count() == count_before
