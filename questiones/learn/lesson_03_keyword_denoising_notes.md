# Lesson 03：关键词提取与去噪

## 学习目标

把带有礼貌、情绪和冗余表达的查询：

```text
麻烦帮我认真看看公司报销到底需要准备哪些材料，谢谢
```

处理为：

```text
cleaned_query：公司报销需要准备哪些材料
keywords：公司报销、材料
```

这里不是简单删除停用词。系统必须保留否定、时间、版本、数字和过滤条件：

```text
请帮我找最近 30 天 Python 3.11 中不使用 Redis 的缓存实现
```

去噪后仍应包含：

```text
最近 30 天 Python 3.11 中不使用 Redis 的缓存实现
```

如果错误删除“不”，检索意图会完全相反。

## 输出字段

- `cleaned_query`：适合向量检索的自然语言查询。
- `keywords`：适合 BM25、倒排索引或混合检索的关键词。
- `removed_noise`：模型实际删除的片段，用于调试和审计。
- `preserved_constraints`：时间、否定、版本等关键条件。
- `confidence`：模型对本次处理的置信度。

`keywords` 没有做同义词扩展。关键词提取与同义词扩展是两个不同步骤，
混在一起会难以判断某个词来自用户还是模型推测。

## 运行真实 Tongyi

```powershell
$env:DASHSCOPE_API_KEY="你的 API Key"
python questiones/learn/lesson_03_keyword_extraction_and_denoising.py
```

指定查询：

```powershell
python questiones/learn/lesson_03_keyword_extraction_and_denoising.py `
  --query "请帮我找最近 30 天 Python 3.11 中不使用 Redis 的缓存实现"
```

## 运行离线测试

```powershell
python -m unittest discover questiones/learn `
  -p "test_lesson_03_keyword_extraction_and_denoising.py" -v
```

## 在混合检索中的使用

```text
原始 Query
    |
    v
关键词提取与去噪
    |
    +-- cleaned_query --> Embedding 向量检索
    |
    +-- keywords ------> BM25 / 倒排索引
    |
    v
合并结果并 Rerank
```

生产环境还应保留原始 Query 参与召回，并对否定、数字和版本条件增加
确定性校验，避免完全依赖 LLM 判断。
