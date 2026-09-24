# 我的规划（个人笔记）

> 这是**我自己的**思考草稿，不是需求，也不是方案真源。
> - 不冻结，随便改、随便删。
> - `doc/PRD.md` 是题目需求原文（冻结）；`openspec/changes/` 才是方案真源。
> - 等这里想清楚了，可以让 AI 据此走 explore → propose，生成 openspec change。

## 1. 一些具体的实现

定义一个会话为 session，session 下面就是直接的一条一条消息

至于消息的存储可以直接调用 Memos，进行记忆存储（这里我不太清楚关于 memos 的存储逻辑，需要去调研一下，怎么样才不会出现消息的堵塞，毕竟不能直接调用 sql 存储吧），或者是自研，如果自研的话可以向 codex 学习：

if 当前的对话 session 已经很久没有更新对话了（2h），后台会有一个协程之类的不断扫描所有的 session，然后通过这一点可以总结记忆，如果保持轻量的话，应该直接本地磁盘开一个文件夹，放入对应 session id 的对应 记忆文档，这里应该分成两部分，一个是 MEMORY.md，写入所有记忆的目录另外一些细碎的文档中写入具体的记忆。

但是去重和修改的方案还需要你去调研一下，完善这个方案

对于模型调用的http进行抽象，具体的话参考 langchain 的做法

然后创建一个 Agent 也学习 langchain 的创建方式，可以绑定对应的 tool 还有中间件（不一定要完全一致那么复杂，但要借鉴这种低耦合的做法）

重点实现 stream 流式输出，同样参考 langchain 的代码，并给出一个demo代码，让我审核，如何把 thinkblock 中的流式输出和 textblock 分开等

关于上下文压缩做成 middleware 的形式，具体的上下文压缩策略有以下：

首先是总 token 数量达到model 窗口的 80% （所以对于每一次调用还需要记录一下 token 消耗之类的）
另外就是当前 session 没有压缩的对话轮次达到 60 轮
以上是两个临界条件

具体压缩方式就是参考 agentscope 的做法，首先把需要压缩的信息给拿出来放入本地运行的文件夹中，这是一点，另外就是对于这部分信息生成摘要，载入上下文中

这里的具体实现参考：D:\Users\hongze01.zhang\PycharmProjects\smart-delivery-agent\smart-delivery-agent-service\src\vip_ads_agent\agent\context 以及 D:\Users\hongze01.zhang\PycharmProjects\smart-delivery-agent\smart-delivery-agent-service\src\vip_ads_agent\agent\executor\context  这里有一些具体的实现方式

还需要定义一个 agentcontext，其中使用 history 这个 list[dict] 存储对应的当前运行状态的消息，这里的具体做法可以再商榷，以及需要定义哪些字段，

基本异常处理
工具调用 trace 或执行日志