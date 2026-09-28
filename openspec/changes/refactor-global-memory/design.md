# Design — refactor-global-memory（跨会话全局记忆）

## 0. 用户设定的设计约束（真源）

以下约束来自用户 2026-09-28 的明确指示，本 design 不得偏离：

1. 记忆是**跨 session** 的：一个总的 `MEMORY` 目录存储全部记忆；
2. 索引记录**一条一条记忆的文件名 + 大致的内容 + tag**；
3. 后台单独的部分提取记忆后**直接追加**到 MEMORY（索引）并写入一个**新的 md 文件**；**不去重、不 merge**（demo 从简，有总结提取过程即可）；
4. 配备 **read_memory 工具**读取对应记忆的内容；
5. 记忆的具体内容**只需要有日期**。

## 1. 模块划分与职责

```text
src/harness/
  memory/
    store.py          [REWRITE] MemoryStore → 全局 MEMORY 目录唯一管理者（写入/索引/读取/进度）
    summarizer.py    [REWRITE] MemorySummarizer → 闲置增量提取（LLM JSON 输出，追加写入）
  tools/
    read_memory.py    [ADD]    ReadMemoryTool → 按文件名读取记忆全文
  context/
    builder.py       [MOD]    记忆段注入改为全局索引 + read_memory 提示行
  prompts.py          [MOD]    MEMORY_EXTRACT_PROMPT（重写）；删 MEMORY_MERGE_PROMPT；SYSTEM_PROMPT 末段
  __main__.py          [MOD]    MemoryStore 构造去 llm；_build_registry 注册 read_memory
tests/
  memory/test_memory_store.py     [REWRITE]
  memory/test_summarizer.py       [REWRITE]
  tools/test_builtin_tools.py     [ADD] TestReadMemoryTool
  context/test_builder.py         [MOD]
```

## 2. 存储布局（核心）

```text
data/
  MEMORY/                       # 全局跨会话记忆目录（data_dir / "MEMORY"）
    MEMORY.md                   # 索引：一行一条记忆，只追加、不重写
    20260928-143005.md          # 一次提取产生的记忆文件（同秒冲突追加 -2、-3 后缀）
    20260928-181030.md
    state.json                  # 各会话已提取进度 {"<session_id>": <最大已提取 ordinal>}
```

三种文件的格式约定：

**索引 `MEMORY.md`**（用户约束 2 的直接落地）：

```markdown
- 20260928-143005.md｜用户偏好简洁回复（等 3 条）（tags: 偏好, 项目）
- 20260928-181030.md｜用户养了一只猫叫团子（tags: 个人, 宠物）
```

- 一行 = 一条记忆（一次提取产生一个文件、一行索引）；
- 简述 = 第一条记忆截断 60 字，多条时追加「（等 N 条）」；
- tags 来自 LLM 提取输出，为空时省略 `（tags: …）` 段；标签数上限 5、单个截断 20 字（防单行膨胀，仅此一处截断）。

**记忆文件 `20260928-143005.md`**（用户约束 5：只要日期）：

```markdown
日期：2026-09-28

- 用户偏好简洁回复
- 正在开发 Agent Runtime demo
```

- 无 frontmatter、无哈希、无标签、无创建时间元数据——正文 = 日期行 + 记忆条目行；
- 文件名即精确时间戳（`%Y%m%d-%H%M%S`），已存在的同名文件自动追加 `-2` 后缀，不覆盖。

**进度 `state.json`**：`{"s1": 12}`，记录会话 s1 已提取到 ordinal 12。

> **为什么需要 state.json（用户未明说，但为必需的最小状态）**：不做去重 / 合并的前提下，"每次提取后直接追加"若不记录进度，同一段对话会在每轮扫描中被反复提取、无限追加副本。state.json 是唯一的防重复机制——它**不参与记忆内容管理**，只回答"这个会话还有没有没提取过的新消息"，15 行以内的实现，符合 demo 从简的原则。

## 3. 关键设计

### 3.1 MemoryStore（全局存储，重写）

```python
class MemoryStore:
    def __init__(self, data_dir: Path) -> None          # 不再注入 llm（merge 已删）
    def append(self, memories: list[str], tags: list[str]) -> str | None
        # 写 <ts>.md（日期 + 条目）→ MEMORY.md 追加一行索引 → 返回文件名
        # memories 为空 → 不写任何文件，返回 None（空提取无产物）
    def render_index(self) -> str | None
        # 读 MEMORY.md 全文；目录/文件不存在或为空 → None（builder 据此省略记忆段）
    def read(self, filename: str) -> str | None
        # 读单个记忆文件；不存在 → None（工具层转友好提示）
    def summarized_ordinal(self, session_id: str) -> int  # state.json 读，缺省 -1；损坏视为空并告警
    def mark_summarized(self, session_id: str, ordinal: int) -> None
```

- **追加即真相**：`append` 只做"新文件 + 索引行"两件事，不扫描旧条目、不重写索引、不调 LLM；
- **索引只追加**：不再有"重扫重建"逻辑（简述与 tags 只存在于索引行中，文件里没有，重建即失真）；
- 对 LLM 产物 / 文件内容一律显式校验（AGENTS 公约）：state.json 非法 JSON → 告警 + 视为空进度（下轮重写覆盖）。

### 3.2 MemorySummarizer（闲置增量提取，重写）

保留：daemon 线程、`start/stop`、`scan_interval_seconds` 周期、mtime 闲置判定（`idle_seconds`）、独立 trace + `idle_summary` span。

`scan_once` 新流程：

```text
for session_id in sessions.session_ids():
    try:
        if not is_idle(session_id): continue            # 活跃会话跳过（不变）
        records = sessions.load_records(session_id)
        last = memory.summarized_ordinal(session_id)
        new_msgs = [r["message"] for r in records
                    if r.get("kind") == "message"          # 只取消息记录
                    and isinstance(r.get("ordinal"), int)
                    and r["ordinal"] > last
                    and isinstance(r.get("message"), dict)]
        if not new_msgs: continue                       # 已提取过 → 0 次 LLM 调用
        raw = llm(MEMORY_EXTRACT_PROMPT + render(new_msgs))   # trace 同现状
        memories, tags = _parse_extraction(raw)          # 显式 JSON 校验
        if memories:
            memory.append(memories, tags)                # 追加文件 + 索引行
        memory.mark_summarized(session_id, max(消息 ordinal))
        summarized.append(session_id)
    except Exception:                                    # 单会话失败仅告警
        logger.warning(...); continue                    # 进度未推进 → 下轮自然重试
```

`_parse_extraction` 校验规则（对 LLM 输出不信任）：

- `json.loads` 失败 / 非对象 → `ValueError` → 本轮跳过、**进度不推进**（下轮重试，无半成品落盘）；
- `memories`：list，逐条 `str` 化并 strip，丢弃空串；缺失或非 list → `ValueError`；
- `tags`：list 同法清洗，缺失容忍为 `[]`（tags 是次要信息）；
- `{"memories": [], "tags": []}` 合法：无值得记的内容 → 不写文件，**只推进进度**（避免每轮空转重试）。

### 3.3 提取提示词（prompts.py）

`MEMORY_SUMMARY_PROMPT` 重写为 `MEMORY_EXTRACT_PROMPT`，`MEMORY_MERGE_PROMPT` 删除：

```text
你是记忆提取助手。请阅读以下对话记录，提取值得长期记住的用户相关信息
（用户偏好、个人事实、约定等）。
不要记录待办事项、工具调用过程或一次性的问答内容。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{"memories": ["一条记忆（中文，简洁，一件事一条）", ...], "tags": ["少量分类标签", ...]}
若没有值得记住的信息，输出 {"memories": [], "tags": []}。

对话记录：
```

选 JSON 而非自由文本：memories 与 tags 需要程序化分流（文件正文 vs 索引行），且符合公约"对 LLM 返回的 JSON 显式校验"。

### 3.4 ReadMemoryTool（新工具）

```python
class ReadMemoryTool(BaseTool):
    name = "read_memory"
    description = "读取长期记忆详细内容：按记忆索引中的文件名读取对应记忆全文"
    parameters = {"file": {"type": "string",
        "description": "记忆文件名（来自系统提示词中的记忆索引），如 20260928-143005.md"}}
    def __init__(self, memory: MemoryStore) -> None: ...   # 注入 store（MEMORY 布局单一真源）
```

- **注入 MemoryStore 而非 data_dir**：MEMORY 目录布局的知识只存在于 store 一处，工具不重复实现路径拼接 / 校验；
- 参数校验（`ToolExecutionError` 结构化回传）：非 str / 空串 / 不以 `.md` 结尾 / 含 `/` 或 `\` / `Path(file).name != file`（路径穿越一律拒绝）；
- 文件不存在：返回 `未找到记忆文件 <file>，请确认文件名来自记忆索引`（与 SearchTool 的"未找到"同风格，可恢复场景不抛异常）；
- 注册：`_build_registry(config, session_ref, memory)` 签名增加 memory 参数，`registry.register(ReadMemoryTool(memory))`。

### 3.5 上下文注入（builder.py）

- `build()`：`memory.render_summary(session_id)` → `memory.render_index()`（**索引是全局的，与当前会话无关**——这正是跨会话共享的落点）；
- 注入形态（`MEMORY_SECTION_HEADER` 不变，新增 `MEMORY_TOOL_HINT` 常量）：

```text
SYSTEM_PROMPT

## 历史记忆
- 20260928-143005.md｜用户偏好简洁回复（tags: 偏好, 项目）
（如需某条记忆的完整内容，用 read_memory 工具按文件名读取）
```

- **索引全文注入、不设全局截断**：索引行按构造即短（简述 60 字 + tags 上限），demo 量级下整段可控；单行截断已把膨胀限制在 O(条数)，换取实现零复杂度。无索引（`render_index() is None`）时保持 SYSTEM_PROMPT 原文，一字不加。

### 3.6 SYSTEM_PROMPT 末段（prompts.py）

```text
若本提示词之后附有记忆段（全局长期记忆索引：文件名、简述与标签），
回答时须结合该段内容理解用户背景与偏好；需要某条记忆的完整内容时，
先用 read_memory 工具按文件名读取。
```

## 4. 关键数据流

**写入侧（后台提取 → 追加）**：

```text
scan 线程 → s1 闲置 & 有 ordinal>12 的新消息
  → LLM（MEMORY_EXTRACT_PROMPT + 新消息）→ {"memories": [...], "tags": [...]}
  → store.append → 写 data/MEMORY/20260928-143005.md + MEMORY.md 追加一行
  → state.json: {"s1": 15}          # 只认消息 ordinal，下轮只看 15 之后
```

**读取侧（任意会话组装上下文 / 工具调用）**：

```text
ContextBuilder.build(任意 session)
  → memory.render_index() → 全局索引注入 system「## 历史记忆」段
LLM 决策 → tool_call: read_memory(file="20260928-143005.md")
  → store.read → 返回「日期：… + 记忆条目」全文（role=tool 回传）
```

跨会话闭环：s1 产生的记忆 → 全局索引 → s2 的 system prompt 可见 → s2 内 LLM 可用 read_memory 取全文。

## 5. 删除清单（旧板块下线）

| 删除项 | 位置 | 理由 |
| --- | --- | --- |
| `merge()` 与 ADD/UPDATE/DELETE 动作应用 | store.py | 用户判定该机制不可行；新方案无合并 |
| 内容哈希去重（`_content_hash` 等） | store.py | 用户明确不去重 |
| 按会话隔离目录 `data/memory/<session_id>/` | store.py | 改为全局 `data/MEMORY/` |
| 索引重扫重建 / frontmatter 解析 | store.py | 索引只追加；文件无元数据 |
| `MEMORY_MERGE_PROMPT` | prompts.py | 无合并即无提示词 |
| `MEMORY_SUMMARY_PROMPT` | prompts.py | 被 `MEMORY_EXTRACT_PROMPT` 取代 |
| 旧 `data/memory/` 产物 | 数据 | 不迁移，直接废弃（demo） |

## 6. 被否决的备选方案与原因

### 备选 1：保留按会话目录，读取时跨目录聚合

- **做法**：各会话仍独立存储，注入时聚合全部目录的索引。
- **否决原因**：用户明确要"一个总的 MEMORY 目录"（约束 1）；且聚合读 + 追加写在目录管理上并不比单目录简单，反而多一层聚合逻辑。

### 备选 2：保留哈希去重（写入口挡一下完全相同内容）

- **做法**：`append` 前对内容做 SHA-256，重复即跳过。
- **否决原因**：用户原话"不用去重以及 merge 之类的，因为只是一个 demo"；去重对"进度型增量提取"收益趋近于零（同一段消息不会二次进入提取 prompt），属于纯增量复杂度。

### 备选 3：每次提取后由 LLM 改写 MEMORY.md 全局索引（摘要式索引）

- **做法**：索引不追加，每次让 LLM 输出整理后的完整索引。
- **否决原因**：这正是用户否定的旧 merge 思路在索引层的翻版（发给 LLM 部分状态、指望它维护全局一致性）；且索引行（文件名+简述+tags）完全可由规则生成，无需 LLM。

### 备选 4：read_memory 同时支持列目录 / 按关键词查记忆

- **做法**：工具支持无参调用返回索引，或关键词过滤。
- **否决原因**：索引已经全文注入 system prompt，LLM 天然可见全部文件名与简述；工具只做"读指定文件"一件事，参数校验与错误路径最简。

### 备选 5：提取进度记进会话 JSONL（session 文件内加 kind=memory_mark 记录）

- **做法**：不建 state.json，在会话文件里追加进度记录。
- **否决原因**：会话 JSONL 是消息真源，混入记忆进度污染数据结构；且提取是记忆域的关注点，进度随 MEMORY 目录内聚更合理（store 单一类管理该目录全部文件）。
