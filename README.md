# Confidential Agent Memory Vault

一个具备隐私保护长期记忆能力的命令行对话 Agent 原型。项目基于 Python、LangGraph、LangChain、通义千问和 Gramine 构建，对话 Agent 运行在 TEE 外部，长期记忆的存储、检索和隐私最小化逻辑已经拆分到 `trusted/` vault 侧。

当前项目已经完成 agent/vault 边界拆分，并已在 `gramine-direct` 模式下成功启动 vault server。由于当前没有 SGX 硬件环境，项目暂时停留在 Gramine direct 原型验证阶段；`gramine-sgx`、SGX sealing、remote attestation 和生产级密钥注入作为后续工作。

## 当前状态

- Agent 主流程基本完成：支持多轮对话、记忆召回、记忆抽取和异步存储。
- `untrusted/` 负责对话和记忆抽取，不直接访问本地记忆存储。
- `interface/` 提供 vault API，通过 socket 调用 trusted vault。
- `trusted/` 包含 vault server、加密存储、向量检索和 Context Minimizer。
- `trusted/vault.manifest.template` 和生成后的 `trusted/vault.manifest` 已存在。
- vault server 已经可以通过 `gramine-direct trusted/vault` 启动。
- 当前没有 SGX 硬件环境，因此暂不进入 `gramine-sgx` 阶段。

## 当前功能

- 使用 LangGraph 构建两节点聊天流程：`retrieve_memory` -> `chatbot`
- 使用 `ChatTongyi` 调用阿里云通义千问模型
- 每轮对话前通过 `interface.vault_api.retrieve_context()` 从 vault 检索记忆上下文
- 每轮对话后异步调用 extractor 抽取长期记忆，再通过 `interface.vault_api.store_memories()` 写入 vault
- 只从用户原话抽取记忆，避免助手回复污染长期记忆
- 使用 DashScope `text-embedding-v4` 为记忆和 query 生成向量
- 使用 Fernet 加密 `memories.enc`
- 使用 numpy 做向量相似度检索，不依赖 FAISS
- 对高敏感记忆做隐私最小化，只返回类别级提示
- 对低敏感记忆返回原始记忆内容
- 写入前使用 embedding 相似度做临时去重，当前阈值为 `0.8`

## 架构

```text
[TEE 外部 / untrusted]                 [TEE 内部 / trusted 目标]

untrusted/chat_app.py                  trusted/vault_server.py
untrusted/memory_extractor.py          trusted/memory_store.py
interface/vault_api.py                 trusted/memory_retriever.py
        │                                      │
        │  store: 结构化记忆                   │
        │ ───────────────────────────────────> │  加密存储 / 向量去重
        │                                      │
        │  retrieve: 当前 query                │
        │ ───────────────────────────────────> │  向量检索 / Context Minimizer
        │                                      │
        │  脱敏后的 memory_context             │
        │ <─────────────────────────────────── │
        │
        ▼
组装 prompt → 外部 LLM API → 返回回复给用户
```

注意：当前 vault 已经可以由 Gramine direct 托管运行，但 direct 模式不提供 SGX 硬件隔离。`trusted/` 目前代表代码和运行边界，尚不代表真实 SGX 机密性保护。

## 工作流程

### 1. 聊天主流程

`untrusted/chat_app.py` 是 agent 入口，主要负责：

- 校验 `DASHSCOPE_API_KEY`
- 构建 LangGraph 对话图
- 维护当前会话的消息历史
- 接收用户输入并输出 Tongyi 回复
- 调用 `retrieve_context()` 获取经过隐私最小化处理的长期记忆
- 在每轮结束后启动后台线程提取并存储记忆

LangGraph 中有两个节点：

- `retrieve_memory`：根据当前用户输入，从 vault 检索 top 3 相关记忆，检索阈值为 `0.4`
- `chatbot`：把历史消息和检索出的记忆上下文一起发给 Tongyi 生成回答

### 2. Vault API

`interface/vault_api.py` 是 TEE 外部访问 vault 的统一入口：

- `store_memories(memories)`：发送 `store` 请求
- `retrieve_context(query, top_k, threshold)`：发送 `retrieve` 请求
- 默认连接 `127.0.0.1:8765`
- 可通过 `VAULT_HOST` 和 `VAULT_PORT` 修改 vault 地址
- vault 不可用或响应异常时抛出 `VaultApiError`

### 3. Vault Server

`trusted/vault_server.py` 是计划放入 TEE 的 socket 服务：

- 接收 JSON line 格式请求
- 支持 `store` 和 `retrieve`
- 对请求字段做基础校验
- 调用 `trusted.memory_store` 完成加密存储和去重
- 调用 `trusted.memory_retriever` 完成向量召回
- 在返回 TEE 外部前执行 Context Minimizer

### 4. 记忆抽取

`untrusted/memory_extractor.py` 会把本轮用户原话发送给 Tongyi，并要求模型按 JSON 格式返回值得长期保存的用户信息。

每条记忆包含：

- `content`
- `category`
- `sensitivity`
- `source`

抽取后会做基础校验和规范化：

- 只接受 `source == "user"` 的记忆
- 只保留非空 `content`
- 将非法 `category` 归为 `other`
- 将非法 `sensitivity` 归为 `low`
- 过滤提问、临时请求和元记忆查询
- 将第一人称表达规范化为“用户...”形式

### 5. 记忆存储和检索

`trusted/memory_store.py` 当前负责：

- 使用 `memory.key` 加解密 `memories.enc`
- 为新记忆补充 `id`、`created_at`、`embedding` 和 `embedding_model`
- 使用 DashScope `text-embedding-v4` 编码记忆内容
- 写入前用 embedding 相似度检测重复

`trusted/memory_retriever.py` 当前负责：

- 从 `memories.enc` 加载全部记忆
- 复用已保存的记忆 embedding
- 只对当前 query 做一次 embedding 编码
- 使用 numpy 计算余弦相似度并返回 top-k 结果

## 运行要求

- Python 3.10+
- 有效的阿里云 DashScope API Key
- Gramine，用于运行 `gramine-direct`
- 如需进入 SGX 阶段，需要可用的 Intel SGX 硬件环境

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
export VAULT_HOST="127.0.0.1"
export VAULT_PORT="8765"
```

`DASHSCOPE_API_KEY` 是必需的。`TONGYI_MODEL`、`VAULT_HOST` 和 `VAULT_PORT` 是可选的。

## 运行项目

当前推荐用 Gramine direct 启动 vault server，再启动聊天程序。

生成 manifest：

```bash
gramine-manifest -Dproject_dir=$(pwd) trusted/vault.manifest.template trusted/vault.manifest
```

通过 Gramine direct 启动 vault：

```bash
gramine-direct trusted/vault
```

另开一个终端启动聊天：

```bash
python3 untrusted/chat_app.py
```

如果只是调试 Python 逻辑，也可以不经过 Gramine，直接启动 vault：

```bash
python3 trusted/vault_server.py
```

输入 `exit` 或 `quit` 可以退出聊天。

查看已加密存储的记忆：

```bash
python3 decrypt.py
```

## 项目文件

- `untrusted/chat_app.py`：聊天主程序，包含 LangGraph 工作流、记忆召回和异步记忆抽取触发逻辑
- `untrusted/memory_extractor.py`：调用 Tongyi 从用户原话中提取结构化长期记忆
- `interface/vault_api.py`：TEE 外部访问 vault 的 socket API 封装
- `trusted/vault_server.py`：计划运行在 TEE 内部的 vault socket 服务
- `trusted/memory_store.py`：负责记忆加密读写、embedding 生成和相似去重
- `trusted/memory_retriever.py`：负责从加密记忆库中加载记忆并做向量相似检索
- `trusted/vault.manifest.template`：Gramine manifest 模板
- `trusted/vault.manifest`：由 `gramine-manifest` 生成的 Gramine manifest
- `decrypt.py`：调试用解密查看脚本
- `memories.enc`：加密后的记忆数据文件，本地运行后生成
- `memory.key`：当前 Fernet 密钥文件，后续需要迁移到 TEE 保护方案
- `requirements.txt`：Python 依赖
- `Problem1.md`：记录助手回复污染长期记忆的问题及解决方案
- `unsolvedProblem2.md`：记录相似记忆重复存储的问题及后续解决思路

## Gramine Direct 状态

当前已经完成：

1. 编写 `trusted/vault.manifest.template`
2. 使用 `gramine-manifest` 生成 `trusted/vault.manifest`
3. 使用 `gramine-direct trusted/vault` 启动 vault server
4. 通过 `interface/vault_api.py` 从 untrusted agent 侧访问 vault

这一步验证了 vault server 可以被 Gramine LibOS 托管运行。由于当前没有 SGX 硬件环境，后续 SGX 相关能力暂时保留为 future work。

## 后续 TEE 工作

1. 明确 Gramine 内的文件挂载策略，尤其是 `memories.enc` 和 `memory.key` 的位置
2. 收紧 manifest 中的文件挂载和依赖范围
3. 避免将真实 API key 写入生成后的 manifest
4. 在具备 SGX 硬件后切换到 `gramine-sgx`
5. 将密钥保护从普通文件升级为 Gramine encrypted files、SGX sealing 或 remote attestation secret provisioning
6. 增加 remote attestation，用于验证 vault 身份和安全注入密钥

## 已知限制

- 当前只跑通了 `gramine-direct`，尚未在 `gramine-sgx` 中运行
- Gramine direct 不提供 SGX 硬件隔离，不能代表最终 TEE 安全性
- `memory.key` 仍是普通文件，不能代表生产级 TEE 密钥保护
- 当前 DashScope embedding 在 vault 内调用，敏感文本会发送给外部 embedding 服务，需要根据最终威胁模型评估
- 当前相似去重依赖 embedding 阈值 `0.8`，可能漏掉同义表达，也可能误合并相关但不同的记忆
- 还没有 `canonical_content`、`fact_key` 或实体关系结构，因此无法稳定判断两条记忆是否是同一事实
- 已经写入 `memories.enc` 的旧脏数据不会自动清理
- `memory_store.py` 当前会打印最大相似度，适合调试，但正式使用时可以改成更清晰的日志

## 后续优化方向

- 增加 `canonical_content` 和 `fact_key`
- 写入重复记忆时合并 metadata，而不是简单跳过或追加
- 为高敏感记忆增加用户确认流程
- 增加历史记忆清理脚本，合并旧重复数据并删除污染数据
- 为 vault 通信增加调用方认证
- 在具备 SGX 硬件后验证 `gramine-sgx`、SGX sealing 和 remote attestation
