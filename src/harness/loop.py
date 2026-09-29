"""ReactLoop —— ReAct 主循环：LLM 决策 → 工具执行 → 结果回传 → 再决策。

一次 run 调用对应一条用户输入：start_trace 注入 trace_id 后把用户消息
落库，随后按 design 决策 1 的数据流逐轮执行——middleware.before_model
（压缩等会话级前置处理）→ ContextBuilder 重建上下文 → LLM 调用（含
trace span）→ assistant 消息落库（保留 reasoning_content 与 tool_calls）
→ parse_response 二分：最终答案即返回；工具调用批次则逐个校验、直接
执行、结果或结构化错误以 tool 消息落库后进入下一轮。达到 max_rounds
仍未收敛则返回截断提示。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from harness.config import RuntimeConfig
from harness.llm import AIMessage, StreamEvent, ToolCall, Usage, collect_stream
from harness.middleware import LoopState, Middleware
from harness.parser import FinalAnswer, parse_response, validate_arguments
from harness.state import CURRENT_SESSION_ID
from harness.tools.registry import ToolNotFoundError

_TRUNCATED_ANSWER = "已达单次请求最大轮次（{max_rounds}），本次请求就此终止。"


@dataclass
class LoopResult:
    """一次用户输入的循环执行结果。"""

    answer: str
    rounds: int
    tool_call_count: int
    truncated: bool


class ReactLoop:
    """ReAct 决策循环（依赖全部注入，自身不持有会话归属）。"""

    def __init__(
        self,
        llm: Any,
        registry: Any,
        sessions: Any,
        builder: Any,
        trace: Any,
        middlewares: list[Middleware],
        config: RuntimeConfig,
    ) -> None:
        """注入 LLM 客户端、工具注册表、会话存储、上下文组装器、追踪收集器、中间件链与配置。"""
        self._llm = llm
        self._registry = registry
        self._sessions = sessions
        self._builder = builder
        self._trace = trace
        self._middlewares = list(middlewares)
        self._config = config

    def run(
        self,
        user_input: str,
        session_id: str,
        on_event: Callable[[StreamEvent], None] | None = None,
    ) -> LoopResult:
        """执行一次用户输入的决策循环，返回最终答案与统计。

        当前输入只在首轮请求出现一次：首轮 build 时历史尚未包含该
        输入（由 build 追加到请求末尾），build 完成后立刻把 user 消息
        落库（LLM 调用前，实时持久化）；后续轮 build 传空串（历史已含
        当前输入，不再重复追加）。

        入口把 session_id 绑定到运行时上下文（ContextVar，协程/线程
        各自隔离）并在出口复位：多个 agent 在不同线程/协程同时执行
        时，有状态工具据此读到各自会话的数据。
        """
        token = CURRENT_SESSION_ID.set(session_id)
        try:
            return self._run(user_input, session_id, on_event)
        finally:
            CURRENT_SESSION_ID.reset(token)

    def _run(
        self,
        user_input: str,
        session_id: str,
        on_event: Callable[[StreamEvent], None] | None = None,
    ) -> LoopResult:
        """决策循环主体（run 已完成会话上下文绑定）。"""
        trace_id = self._trace.start_trace(session_id)
        tools_schema = self._registry.to_openai_tools()
        tools_param: list[dict[str, Any]] | None = tools_schema if tools_schema else None
        rounds = 0
        tool_call_count = 0
        carried: list[dict[str, Any]] = []
        for round_no in range(self._config.max_rounds):
            state = LoopState(
                session_id=session_id,
                round_no=round_no,
                messages=carried,
                user_input=user_input,
                trace_id=trace_id,
            )
            for middleware in self._middlewares:
                middleware.before_model(state)
            request = self._builder.build(
                session_id, user_input if round_no == 0 else ""
            )
            if round_no == 0:
                self._sessions.append_message(
                    session_id, {"role": "user", "content": user_input}
                )
            state.messages = request
            carried = request
            span_id = self._trace.start_llm_span(
                trace_id, self._config.model, request
            )
            message = self._call_llm(request, tools_param, on_event)
            rounds += 1
            self._sessions.append_message(
                session_id, self._assistant_record(message)
            )
            usage = message.usage if message.usage is not None else Usage(0, 0, 0)
            self._trace.end_llm_span(
                span_id, {"content": message.content}, usage, message.finish_reason
            )
            decision = parse_response(message)
            if isinstance(decision, FinalAnswer):
                for middleware in self._middlewares:
                    middleware.after_model(state)
                return LoopResult(
                    answer=decision.content,
                    rounds=rounds,
                    tool_call_count=tool_call_count,
                    truncated=False,
                )
            for call in decision.calls:
                tool_call_count += 1
                self._handle_tool_call(call, session_id, trace_id, span_id)
        return LoopResult(
            answer=_TRUNCATED_ANSWER.format(max_rounds=self._config.max_rounds),
            rounds=rounds,
            tool_call_count=tool_call_count,
            truncated=True,
        )

    def _call_llm(
        self,
        request: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_event: Callable[[StreamEvent], None] | None,
    ) -> AIMessage:
        """调用 LLM：无回调走 invoke；有回调走流式并逐事件转发后聚合。"""
        if on_event is None:
            return self._llm.invoke(request, tools)
        return collect_stream(self._tee(self._llm.stream(request, tools), on_event))

    def _tee(
        self,
        events: Iterator[StreamEvent],
        on_event: Callable[[StreamEvent], None],
    ) -> Iterator[StreamEvent]:
        """把流式事件逐个转发给回调后再向下传递（生成器）。"""
        for event in events:
            on_event(event)
            yield event

    def _assistant_record(self, message: AIMessage) -> dict[str, Any]:
        """把 AIMessage 转为落库的 assistant 消息（保留思考与工具调用结构）。"""
        record: dict[str, Any] = {"role": "assistant", "content": message.content}
        if message.reasoning_content is not None:
            record["reasoning_content"] = message.reasoning_content
        if message.tool_calls:
            record["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_raw,
                    },
                }
                for call in message.tool_calls
            ]
        return record

    def _handle_tool_call(
        self,
        call: ToolCall,
        session_id: str,
        trace_id: str,
        parent_span_id: str,
    ) -> None:
        """处理单个工具调用：查找 → 校验 → 执行 → 落库。

        三类失败（未注册 / 参数非法 / 执行异常）均以结构化错误
        JSON 回传 LLM，不静默吞（D23 契约）。工具自身抛出的
        TimeoutError 等一切异常统一归类 ToolExecutionError。
        """
        try:
            tool = self._registry.get(call.name)
        except ToolNotFoundError as exc:
            self._append_tool_message(
                session_id,
                call,
                result=None,
                error=self._error_payload(call, "ToolNotFoundError", str(exc)),
            )
            return
        errors = validate_arguments(call, tool.parameters)
        if errors:
            self._append_tool_message(
                session_id,
                call,
                result=None,
                error=self._error_payload(
                    call, "InvalidToolArguments", "；".join(errors)
                ),
            )
            return
        tool_span_id = self._trace.start_tool_span(
            trace_id, parent_span_id, call
        )
        result: str | None = None
        error: dict[str, Any] | None = None
        try:
            result = tool.execute(**(call.args or {}))
        except Exception as exc:
            error = self._error_payload(call, "ToolExecutionError", str(exc))
        self._trace.end_tool_span(tool_span_id, result, error)
        self._append_tool_message(session_id, call, result, error)

    def _error_payload(
        self, call: ToolCall, error_type: str, message: str
    ) -> dict[str, Any]:
        """构造 D23 契约的结构化错误负载。"""
        return {
            "type": error_type,
            "message": message,
            "tool": call.name,
            "args": call.args or {},
            "tool_call_id": call.id,
        }

    def _append_tool_message(
        self,
        session_id: str,
        call: ToolCall,
        result: str | None,
        error: dict[str, Any] | None,
    ) -> None:
        """把工具结果或结构化错误以 role=tool 消息落库（与 assistant 的 tool_calls 配对）。"""
        if error is not None:
            content = json.dumps({"error": error}, ensure_ascii=False)
        else:
            content = result or ""
        self._sessions.append_message(
            session_id,
            {"role": "tool", "tool_call_id": call.id, "content": content},
        )
