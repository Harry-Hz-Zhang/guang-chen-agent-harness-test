# guang-chen-agent-harness-test

从零实现的最小可用 Agent Runtime，不依赖任何 agent 框架，通过 `openai` SDK 直连 DeepSeek。

## 功能

- ReAct 基本循环：输入 → LLM 决策 → 工具调用 → 最终答案
- 工具注册机制，LLM 基于参数 Schema 自主调用（calculator / search / weather / read_memory）
- 会话隔离与持久化，支持重启续接与多轮追问
- 上下文管理与超长自动压缩
- 全局长期记忆，跨会话共享
- 流式分通道渲染（思考 / 正文）
- 工具调用 trace 日志

## 测试

```bash
uv run pytest
```

全部测试通过（全 mock，零真实网络调用）。
