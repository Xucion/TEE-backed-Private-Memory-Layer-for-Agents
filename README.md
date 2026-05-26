# LangGraph Tongyi Memory Chat

一个基于 Python、LangGraph 和阿里云 Tongyi 的命令行多轮聊天示例。项目包含聊天、长期记忆抽取、本地加密存储、embedding 检索召回，以及基础的相似记忆去重逻辑。

## 项目描述

这个项目展示了一个简化版的带记忆智能体工作流：用户输入进入 LangGraph 图后，系统先从本地加密记忆库中检索相关记忆，再把经过隐私最小化处理的上下文注入给 Tongyi 生成回复。每轮对话结束后，程序会异步从用户原话中抽取长期记忆，并将记忆加密保存到本地。

当前实现更适合学习和原型验证，用来观察一个记忆系统从“抽取、存储、召回、注入”到“问题复盘”的完整闭环。

## 当前功能

- 使用 LangGraph 构建两节点聊天流程：`retrieve_memory` -> `chatbot`
- 使用 `ChatTongyi` 调用阿里云 Tongyi 模型
- 支持命令行多轮对话
- 使用 DashScope `text-embedding-v4` 为记忆生成向量
- 从 `memories.enc` 中加载加密记忆并做向量相似检索
- 对高敏感记忆做隐私最小化，只注入类别级提示
- 对低敏感记忆注入原始记忆内容
- 每轮对话后异步抽取长期记忆，不阻塞聊天
- 只从用户原话抽取记忆，避免助手回复污染长期记忆
- 使用 Fernet 加密本地记忆文件
- 写入前使用 embedding 相似度做临时重复检测，当前阈值为 `0.8`
- 提供 `decrypt.py` 查看已存储记忆

## 工作流程

### 1. 聊天主流程

`chat_app.py` 是程序入口，主要负责：

- 校验 `DASHSCOPE_API_KEY`
- 构建 LangGraph 对话图
- 维护当前会话的消息历史
- 接收用户输入并输出 Tongyi 回复
- 在每轮结束后启动后台线程提取并存储记忆

LangGraph 中有两个节点：

- `retrieve_memory`：根据当前用户输入，从本地记忆库中检索 top 3 相关记忆，检索阈值为 `0.4`
- `chatbot`：把历史消息和检索出的记忆上下文一起发给 Tongyi 生成回答

### 2. 记忆召回与隐私最小化

`memory_retriever.py` 会从 `memories.enc` 加载全部记忆，读取已保存的 embedding，并对当前 query 生成 embedding 后计算相似度。

召回结果会在 `chat_app.py` 中进一步处理：

- 如果记忆敏感度是 `high`，不会直接注入原始内容，而是注入类别级提示
- 如果记忆敏感度是 `low`，会注入原始记忆内容

例如，高敏感健康记忆会被最小化成：

```text
用户有健康方面的限制，请给出保守建议。
```

### 3. 记忆抽取

`memory_extractor.py` 会把本轮用户原话发送给 Tongyi，并要求模型按 JSON 格式返回值得长期保存的用户信息。

每条记忆包含：

- `content`
- `category`
- `sensitivity`
- `source`

抽取后会做基础校验：

- 只接受 `source == "user"` 的记忆
- 只保留非空 `content`
- 将非法 `category` 归为 `other`
- 将非法 `sensitivity` 归为 `low`

这能避免把助手建议、模型推断或临时任务直接写入长期记忆。

### 4. 记忆存储

`memory_store.py` 负责本地加密存储：

- `memory.key` 保存 Fernet 加解密密钥
- `memories.enc` 保存加密后的记忆数组
- 新记忆会补充 `id`、`created_at`、`embedding` 和 `embedding_model`
- embedding 模型当前为 DashScope `text-embedding-v4`
- 写入前会用 embedding 相似度检测重复，当前阈值为 `0.8`

注意：当前去重仍是临时方案，依赖 embedding 相似度，不能稳定识别所有同义或近义事实。

### 5. 解密查看

`decrypt.py` 会调用 `load_all_memories()`，解密本地记忆并打印：

```text
[1] 用户有糖尿病 | 分类: health | 敏感度: high
```

## 运行要求

- Python 3.10+
- 有效的阿里云 DashScope API Key

## 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置环境变量

```bash
export DASHSCOPE_API_KEY="your_api_key"
export TONGYI_MODEL="qwen-turbo"
```

`DASHSCOPE_API_KEY` 是必需的。

`TONGYI_MODEL` 是可选的；如果不设置，默认使用 `qwen-turbo`。

## 运行项目

启动聊天：

```bash
python3 chat_app.py
```

查看已加密存储的记忆：

```bash
python3 decrypt.py
```

输入 `exit` 或 `quit` 可以退出聊天。

## 项目文件

- `chat_app.py`：聊天主程序，包含 LangGraph 工作流、记忆召回、隐私最小化和异步记忆抽取触发逻辑
- `memory_extractor.py`：调用 Tongyi 从用户原话中提取结构化长期记忆
- `memory_store.py`：负责记忆加密读写、embedding 生成和临时相似去重
- `memory_retriever.py`：负责从加密记忆库中加载记忆并做向量相似检索
- `decrypt.py`：解密并打印当前本地记忆
- `memories.enc`：加密后的记忆数据文件，本地运行后生成
- `memory.key`：用于加解密记忆文件的本地密钥，本地运行后生成
- `requirements.txt`：Python 依赖
- `Problem1.md`：记录助手回复污染长期记忆的问题及解决方案
- `unsolvedProblem2.md`：记录相似记忆重复存储的问题及后续解决思路

## 已知限制

- 当前相似去重依赖 embedding 阈值 `0.8`，可能漏掉同义表达，也可能误合并相关但不同的记忆
- 还没有 `canonical_content`、`fact_key` 或实体关系结构，因此无法稳定判断两条记忆是否是同一事实
- 已经写入 `memories.enc` 的旧脏数据不会自动清理
- 后台异步写入没有加文件锁，快速连续写入时后续需要考虑并发安全
- `memory_store.py` 当前会打印最大相似度，适合调试，但正式使用时可以改成更清晰的日志

## 后续优化方向

- 增加 `canonical_content` 和 `fact_key`，例如把“糖尿疾病”归一到“糖尿病”
- 写入重复记忆时合并 metadata，而不是简单跳过或追加
- 为高敏感记忆增加用户确认流程
- 增加历史记忆清理脚本，合并旧重复数据并删除污染数据
- 为加密文件读写增加锁，避免异步线程并发写入问题
