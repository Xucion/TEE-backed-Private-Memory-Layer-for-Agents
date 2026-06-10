# Lesson 02：指代消解与上下文补全

## 学习目标

把依赖上下文的问题：

```text
历史：请介绍一下 Redis。
当前问题：它支持什么持久化方式？
```

改写成可以独立进入搜索或向量检索的问题：

```text
Redis 支持什么持久化方式？
```

## 代码结构

`lesson_02_coreference_query_rewrite.py` 分为四层：

1. `format_history`：清洗并限制发送给 LLM 的历史消息。
2. `build_rewrite_prompt`：明确告诉模型只改写、不回答、不猜测。
3. `parse_rewrite_response`：解析并校验固定 JSON。
4. `rewrite_query`：调用 Tongyi 并返回 `RewriteResult`。

不要直接信任模型返回的字符串。固定结构可以让后续流程判断：

- 是否使用了历史；
- 具体消解了哪些指代；
- 是否存在歧义；
- 应该检索还是向用户追问。

## 运行真实 Tongyi

PowerShell：

```powershell
$env:DASHSCOPE_API_KEY="你的 API Key"
python questiones/learn/lesson_02_coreference_query_rewrite.py
```

也可以传入另一个问题：

```powershell
python questiones/learn/lesson_02_coreference_query_rewrite.py `
  --query "那它和 MySQL 相比呢？"
```

## 运行离线测试

测试使用 `FakeTongyi`，不会访问网络，也不会消耗 Token：

```powershell
python -m unittest discover questiones/learn `
  -p "test_lesson_02_coreference_query_rewrite.py"
```

## 在 RAG 中的接入位置

```text
对话历史 + 当前问题
        |
        v
Query Rewrite
        |
        +-- needs_clarification=true --> 向用户追问
        |
        +-- standalone_query ----------> embedding / BM25 检索
```

生产系统通常还会保留原始 Query，使用“原始 Query + 改写 Query”双路召回，
防止错误改写导致相关文档完全丢失。
