"""ConcurrentRunner —— 单进程内以线程池并发执行多个会话的 ReAct 循环。

一个 agent 一个线程：共享同一 ReactLoop（实例无可变状态）与其全部
依赖（LLM 客户端、工具注册表、会话存储、trace 收集器），会话归属由
loop.run 入口绑定的 CURRENT_SESSION_ID 在各线程上下文内天然隔离。
批次内 session_id 必须唯一——SessionStore 的 ordinal 为先读后写且
无锁，同一会话并发写不受支持；单任务失败结构化为 JobResult.error，
不中断批次其余任务。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from harness.config import RuntimeConfig


@dataclass(frozen=True)
class SessionJob:
    """一个并发执行单元：目标会话 id 与一次用户输入。"""

    session_id: str
    user_input: str


@dataclass
class JobResult:
    """单个并发任务的执行结果（LoopResult 平铺 + 失败信息）。"""

    session_id: str
    answer: str
    rounds: int
    tool_call_count: int
    truncated: bool
    error: str | None


class ConcurrentRunner:
    """以线程池并发执行多个会话的 ReAct 循环（一个 agent 一个线程）。"""

    def __init__(self, loop: Any, config: RuntimeConfig) -> None:
        """注入共享的 ReactLoop 实例与运行配置。"""
        self._loop = loop
        self._config = config

    def run_batch(self, jobs: list[SessionJob]) -> list[JobResult]:
        """校验后并发执行全部任务，按提交顺序返回各任务结果。

        线程数取任务数与 max_concurrent_sessions 的较小值（至少 1，
        防御配置为 0/负数）；pool.map 保序，结果顺序即提交顺序。
        """
        self._validate(jobs)
        workers = max(1, min(len(jobs), self._config.max_concurrent_sessions))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="harness-session"
        ) as pool:
            return list(pool.map(self._execute, jobs))

    def _execute(self, job: SessionJob) -> JobResult:
        """执行单任务：任何异常（含 LLMError）结构化为 error 字段，不上抛、不倒批。"""
        try:
            result = self._loop.run(job.user_input, job.session_id)
        except Exception as exc:
            return JobResult(
                session_id=job.session_id,
                answer="",
                rounds=0,
                tool_call_count=0,
                truncated=False,
                error=str(exc),
            )
        return JobResult(
            session_id=job.session_id,
            answer=result.answer,
            rounds=result.rounds,
            tool_call_count=result.tool_call_count,
            truncated=result.truncated,
            error=None,
        )

    def _validate(self, jobs: list[SessionJob]) -> None:
        """校验批次：非空、session_id 与输入非空白、session_id 不重复。"""
        if not jobs:
            raise ValueError("并发任务列表不能为空")
        seen: set[str] = set()
        for index, job in enumerate(jobs, start=1):
            if not job.session_id.strip():
                raise ValueError(f"第 {index} 个任务的 session_id 不能为空")
            if not job.user_input.strip():
                raise ValueError(f"会话 {job.session_id} 的输入不能为空")
            if job.session_id in seen:
                raise ValueError(
                    f"会话 {job.session_id} 在批次内重复：同一会话不支持并发执行"
                )
            seen.add(job.session_id)
