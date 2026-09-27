# GLOSSARY.md — 多Agent协同开发术语表

## A

**Agent（智能体）**
一个能感知环境、做出决策并采取行动的自主实体。在多Agent系统中，每个Agent通常由 LLM + Tool + Memory + Prompt 组成。

**Orchestrator / Worker（主从架构）**
一个中心Agent（Orchestrator）负责任务拆解和路由，多个Worker Agent负责执行子任务。适合任务结构清晰、依赖关系明确的场景。

**Pipeline（流水线架构）**
Agent按固定顺序依次执行，每个Agent的输出是下一个Agent的输入。类似Unix管道，简单直观，适合线性工作流。

**Router（路由架构）**
根据输入内容动态选择下一步要走的路径（哪个Agent处理什么）。适合需要根据输入类型做分支判断的场景。

**Peer-to-Peer（对等架构）**
Agent之间直接对话、协作，没有中心调度者。适合需要高度灵活协商的场景，但协调成本较高。

## M

**Memory（记忆）**
Agent跨轮次保留上下文的能力。分为 Short-term（当前对话窗口）和 Long-term（持久化存储，如向量数据库）。

**Multi-turn Conversation（多轮对话）**
Agent之间进行多轮消息交换，逐步推进任务。AutoGen 框架的核心交互模式。

## P

**Prompt Engineering（提示工程）**
设计和优化输入给LLM的文本，以引导其产生期望的行为和输出。是Agent可靠性的核心。

**System Prompt（系统提示）**
定义Agent角色、行为准则和能力的指令，通常在会话开始时设置，不随对话轮次消失。

## T

**Tool Calling / Function Calling**
让LLM在需要时调用外部函数（而非直接生成文本）。是Agent获得"行动能力"的关键机制。

**Tool（工具）**
Agent可调用的外部函数，如搜索API、代码执行、数据库查询等。

## L

**LangGraph**
LangChain官方提供的多Agent编排框架，基于图（Graph）模型，支持状态机、循环、条件路由等复杂控制流。

**CrewAI**
基于角色的多Agent协作框架，概念直观（Role/Task/Crew），适合快速构建协作型Agent团队。

**AutoGen（微软）**
以对话驱动的多Agent框架，Agent之间通过消息交换完成协作，适合研究和探索型任务。

**LLM（大语言模型）**
Large Language Model，提供自然语言理解和生成能力的模型（如GPT-4、Claude等）。

**Hallucination（幻觉）**
LLM生成看似合理但事实错误的内容。多Agent系统中需要Retry、验证、Human-in-the-loop等机制来缓解。

**Human-in-the-loop（人工介入）**
在Agent流程中插入人工审核环节，让人类确认关键决策或输出后再继续。

**State（状态）**
多Agent系统中所有Agent共享的上下文数据，通常以字典/JSON结构存储，用于跨轮次传递信息。

**MCP（Model Context Protocol）**
Anthropic提出的开放协议，标准化Agent与外部数据源/工具之间的连接方式。

**Citation（引用）**
在知识类课程中，指向原始高信任资源的链接，用于验证课程内容并供学习者深入阅读。
