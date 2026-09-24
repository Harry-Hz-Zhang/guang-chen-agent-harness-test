# AGENTS.md — 本项目公约

> 本文件是 AI 助手（Copilot / Codex / Claude 等）在本仓库工作时**每次会话都会读取**的公约。
> 修改本文件等同于修改项目规范，请保持简短、可执行、不含歧义。

## 1. 项目目标

从零实现一个**最小可用 Agent Runtime**（不依赖 langgraph / openhands / openclaw 等任何 agent 框架），
需求原文见 [`doc/PRD.md`](doc/PRD.md)。

核心要交付的能力：

1. ReAct 基本循环（接收输入 → 决策 → 调工具 → 判断继续或返回）
2. 工具注册机制（名称 + 描述 + 参数 Schema），由 LLM 基于 Schema 自主决策调用
3. LLM 输出解析（提取思考过程 / 工具调用 / 最终答案）
4. session 隔离与持久化（同用户多窗口互不影响）
5. context 有效管理（最大轮次、多轮记忆、追问支持、超长基础压缩）
6. 异常处理 + 工具调用 trace 日志

## 2. 技术栈

| 项 | 选择 |
| --- | --- |
| 语言 | Python 3.13+（本机实测 3.13.15） |
| 依赖管理 | `uv`（本机已装，`.venv` 已就绪） |
| 测试 | `pytest`（实测可用） |
| LLM 提供方 | **DeepSeek**（OpenAI 兼容协议） |
| LLM SDK | `openai` 官方 SDK（实测 3.19.2），通过 `base_url` 指向 DeepSeek |
| 密钥 | 环境变量 `DEEPSEEK_API_KEY`，**绝不写入代码或提交** |
| base_url | `https://api.deepseek.com` |

约定模型名：

- 主模型：`deepseek-chat`
- 模型名与 base_url 必须可从环境变量覆盖：`LLM_MODEL` / `LLM_BASE_URL`

## 3. 目录约定

```text
doc/                    PRD 与说明文档（需求原文，冻结不改）
src/harness/            核心 runtime
  __init__.py
  __main__.py           CLI 入口（python -m harness）
  loop.py               ReAct 主循环 + 最大轮次限制
  llm.py                模型客户端封装
  parser.py             LLM 输出解析（思考 / 工具调用 / 最终答案）
  tools/
    base.py             工具抽象与参数 Schema 描述
    registry.py         工具注册表
    calculator.py
    search.py
    ...                 其余工具
  session/
    store.py            session 存储（隔离 + 持久化）
  context/
    builder.py          context 组装
    compressor.py       超长压缩
  trace.py              工具调用 trace 与日志
tests/                  pytest 测试（与 src 镜像）
openspec/               VSDD/OpenSpec artifacts（方案真源）
```

**新增顶层目录必须先在本文件登记。**

## 4. 代码规范

- 所有函数（含私有）必须有**类型注解**，包括返回值。
- 公共模块、类、函数必须有 **中文 docstring**，说明「做什么」而不是「怎么做」。
- 用 `logging` 或项目内 `trace` 模块记录，**禁止**用 `print` 做调试输出。
- **禁止静默吞异常**：工具执行失败必须把错误信息结构化后回传给 LLM，由 LLM 决定是否重试或向用户说明。
- 对外部输入（LLM 返回的 JSON、工具参数）一律做**显式校验**，不信任其格式。
- 命名：模块/函数 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE`。
- 提示词模板集中放置，不散落在业务逻辑里。

## 5. 常用命令

```bash
# 安装依赖（uv，会按 pyproject 创建 .venv 并安装 openai / pytest）
uv sync

# 运行全部测试
uv run pytest

# 运行单个测试文件
uv run pytest tests/test_loop.py

# 启动 CLI
uv run python -m harness

# 指定 session 启动
uv run python -m harness --session s1
```

> 说明：本项目是**应用**不是可分发的库，`pyproject.toml` 里设了 `[tool.uv] package = false`，
> 不走 `pip install -e .` 打包安装；`src/` 通过 pytest 的 `pythonpath = ["src"]` 进入搜索路径。

## 6. 提交规范

- 每个 task 一次原子 commit，信息格式：`<type>: <中文描述>`
- `type` 取值：`feat` / `fix` / `test` / `docs` / `refactor` / `chore`
- **禁止在 main 上直接开发**；改代码前先切分支
- **禁止自动 push**，推送必须由人工确认

## 7. 明确禁止

- ❌ 引入任何 agent 框架（langgraph / openhands / openclaw / autogen / crewai）作为主流程依赖
- ❌ 把 API key、token 写进代码、测试或文档
- ❌ 在测试中调用真实 LLM API（测试必须用假模型 / mock）
- ❌ 修改 `doc/PRD.md` 的需求原文
- ❌ 未确认方案就直接开始写业务代码（走 VSDD 流程）

## 8. 开发流程（VSDD）

本项目采用 **VSDD standard 模式**，阶段顺序：

```text
explore → propose → (用户确认) → apply → verify → archive
```

- 方案真源在 `openspec/changes/<change-name>/`，不在 `doc/`
- standard 模式每个 task 必须有 RED / GREEN / ASSERT / DoD，且先写测试
- explore 阶段**不写业务代码**
