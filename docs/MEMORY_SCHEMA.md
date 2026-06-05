# Memory Schema Design

本文档定义 Confidential Agent Memory Vault 的长期记忆设计策略。目标是把当前的“加密向量存储”逐步升级为企业 Agent 可使用、可解释、可治理、可遗忘的长期记忆系统。

当前文档只描述设计策略，不要求立即修改代码。

## 1. 设计目标

长期记忆系统需要解决的不只是“保存文本并向量检索”，而是完整管理一条事实的生命周期：

- 判断哪些信息值得进入长期记忆。
- 将自然语言事实规范化为结构化事实。
- 对重复事实进行去重和合并。
- 对冲突事实进行替换和失效处理。
- 支持用户主动遗忘和自动过期。
- 在检索时只返回当前有效、必要且允许暴露的记忆。
- 保持 per-user 隔离，避免跨用户泄露。

本项目的 memory vault 应该保存长期事实，而不是完整聊天记录。

## 2. Memory 分层

系统中的信息可以分为三层：

```text
短期上下文
  当前会话临时使用，不写入 vault。

长期记忆
  稳定事实、长期偏好、用户背景、项目背景、长期指令。

审计记录
  谁在什么时候访问、创建、更新、删除了 memory。v1 可以先不实现。
```

长期记忆的判断标准：

- 未来多次对话中可能复用。
- 来自用户明确表达、可信工具或业务系统。
- 不是一次性任务。
- 不是模型猜测。
- 不是助手建议。

应该保存的例子：

```text
用户喜欢喝粥。
用户正在寻找 Agent 开发实习。
用户希望技术解释一步一步来。
用户的项目是一个 confidential memory vault。
```

不应该保存的例子：

```text
用户今天想吃什么。
用户刚才问了一个问题。
助手建议用户学习 LangGraph。
模型推测用户可能焦虑。
```

## 3. Schema v1

每条 memory 同时包含两种表达：

- `content`：给 LLM 使用的自然语言事实。
- 结构化字段：给程序进行去重、合并、冲突处理和治理。

建议 v1 schema：

```json
{
  "schema_version": 1,
  "id": "uuid",

  "content": "用户喜欢喝粥",
  "memory_type": "preference",
  "sensitivity": "low",

  "subject": "user",
  "predicate": "likes",
  "object": "粥",
  "value": true,
  "slot": "preference.like_dislike:粥",

  "fact_key": "preference:user:likes:粥",
  "conflict_key": "user:slot:preference.like_dislike:粥",

  "status": "active",
  "confidence": 0.8,
  "evidence_count": 1,

  "source": "user",
  "created_at": "2026-05-31T00:00:00+00:00",
  "updated_at": "2026-05-31T00:00:00+00:00",
  "last_seen_at": "2026-05-31T00:00:00+00:00",
  "last_accessed_at": null,
  "access_count": 0,

  "expires_at": null,
  "supersedes": [],
  "superseded_by": null,
  "forgotten_at": null,
  "forgotten_reason": null,

  "embedding": [],
  "embedding_model": "text-embedding-v4"
}
```

## 4. 字段说明

`schema_version`

当前 memory schema 版本。用于后续迁移旧数据。

`id`

单条 memory 的 UUID。不要使用可预测 ID。

`content`

自然语言长期事实，主要给 LLM 使用。应使用第三人称，例如“用户喜欢喝粥”，不要保存“我喜欢喝粥”。

`memory_type`

细粒度记忆类型。v1 推荐限制为：

```text
preference
profile
health
project
instruction
other
```

`sensitivity`

敏感度。v1 推荐：

```text
low
high
```

高敏感 memory 在检索输出时应做最小化处理，不应默认把原文注入外部 LLM。

`subject`

事实主体。当前大多数是 `user`。未来可以支持 `project`、`team`、`organization`。

`predicate`

事实关系，例如：

```text
likes
dislikes
has_goal
has_health_condition
prefers_response_style
works_on_project
lives_in
works_at
has_job_search_status
prefers_language
```

`object`

事实对象，例如“粥”、“Agent 开发实习”、“step_by_step”。

`value`

事实值。可以是布尔值、字符串、数字或对象。v1 中尽量使用简单 JSON 类型。

`slot`

互斥事实槽位。`slot` 描述这条 memory 是否占用某个“同一时间只能有一个 active 值”的事实槽位。

```text
slot == null
  这条 memory 是可共存事实，不会和其他不同 fact_key 的 memory 冲突。

slot != null
  同一用户下，同一个 slot 只能有一条 active memory。
```

示例：

```text
profile.current_city
profile.current_company
profile.job_search_status
project.primary_project
instruction.response_language
preference.like_dislike:咖啡
```

`predicate` 描述事实语义，`slot` 描述生命周期互斥槽位。extractor 可以提出 slot 候选，但 trusted vault 必须重新规范化和校验 slot。

`fact_key`

用于判断“是否是同一个事实”的稳定 key。

推荐格式：

```text
{memory_type}:{subject}:{predicate}:{object}
```

例子：

```text
preference:user:likes:粥
instruction:user:prefers_response_style:step_by_step
profile:user:has_goal:agent_developer_internship
```

`conflict_key`

store 内部用于执行冲突替换的 key。v1 中 `conflict_key` 由 `slot` 推导：

```text
slot != null:
  conflict_key = user:slot:{slot}

slot == null:
  conflict_key = fact_key
```

冲突事实可能有不同的 `fact_key`，但拥有同一个 `conflict_key`。

例子：

```text
user:slot:preference.like_dislike:咖啡
user:slot:profile.current_city
user:slot:profile.job_search_status
```

`status`

memory 生命周期状态。v1 推荐：

```text
active
superseded
forgotten
expired
```

只有 `active` 状态可以参与普通检索。

`confidence`

置信度，范围 `0.0` 到 `1.0`。用户明确表达的事实可以从 `0.8` 起步，模型推断不应进入长期记忆。

`evidence_count`

同一事实被重复确认的次数。重复确认时不新增 memory，而是增加该字段。

`source`

事实来源。v1 推荐：

```text
user
system
tool
admin
```

`created_at`

第一次创建时间。

`updated_at`

最后一次修改时间，包括合并、冲突替换、状态变化。

`last_seen_at`

最后一次从输入中再次观察到同一事实的时间。

`last_accessed_at`

最后一次被检索使用的时间。

`access_count`

被检索使用次数。后续可用于记忆重要性排序或遗忘策略。

`expires_at`

过期时间。`null` 表示默认长期保留。高敏感或项目型 memory 可以设置 TTL。

`supersedes`

当前 memory 替代了哪些旧 memory。

`superseded_by`

当前 memory 被哪条新 memory 替代。

`forgotten_at`

用户主动要求遗忘的时间。

`forgotten_reason`

遗忘原因，例如：

```text
user_requested
expired
conflict_superseded
policy
```

`embedding`

向量表示，用于语义检索。

`embedding_model`

生成 embedding 的模型名，用于未来模型迁移。

## 5. Memory Type 策略

v1 中不要设计过多类型，避免 extractor 和 store 逻辑过早复杂化。

推荐类型：

```text
preference
  用户长期偏好。

profile
  用户身份、背景、长期目标。

health
  健康限制、医疗相关事实。默认 high sensitivity。

project
  用户正在长期推进的项目背景。

instruction
  用户对 Agent 行为方式的长期偏好。

other
  暂时无法归类但确实值得长期保存的事实。
```

示例：

```json
{
  "memory_type": "preference",
  "content": "用户喜欢喝粥",
  "subject": "user",
  "predicate": "likes",
  "object": "粥",
  "value": true,
  "slot": "preference.like_dislike:粥"
}
```

```json
{
  "memory_type": "profile",
  "content": "用户正在寻找 Agent 开发实习",
  "subject": "user",
  "predicate": "has_goal",
  "object": "agent_developer_internship",
  "value": "active",
  "slot": null
}
```

```json
{
  "memory_type": "instruction",
  "content": "用户希望技术解释一步一步来",
  "subject": "user",
  "predicate": "prefers_response_style",
  "object": "step_by_step",
  "value": true,
  "slot": null
}
```

## 6. 状态机

memory 的生命周期：

```text
active
  当前有效，可以参与检索。

superseded
  被更新事实替代，默认不参与检索。

forgotten
  用户主动要求遗忘，默认不参与检索。

expired
  到达 expires_at 或 retention policy 到期，默认不参与检索。
```

状态转换：

```text
new memory -> active
active + duplicate evidence -> active, evidence_count += 1
active + conflicting newer fact -> superseded
active + user forget request -> forgotten
active + expires_at reached -> expired
```

检索默认规则：

```text
只检索 status == active 且未过期的 memory。
```

## 7. 去重策略

不要只依赖 embedding 去重。推荐三层判断：

```text
第一层：fact_key 精确匹配
第二层：规范化 content 匹配
第三层：embedding 高相似度匹配
```

处理顺序：

1. 如果 `fact_key` 相同，视为同一事实，执行合并。
2. 如果规范化后的 `content` 相同，执行合并。
3. 如果 embedding 相似度超过高阈值，并且类型一致，执行合并。
4. 否则创建新 memory。

推荐初始阈值：

```text
duplicate_similarity_threshold = 0.90
retrieval_similarity_threshold = 0.40
```

## 8. 合并策略

重复事实不应该简单跳过，而应该增强已有 memory。

合并时更新：

```text
evidence_count += 1
confidence = min(0.99, confidence + 0.03)
last_seen_at = now
updated_at = now
```

如果新事实的 `content` 更规范、更清晰，可以替换旧 `content`，但不能改变事实含义。

示例：

```text
已有：用户喜欢喝粥。
新输入：我平时挺爱喝粥的。
结果：不新增，更新 evidence_count、confidence、last_seen_at。
```

## 9. 冲突策略

冲突不是重复。冲突表示两个事实不能同时作为当前事实使用。

典型冲突：

```text
用户喜欢咖啡。
用户不喜欢咖啡。

用户住在北京。
用户住在上海。

用户当前求职状态是正在找实习。
用户当前求职状态是已经找到实习。
```

冲突判断应基于 `slot` 推导出的 `conflict_key`。

如果新 memory 与旧 active memory 有相同 `conflict_key`，但 `fact_key` 不同，则认为可能冲突。

默认情况下，不同 `fact_key` 的 memory 不冲突。只有 `slot` 非空时，才会占用互斥槽位并触发替换。

可共存事实示例：

```text
用户正在找 Agent 开发实习。slot = null
用户正在练英语。slot = null
用户喜欢代码示例。slot = null
用户喜欢一步一步解释。slot = null
```

互斥事实示例：

```text
用户住在北京。slot = profile.current_city
用户住在上海。slot = profile.current_city

用户喜欢咖啡。slot = preference.like_dislike:咖啡
用户不喜欢咖啡。slot = preference.like_dislike:咖啡
```

冲突处理：

```text
旧 memory:
  status = superseded
  superseded_by = new_memory_id
  updated_at = now

新 memory:
  status = active
  supersedes = [old_memory_id]
```

不要直接删除旧 memory。保留替换链路有助于审计和调试。

## 10. 遗忘策略

v1 支持三种遗忘：

```text
用户主动遗忘
  用户明确要求删除或忘记某条记忆。

TTL 自动过期
  memory 到达 expires_at 后变为 expired。

冲突替换
  新事实替代旧事实，旧事实变为 superseded。
```

主动遗忘建议默认 soft delete：

```json
{
  "status": "forgotten",
  "forgotten_at": "2026-05-31T00:00:00+00:00",
  "forgotten_reason": "user_requested"
}
```

hard delete 可作为后续能力，但 v1 默认 soft delete 更便于调试。

不同类型的 retention 建议：

```text
preference
  长期保留，除非用户主动删除或出现冲突。

profile
  长期保留，但状态型事实需要冲突替换。

health
  高敏感。默认 high sensitivity，检索时做最小化输出。

project
  中期保留，可以设置 TTL。

instruction
  长期保留，除非用户更新偏好。
```

## 11. 检索策略

retrieve 流程应当是：

```text
1. 根据 user_id 找到该用户 key。
2. 解密该用户 memory 文件。
3. 过滤 status != active 的 memory。
4. 过滤已经 expired 的 memory。
5. 根据 query 做向量检索。
6. 对 high sensitivity memory 做最小化输出。
7. 返回最小必要上下文。
8. 更新 last_accessed_at 和 access_count。
```

返回给外部 Agent 的内容不应该是完整 memory JSON，而应该是最小化后的上下文文本。

## 12. 敏感信息最小化

高敏感 memory 不应默认原文返回。

例子：

原始 memory：

```text
用户有糖尿病。
```

返回给外部 LLM 的最小化上下文：

```text
用户有健康相关限制，饮食或生活建议需要保守。
```

当前项目已有 `_minimize_memories()`，后续可以按 `memory_type` 和 `sensitivity` 增强这部分逻辑。

## 13. 安全边界策略

memory schema 需要服从 vault 的安全边界：

- `user_id` 来自 secure request，不信任 memory payload 自带的 `user_id`。
- `src/untrusted/memory_extractor.py` 只生成候选 memory。
- `src/trusted/vault_server.py` 必须重新校验所有字段。
- `src/trusted/memory_store.py` 负责最终规范化、去重、合并、冲突处理。
- `src/trusted/memory_retriever.py` 只检索当前用户的 active memory。
- `forgotten`、`expired`、`superseded` memory 默认不能参与检索。
- 高敏感 memory 默认不原文返回给外部 LLM。

## 14. Slot 规范化策略

extractor 可以输出候选 `slot`，但 trusted vault 必须重新规范化，store 不直接相信 untrusted 输入。

推荐 v1 规则：

```text
likes / dislikes:
  slot = preference.like_dislike:{object}

lives_in:
  slot = profile.current_city

works_at:
  slot = profile.current_company

has_job_search_status:
  slot = profile.job_search_status

prefers_language:
  slot = instruction.response_language

works_on_project:
  默认 slot = null；只有明确“当前主要项目”时才允许 project.primary_project。

has_goal / prefers_response_style:
  默认 slot = null，可以共存。
```

核心职责分工：

```text
predicate 描述事实语义。
slot 描述互斥生命周期槽位。
vault 负责规范化 slot。
store 只根据 slot 执行冲突替换。
```

## 15. 后续实现顺序

建议按以下顺序实现，避免一次性改动过大：

1. 升级 extractor 输出，让它生成结构化 memory 候选。
2. 扩展 vault 的 `_validate_memory()`，补默认值并校验字段。
3. 在 store 中生成 `fact_key` 和 `conflict_key`。
4. 实现 `fact_key` 精确去重和合并。
5. 实现 `conflict_key` 冲突替换。
6. retrieve 只返回 `active` 且未过期 memory。
7. 增加 `forget` secure request。
8. 增加 memory schema 迁移逻辑，兼容旧数据。

## 16. v1 验收标准

完成 v1 后，系统应满足：

- 同一用户重复表达同一事实时，不新增重复 memory。
- 重复事实会增加 `evidence_count` 并更新 `last_seen_at`。
- 冲突事实会让旧 memory 变为 `superseded`。
- `forgotten`、`expired`、`superseded` memory 不参与检索。
- 高敏感 memory 不直接原文注入外部 LLM。
- 不同用户 memory 仍然使用不同文件和不同 key 隔离。
- 旧 schema 数据可以被兼容读取或迁移。

核心原则：

```text
Memory 不是聊天记录，也不是普通向量片段。
Memory 是有来源、有结构、有状态、有生命周期、可治理的长期事实。
```
