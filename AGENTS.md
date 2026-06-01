## 项目背景与当前状态

### 项目目标

构建一个具备**隐私保护长期记忆**能力的对话 Agent。核心思想是将用户的长期记忆存储与检索逻辑放入**可信执行环境（TEE）**，降低敏感用户画像被长期攻击窃取的风险。

项目名称：**Confidential Agent Memory Vault**。

---

### 当前总体进度

Agent 主流程已经基本完成，项目已经从“单体本地记忆原型”推进到“Gramine direct 原型验证完成”阶段：

- `untrusted/`：运行在 TEE 外部，负责对话、记忆抽取、调用外部 LLM。
- `interface/`：TEE 外部访问 vault 的统一 API 层，屏蔽 socket 通信细节。
- `trusted/`：计划运行在 TEE 内部，负责记忆存储、检索、隐私最小化和 vault socket 服务。

目前代码结构已经具备 TEE 化接口边界，`trusted/vault_server.py` 已经可以通过 Gramine direct 启动。由于当前没有 SGX 硬件环境，项目暂时停留在 `gramine-direct` 阶段；`gramine-sgx`、SGX sealing 和 remote attestation 作为后续工作。

---

### 已完成的工作

#### 1. 基础对话框架（`untrusted/chat_app.py`）

- 基于 **LangGraph** 构建对话图，节点为 `retrieve_memory` 和 `chatbot`。
- 使用**通义千问**（`ChatTongyi` / `DASHSCOPE_API_KEY`）作为对话模型。
- 手动维护 `history` 列表保持多轮对话连贯性。
- 每轮对话前通过 `interface.vault_api.retrieve_context()` 检索长期记忆上下文。
- 每轮对话结束后异步触发记忆提取和存储，不阻塞主流程。
- 存储路径已经统一改为 `interface.vault_api.store_memories()`，不再由 agent 直接访问本地记忆文件。

#### 2. 记忆提取器（`untrusted/memory_extractor.py`）——TEE 外部

- 调用外部 LLM，只分析**用户原话**，过滤 AI 回复。
- 提取客观事实或明确长期偏好。
- 输出结构化 JSON，字段包括 `content`、`memory_type`（`preference` / `profile` / `health` / `project` / `instruction` / `other`）、`sensitivity`（`high` / `low`）、`source`。
- 包含输出校验（`_normalize_memories`），过滤格式错误、临时请求、提问句和非用户来源记忆。
- 规范化第一人称表述，例如将“我喜欢喝粥”转换为“用户喜欢喝粥”。

#### 3. Vault API（`interface/vault_api.py`）——TEE 外部接口层

- 提供 `store_memories()` 和 `retrieve_context()` 两个统一入口。
- 使用本地 socket 与 vault server 通信。
- 支持 `VAULT_HOST`、`VAULT_PORT` 环境变量配置。
- 对响应大小、JSON 格式、错误状态和返回字段做基础校验。
- 向上抛出 `VaultApiError`，使 agent 在 vault 不可用时可以降级为无记忆对话。

#### 4. Vault Server（`trusted/vault_server.py`）——计划运行在 TEE 内部

- 已实现 socket server，暴露 `store` 和 `retrieve` 两个操作。
- 对请求大小、动作类型、记忆字段、`top_k` 和 `threshold` 做基础校验。
- `store` 路径调用 `trusted.memory_store.store_memories()`。
- `retrieve` 路径调用 `trusted.memory_retriever.retrieve()`。
- 在 vault 内部执行 Context Minimizer：高敏感记忆只返回类别级提示，低敏感记忆可返回原始内容。
- 使用 `_STORE_LOCK` 保护同一进程内的存储和检索临界区。
- 已通过 `gramine-direct trusted/vault` 成功启动，完成 Gramine direct 运行验证。

#### 5. Gramine Direct 接入

- 已编写 `trusted/vault.manifest.template`。
- 可通过 `gramine-manifest` 生成 `trusted/vault.manifest`。
- 当前启动命令：

```bash
gramine-manifest -Dproject_dir=$(pwd) trusted/vault.manifest.template trusted/vault.manifest
gramine-direct trusted/vault
```

- 该阶段验证 vault server 能被 Gramine LibOS 托管运行。
- 由于缺少 SGX 硬件，当前不继续推进 `gramine-sgx` 实测。

#### 6. 记忆存储（`trusted/memory_store.py`）——已进入 Gramine direct 运行边界

- 使用 **Fernet 对称加密**将记忆加密存储到 `memories.enc`。
- 当前密钥文件为 `memory.key`，后续迁入 SGX 时需要改为 sealing / encrypted FS / attestation provisioning 方案。
- 存储时调用 **DashScope `text-embedding-v4`** 将 `content` 向量化后一并存入，避免检索时重复编码记忆内容。
- 使用 numpy 计算向量相似度做去重，当前阈值为 `0.8`。
- 记录 `embedding_model` 字段，便于将来换模型时重新编码。

#### 7. 记忆检索器（`trusted/memory_retriever.py`）——已进入 Gramine direct 运行边界

- 从加密存储中读取记忆，直接复用存储的向量。
- 只对 query 做一次 embedding 编码。
- 使用纯 numpy 余弦相似度计算，不依赖 FAISS，适配 TEE 有限内存。
- 返回去除 `embedding` 字段后的召回结果，并附带相似度分数。

---

### 当前架构的信任边界

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

---

### 威胁模型

| 攻击场景 | 当前应对方式 | 状态 |
|---|---|---|
| 长期攻击窃取用户画像 | 记忆存储、检索和最小化逻辑已进入 Gramine direct 运行边界 | direct 已跑通，SGX 待硬件环境 |
| 宿主机读取记忆文件 | Fernet 加密 `memories.enc` | 原型可用，但 `memory.key` 仍需 TEE 保护 |
| 单次对话明文拦截 | 目前接受该风险，认为时间窗口短、信息碎片化 | 已纳入模型 |
| LLM 回复反射隐私 | vault server 内部执行 Context Minimizer，高敏感信息不原文出 TEE | 已实现 |
| 助手回复污染长期记忆 | extractor 只分析用户原话，并校验 `source == "user"` | 已实现 |
| 伪造记忆注入 | 计划通过 Remote Attestation 验证调用方/Extractor 来源 | 待实现 |
| 外部 embedding 服务看到敏感文本 | 当前 store/retrieve 会调用 DashScope embedding | 需根据威胁模型进一步评估 |

---

### 当前目录结构

```text
project/
├── untrusted/
│   ├── chat_app.py
│   └── memory_extractor.py
├── trusted/
│   ├── memory_store.py
│   ├── memory_retriever.py
│   ├── vault_server.py
│   ├── vault.manifest.template
│   └── vault.manifest
├── interface/
│   └── vault_api.py
├── decrypt.py
├── memories.enc
├── memory.key
├── requirements.txt
└── README.md
```

---

### 下一步目标

当前阶段已经完成 Gramine direct 原型验证。由于没有 SGX 硬件，下一阶段重点是整理 direct 阶段成果，并为未来 SGX 环境预留安全设计。

具体任务：

1. 记录并维护 Gramine direct 启动流程。
2. 明确 Gramine 内的文件挂载策略，尤其是 `memories.enc` 和 `memory.key` 的位置。
3. 收紧 manifest 中的文件挂载和依赖范围。
4. 避免将真实 API key 写入生成后的 manifest。
5. 在具备 SGX 硬件后切换到 `gramine-sgx`，验证 vault server 在 SGX enclave 内运行。
6. 将长期密钥从普通文件方案迁移到 Gramine encrypted files、SGX sealing 或 attestation secret provisioning。
7. 设计 Remote Attestation 流程，后续用于验证 vault 身份和密钥注入。
8. 评估 DashScope embedding 在 TEE 内调用是否符合最终威胁模型；如不符合，考虑本地 embedding 或更严格的数据最小化。

---

### 技术栈

- 对话框架：LangGraph + LangChain
- LLM / Embedding：通义千问 / DashScope API
- 加密：Python `cryptography`（Fernet，当前原型）
- TEE：Gramine（开发阶段 `gramine-direct`，目标 `gramine-sgx`）
- 向量计算：numpy（无 FAISS 依赖，适配 TEE 内存限制）
