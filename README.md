# LangGraph Simple Chat

一个基于 Python、LangGraph 和阿里云 Tongyi 的命令行多轮聊天示例。项目在基础聊天之外，还包含“长期记忆提取 + 本地加密存储”的演示流程。

## 项目描述

这个项目展示了一个简化版的智能体工作流：用户输入先进入 LangGraph 图，图中会先执行记忆检索节点，再调用 Tongyi 模型生成回复。每轮对话结束后，程序还会异步调用另一个模型提取值得长期保存的用户信息，并将这些记忆加密后写入本地文件。

当前实现更偏向教学和原型验证，适合用来理解以下几件事：

- 如何用 LangGraph 组织多节点聊天流程
- 如何在聊天阶段向模型注入额外上下文
- 如何从对话中抽取结构化记忆
- 如何把记忆以加密形式保存在本地

## 当前功能

- 使用 LangGraph 构建两节点聊天流程
- 使用 `ChatTongyi` 调用阿里云 Tongyi 模型
- 支持命令行多轮对话
- 根据当前输入做简单关键词记忆检索
- 将命中的记忆上下文作为系统消息注入模型
- 对每轮对话异步提取长期记忆，不阻塞聊天
- 使用 Fernet 对本地记忆文件进行加密存储
- 提供解密查看工具脚本

## 工作流程

### 1. 聊天主流程

`chat_app.py` 是程序入口，主要负责：

- 校验 `DASHSCOPE_API_KEY`
- 构建 LangGraph 对话图
- 维护当前会话的消息历史
- 接收用户输入并输出模型回复
- 在每轮结束后启动后台线程提取并存储记忆

图中的两个节点分别是：

- `retrieve_memory`：根据本轮输入做简单关键词匹配，生成 `memory_context`
- `chatbot`：把历史消息和记忆上下文一起发给 Tongyi 生成回答

### 2. 记忆提取

`memory_extractor.py` 会把一轮对话内容发送给 Tongyi，并要求模型按 JSON 格式返回值得长期保存的用户信息。每条记忆包含：

- `content`
- `category`
- `sensitivity`

如果模型返回的内容不是合法 JSON，当前实现会打印错误并忽略这一轮提取结果。

### 3. 记忆存储

`memory_store.py` 负责把提取到的记忆保存在本地：

- 使用 `memory.key` 保存 Fernet 密钥
- 使用 `memories.enc` 保存加密后的记忆数据
- 为每条新记忆自动补充 `id` 和 `created_at`
- 支持读取全部已存储记忆

### 4. 解密查看

`decrypt.py` 会调用 `load_all_memories()`，把本地已存储的记忆解密后打印成格式化 JSON，便于调试和查看。

## 当前记忆行为

当前聊天阶段的“记忆召回”仍然是演示版逻辑，基于关键词匹配：

- 输入里包含“饮食”或“吃”时，注入饮食限制上下文
- 输入里包含“会议”或“日程”时，注入上午安排会议偏好
- 其他输入不注入额外记忆

注意：当前代码里虽然定义了示例 `memories` 列表，但聊天时并没有真正从 `memories.enc` 中读取并召回历史记忆。

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

查看已加密存储的记忆内容：

```bash
python3 decrypt.py
```

输入 `exit` 或 `quit` 可以退出聊天。

## 项目文件

- `chat_app.py`：聊天主程序，包含 LangGraph 工作流和异步记忆提取触发逻辑
- `memory_extractor.py`：调用 Tongyi 提取结构化长期记忆
- `memory_store.py`：负责记忆的加密读写
- `decrypt.py`：解密并打印当前本地记忆
- `memories.enc`：加密后的记忆数据文件
- `memory.key`：用于加解密记忆文件的本地密钥
- `requirements.txt`：Python 依赖
- `README.md`：项目说明文档

## 说明

当前项目是一个原型示例，已经具备聊天、记忆提取和加密存储能力，但“聊天时使用真实历史记忆召回”这一部分还没有完全接到本地记忆库上。如果后续继续扩展，可以优先把检索逻辑从硬编码关键词升级为从加密记忆库中读取和筛选。
