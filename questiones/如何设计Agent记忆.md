# 面试问题：如何设计 Agent 记忆

## 回答

我认为 Agent 记忆不能简单理解为“把历史对话存进向量数据库”。一个完整的记忆系统需要同时解决五个问题：

1. 记什么：区分临时上下文和长期有价值的信息。
2. 怎么表示：既保留自然语言语义，也支持结构化事实管理。
3. 怎么更新：处理重复、冲突、过期和遗忘。
4. 怎么检索：只召回与当前任务相关且仍然有效的记忆。
5. 怎么保护：控制谁能访问、能访问什么，以及最终向模型披露多少。

基于这些目标，我会把 Agent 记忆设计为“短期记忆 + 长期记忆”的分层系统。

## 一、短期记忆

短期记忆保存最近几轮消息和当前任务状态，主要用于：

- 保持多轮对话连贯性。
- 处理指代和省略。
- 补全工具调用所需的参数。
- 保存当前任务的中间状态。

本项目使用 Redis 保存短期会话历史，并通过 capability 指纹和 `session_id` 隔离用户与会话，不保存 capability 原文。

短期记忆具有较短的生命周期，不应该直接变成稳定用户画像。例如“今天想喝粥”通常只是临时状态，不能自动推断为长期饮食偏好。

## 二、长期记忆

长期记忆保存跨会话仍然有价值的信息，例如：

- 用户长期偏好。
- 稳定身份和背景。
- 长期目标和项目。
- 健康限制。
- 用户希望助手长期遵循的交互方式。

长期记忆不应保存全部对话，而应从用户原话中抽取稳定事实。本项目明确不从助手回答中抽取，避免把模型幻觉写入用户画像。

例如：

```text
用户原话：我长期更喜欢简洁的回答

抽取结果：
(user, prefers_response_style, 简洁)
```

对于“帮我推荐一下今天吃什么”这类一次性请求，则不写入长期记忆。

## 三、混合记忆表示

我会同时保留自然语言内容、结构化三元组和 embedding：

```json
{
  "content": "用户住在上海",
  "memory_type": "profile",
  "subject": "user",
  "predicate": "lives_in",
  "object": "上海",
  "value": true,
  "slot": "profile.current_city",
  "confidence": 0.9,
  "sensitivity": "low",
  "source": "user"
}
```

三种表示各有作用：

- `content` 用于展示、审计和生成 embedding。
- `(subject, predicate, object)` 用于事实识别、过滤和冲突判断。
- embedding 用于处理自然语言表达不同但语义相近的情况。

因此它不是纯向量记忆，也不是完整图数据库，而是轻量级结构化记忆图谱与语义检索的结合。

## 四、谓词与 Slot

谓词负责表达关系，例如：

```text
likes
dislikes
has_goal
lives_in
works_at
prefers_language
```

Slot 只负责表达互斥性。没有明确互斥语义时，不应该设置 slot。

例如当前城市通常是单值属性：

```text
lives_in -> profile.current_city
```

而长期目标允许多值：

```text
has_goal -> slot = null
```

喜欢和不喜欢需要按对象动态互斥：

```text
(user, likes, 咖啡)
(user, dislikes, 咖啡)

slot = preference.like_dislike:咖啡
```

本项目当前通过谓词白名单和硬编码 slot 规则进行管理。更完善的方案是引入版本化、分层的谓词注册表：

- 核心层：稳定且需要精确冲突处理的通用谓词。
- 领域层：例如 `career`、`wechat` 等业务谓词。
- 候选层：保存模型提出但尚未注册的长尾关系。

LLM 可以提出候选关系，但不能自行定义正式谓词、slot 或冲突规则。最终规则必须由 Vault 校验。

## 五、去重机制

长期记忆不能简单追加，否则会快速膨胀并重复影响回答。本项目使用两级去重。

### 1. 确定性去重

系统生成 `fact_key`：

```text
memory_type:subject:predicate:object
```

例如：

```text
profile:user:lives_in:上海
```

如果新旧记忆的 `fact_key` 相同，说明是同一个事实。系统不新增记录，而是：

- 增加 `evidence_count`。
- 更新 `last_seen_at`。
- 提高 `confidence`，但设置上限。

### 2. 语义去重

对于“用户喜欢代码示例”和“用户喜欢代码样例”这类措辞不同但含义相同的记忆，系统使用 embedding 相似度进行补充判断。

语义比较必须先限制候选范围：

```text
status 相同且为 active
memory_type 相同
predicate 相同
slot 相同
```

这样可以避免把“喜欢咖啡”和“不喜欢咖啡”因为向量相似而错误合并。

## 六、冲突处理

冲突是指两条记忆不是同一个事实，但占用了同一个互斥 slot。

系统生成：

```text
conflict_key = subject:slot:<slot>
```

例如：

```text
旧事实：(user, lives_in, 北京)
新事实：(user, lives_in, 上海)
```

对应结果是：

```text
fact_key 不同
conflict_key 相同
```

因此系统将旧记忆从 `active` 更新为 `superseded`，新记忆保持 `active`：

```text
北京.superseded_by = 上海的 memory_id
上海.supersedes = [北京的 memory_id]
```

检索只返回 `active` 记忆，因此模型使用最新事实，同时系统仍然保留变化历史。

写入顺序应当是：

```text
精确事实去重
-> 互斥槽位冲突处理
-> embedding 语义去重
-> 新记忆写入
```

冲突检查放在语义去重之前，是为了避免将“喜欢咖啡”和“不喜欢咖啡”错误地合并为同一事实。

更成熟的冲突决策不应只有覆盖，还应支持：

```text
MERGE       同一事实，合并证据
SUPERSEDE   新事实明确替代旧事实
COEXIST     多值事实或有效时间不同
PENDING     冲突不明确，等待用户确认
```

## 七、生命周期管理

长期记忆需要有明确状态：

```text
active
superseded
expired
forgotten
```

- `active`：当前有效并可参与检索。
- `superseded`：已经被更新事实替代。
- `expired`：超过有效期。
- `forgotten`：用户要求遗忘或系统执行软删除。

同时记录：

```text
created_at
updated_at
last_seen_at
last_accessed_at
expires_at
valid_from
valid_to
```

时间建模很重要。例如“去年住在北京，现在住在上海”不应该被处理为两个互相覆盖的当前城市，而应该保留历史有效区间。

## 八、检索设计

检索时不能把用户的全部记忆放入 prompt，而应进行多阶段筛选：

```text
用户问题
-> 用户和权限隔离
-> active 状态与时间过滤
-> memory_type / predicate / 实体过滤
-> 向量和关键词混合召回
-> 相关性、置信度、时效性重排
-> 敏感信息最小化
-> 注入模型上下文
```

对于结果数量，应使用相关性阈值和 `top_k` 双重控制。没有足够相关的记忆时，宁可不注入，也不要让错误记忆污染回答。

## 九、安全与隐私

本项目把长期记忆放在 Vault 边界内：

- 每个用户使用独立 Fernet 密钥。
- Agent 不直接持有用户密钥。
- 记忆按用户加密存储。
- Agent 只能凭 capability 调用受限的存取接口。
- Vault 校验 predicate、slot、scope 和请求大小。
- Context Minimizer 控制解密后的披露粒度。

低敏感记忆可以返回具体内容，高敏感记忆只返回完成任务所需的类别级提示。

这体现了两个不同问题：

```text
加密解决“静态数据是否可被直接读取”
最小化解决“解密后是否向 Agent 暴露过多信息”
```

## 十、准确性与评估

记忆系统的目标不是记得越多越好，而是尽量降低错误记忆的长期影响。因此需要评估：

- 记忆抽取准确率与召回率。
- 一次性信息误写入率。
- 事实去重准确率。
- 冲突更新准确率。
- 过期记忆误召回率。
- `Precision@K` 和 `Recall@K`。
- 记忆对最终回答的正向增益和负面影响。
- 用户纠正和删除是否真正生效。

测试集需要覆盖否定、改口、时间变化、重复表达、含糊表达和敏感信息。

## 总结

我的设计原则可以概括为：

> 短期记忆负责当前上下文，长期记忆负责稳定事实；自然语言和 embedding 负责语义召回，三元组和 slot 负责一致性；Vault 负责权限、加密、生命周期和最小披露。

一个可靠的 Agent 记忆系统不只是“能记住”，还必须做到“记得准、更新对、取得少、可纠正、可遗忘”。
