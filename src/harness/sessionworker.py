"""会话工人与会话路由器：每会话一个常驻 worker 线程 + 提交入口与并发护栏。

SessionWorker 是会话专属的「工人」：一个 daemon 线程 + 一个信箱
（queue.Queue），终生死循环逐条消费本会话输入——同会话串行由「单
worker 逐条取」天然保证。SessionRouter 是「路由器」：会话 → worker 的
注册表、submit 提交入口（毫秒级返回）、并发上限护栏与死亡复活保险。
runner 由外部注入（__main__ 用 partial 绑定 _run_turn），本模块不感知
loop/sessions 等上层依赖。
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from harness.config import RuntimeConfig
from harness.llm import LLMError
from harness.outputmux import OutputMux

WorkerRunner = Callable[[str, str, Callable[[str], None], Callable[[str], None]], None]
# runner(text, session_id, writer, raw_writer)：跑完一整轮对话（含流式）。

logger = logging.getLogger(__name__)

_LLM_ERROR_PREFIX = "出错了："
_FAILURE_PREFIX = "该会话处理失败："


@dataclass(frozen=True)
class SubmitOutcome:
    """submit 的同步回执：这条消息是入队了，还是被护栏拒了。"""

    accepted: bool
    reason: str | None = None


class SessionWorker:
    """会话专属工人：一个线程 + 一个信箱，终生死循环消费本会话输入。"""

    def __init__(
        self,
        session_id: str,
        runner: WorkerRunner,
        mux: OutputMux,
    ) -> None:
        """绑定会话 id、注入 runner 与输出复用器，初始化信箱与状态。"""
        self.session_id = session_id
        self._runner = runner
        self._mux = mux
        self._inbox: queue.Queue[str | None] = queue.Queue()
        self._state_lock = threading.Lock()
        self._running = False
        self._thread = threading.Thread(
            target=self._mainloop,
            name=f"harness-session-{session_id}",
            daemon=True,
        )

    @property
    def thread_name(self) -> str:
        """worker 线程名（harness-session-<sid>，daemon 断言锚点）。"""
        return self._thread.name

    @property
    def busy(self) -> bool:
        """是否忙碌：队列非空或正在跑一单。"""
        with self._state_lock:
            running = self._running
        return running or not self._inbox.empty()

    @property
    def alive(self) -> bool:
        """worker 线程是否存活（诊断与护栏判定用）。"""
        return self._thread.is_alive()

    def start(self) -> None:
        """启动 worker 线程（幂等：重复调用无效果）。"""
        if not self._thread.is_alive() and not self._thread.ident:
            self._thread.start()

    def submit(self, text: str) -> None:
        """把一条用户输入投进信箱（FIFO，立即返回不阻塞）。

        空串不入队（CLI 层会拦，此处再拦一次防假入内部调用）。
        """
        if not text:
            return
        self._inbox.put(text)

    def close(self) -> None:
        """投递 None sentinel：worker 排空在途消息后退出。

        可重复调用（额外 sentinel 无害，随对象 GC 丢弃）；仅供测试与受控收尾。
        """
        self._inbox.put(None)

    def join(self, timeout: float | None = None) -> None:
        """等待 worker 线程退出（仅供测试与受控收尾）。"""
        self._thread.join(timeout=timeout)

    def _mainloop(self) -> None:
        """主循环：逐条取信箱，跑 runner，异常兜底不死线程。"""
        while True:
            text = self._inbox.get()
            if text is None:
                break
            with self._state_lock:
                self._running = True
            try:
                self._run_turn(text)
            except LLMError as exc:
                self._mux.line(f"{_LLM_ERROR_PREFIX}{exc}", session_id=self.session_id)
            except Exception as exc:  # 兜底：worker 线程不死（结构化留痕）
                self._mux.line(
                    f"{_FAILURE_PREFIX}{exc.__class__.__name__}: {exc}",
                    session_id=self.session_id,
                )
                logger.warning(
                    "session %s 回合处理异常（已回传用户）",
                    self.session_id,
                    exc_info=True,
                )
            except BaseException as exc:  # 极端路径：提示后照死，复活保险兜底
                self._mux.line(
                    f"{_FAILURE_PREFIX}{exc.__class__.__name__}: {exc}",
                    session_id=self.session_id,
                )
                logger.warning(
                    "session %s 回合遭遇 BaseException，线程即将退出（复活保险兜底）",
                    self.session_id,
                    exc_info=True,
                )
                raise
            finally:
                with self._state_lock:
                    self._running = False
                self._mux.session_idle_hint(self.session_id)

    def _run_turn(self, text: str) -> None:
        """以本会话身份跑一整轮（writer/raw_writer 绑定 mux 与会话 id）。"""
        sid = self.session_id
        self._runner(
            text,
            sid,
            lambda t: self._mux.line(t, session_id=sid),
            lambda p: self._mux.chunk(p, session_id=sid),
        )


class SessionRouter:
    """会话 → worker 的注册表 + 提交入口 + 并发护栏。"""

    def __init__(
        self,
        runner: WorkerRunner,
        config: RuntimeConfig,
        mux: OutputMux,
    ) -> None:
        """注入 runner 材料与配置，初始化 worker 注册表。"""
        self._runner = runner
        self._config = config
        self._mux = mux
        self._workers: dict[str, SessionWorker] = {}
        self._registry_lock = threading.RLock()

    def submit(self, text: str, session_id: str) -> SubmitOutcome:
        """提交一条输入：建/复用/复活 worker 后入队，毫秒级同步返回。

        锁序说明：registry_lock 内调用的 worker 三方法（alive/close/submit）
        均为 _state_lock-free（put 永不阻塞、is_alive 不取锁），无锁序反转
        风险；若未来 worker 方法引入 state_lock 获取，须回访此段。
        """
        if not text:
            return SubmitOutcome(accepted=False, reason="空白输入不入队")
        with self._registry_lock:
            # 顺手清扫死亡残骸：死而未被替换的 worker 不应继续占用并发名额
            for sid in [sid for sid, w in self._workers.items() if not w.alive]:
                dead = self._workers.pop(sid)
                dead.close()
                logger.info(
                    "session %s worker 已死亡，弃置旧信箱（残余消息随队列丢弃）", sid
                )
            worker = self._workers.get(session_id)
            if worker is None:
                if len(self._workers) >= self._config.max_concurrent_sessions:
                    return SubmitOutcome(
                        accepted=False,
                        reason=(
                            f"并发已达上限（最多同时 "
                            f"{self._config.max_concurrent_sessions} 个会话在跑），"
                            f"请稍候或 /switch 到已有会话"
                        ),
                    )
                worker = SessionWorker(
                    session_id=session_id, runner=self._runner, mux=self._mux
                )
                self._workers[session_id] = worker
                worker.start()
            worker.submit(text)
            return SubmitOutcome(accepted=True)

    def switch(self, session_id: str) -> None:
        """切换前台：仅改 mux 的前台指针，不打扰任何 worker。"""
        self._mux.set_foreground(session_id)

    def mux_line(self) -> Callable[[str], None]:
        """取 mux 的整行输出入口（session_id=None 恒前台，命令提示用）。

        run_repl 用它输出命令提示，保证全部终端输出都过同一把 mux 锁。
        """
        return self._mux.line

    def active_worker_count(self) -> int:
        """当前注册表中的 worker 数（含已建未亡）。"""
        with self._registry_lock:
            return len(self._workers)

    def busy_sessions(self) -> list[str]:
        """返回「忙碌」会话 id 列表（在跑一单或队列非空；/sessions 展示用）。"""
        with self._registry_lock:
            workers = list(self._workers.values())
        return [w.session_id for w in workers if w.busy]

    def get_worker(self, session_id: str) -> SessionWorker | None:
        """按会话 id 取 worker（无则 None；测试与诊断用）。"""
        with self._registry_lock:
            return self._workers.get(session_id)

    def close_all(self) -> None:
        """向全部 worker 投 sentinel 并等待退出（测试收尾用）。"""
        with self._registry_lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.close()
        for worker in workers:
            worker.join(timeout=2)
        for worker in workers:
            if worker.alive:
                logger.warning(
                    "session %s worker 收尾超时未退出（daemon 悬挂至进程结束）",
                    worker.session_id,
                )
