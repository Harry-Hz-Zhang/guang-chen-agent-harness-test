# guang-chen-agent-harness-test

从零实现的最小可用 Agent Runtime，不依赖任何 agent 框架，通过 `openai` SDK 直连 DeepSeek。

## 运行方式

```bash
uv sync

export DEEPSEEK_API_KEY=sk-xxx    # 或复制 .env.example 为 .env 填入

PYTHONPATH=src uv run python -m harness                # 启动（新会话）
PYTHONPATH=src uv run python -m harness --session s1   # 续接指定会话
PYTHONPATH=src uv run python -m harness --concurrent s1:查天气 s2:写周报   # 单进程并发跑多个会话
```

REPL 命令：`/new` 新会话、`/switch <id>` 切换、`/sessions` 列出全部、`/history` 概览、`/exit` 退出。

`--no-stream` 关闭流式输出，`--no-thinking` 关闭思考模式。模型与运行参数可用环境变量覆盖：`LLM_MODEL` / `LLM_BASE_URL`，以及 `HARNESS_` 前缀的数值项（如 `HARNESS_MAX_ROUNDS`）。

## 系统设计

一次请求的流转：ContextBuilder 组装上下文（system → 压缩摘要 → 未压缩历史 → 当前输入）→ LLM 决策 → parser 解析输出；是工具调用就执行并回填结果进入下一轮，是最终答案就渲染返回。单次请求最多 15 轮决策。

| 模块 | 职责 |
| --- | --- |
| `loop.py` | ReAct 主循环与决策轮上限 |
| `runner.py` | 单进程多会话并发：线程池批量执行多个会话的 ReAct 循环（一个 agent 一个线程，会话上下文经 ContextVar 隔离） |
| `tools/` | 工具注册表，LLM 按参数 Schema 自主调用（calculator / search / weather / read_memory / write_todos） |
| `session/` | 会话隔离与持久化，每会话一个 JSONL 文件，只追加 |
| `context/` | 上下文组装；未压缩窗口超 60 轮或估算 10 万 token 时压缩为摘要，保留最近 5 轮 |
| `memory/` | 全局长期记忆，见下节 |
| `renderer.py` | 流式输出按思考 / 正文分通道渲染 |
| `trace.py` | LLM 与工具调用记 JSONL trace，落在 `data/traces/` |

## 长期记忆

记忆是全局的，跨会话共享，落在 `data/MEMORY/`：`MEMORY.md` 是索引（一行一条：文件名、简述、tags，只追加），正文是时间戳命名的 md 文件，`state.json` 记录各会话提取到哪条消息。

写入：后台线程每 5 分钟扫描一次会话，闲置超过 2 小时（按文件 mtime 判断）且有未提取消息的，用 LLM 提取成记忆追加写入。失败本轮跳过，下轮重试。

召回：每次组装上下文时，把 `MEMORY.md` 索引全文追加在 system 提示词末尾（「## 历史记忆」小节），只放索引不放全文；LLM 需要某条的完整内容时，用 read_memory 工具按文件名读取。

## 测试

```bash
uv run pytest
```

全部测试通过（全 mock，零真实网络调用）。
