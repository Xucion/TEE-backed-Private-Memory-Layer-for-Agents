# Confidential Agent Memory Vault

一个具备隐私保护长期记忆能力的命令行对话 Agent 原型。项目把对话主流程、外部 LLM 调用和记忆抽取放在 `untrusted/`，把长期记忆的密钥管理、加密存储、向量检索、记忆生命周期管理和隐私最小化放在 `trusted/` vault 侧。

当前代码已经从早期的单一全局记忆文件推进到：

- per-user 记忆文件：`vault_data/{user_id}.memories.enc`
- per-user Fernet key 注入：通过安全信道进入 vault 进程内存
- 模拟 remote attestation：固定 measurement + 开发私钥签名 quote
- 应用层加密信道：X25519 派生会话密钥，AES-GCM 加密请求和响应
- 记忆生命周期：active、superseded、expired、forgotten
- 记忆管理命令：列出和遗忘指定 memory

注意：当前 secure channel 和 attestation 是开发原型，不是真实 SGX remote attestation。`gramine-direct` 可以托管 vault server，但不提供 SGX 硬件隔离。

## 当前状态

- Agent 仍是命令行应用，不是 HTTP API 服务。
- Agent 启动时会建立 vault 安全会话，并为当前 `VAULT_USER_ID` 注入 Fernet key。
- 默认使用 `secure_request` 访问 vault；旧版明文 `store` / `retrieve` 默认禁用。
- Vault 使用用户 key 加密对应用户的 `vault_data/{user_id}.memories.enc`。
- Vault 进程重启后，用户 key 需要重新注入。
- 没有设置 `USER_MEMORY_KEY` 时，客户端会生成临时 key；重启后无法读取旧记忆。
- 当前仍依赖 DashScope 的 Tongyi 模型和 `text-embedding-v4` embedding 服务。

## 主要功能

### 对话 Agent

- 使用 LangGraph 构建 `retrieve_memory -> chatbot` 两节点流程。
- 使用 `ChatTongyi` 调用通义千问模型。
- 维护当前 CLI 会话内的多轮历史。
- 每轮用户输入前通过安全 vault 检索长期记忆上下文。
- 每轮回复后异步抽取并存储用户长期记忆。
- Vault 不可用时自动降级为无长期记忆对话。

### 记忆抽取

- 只分析 `HumanMessage`，避免助手回复污染长期记忆。
- 调用外部 LLM 输出结构化 JSON。
- 支持字段包括：
  - `content`
  - `memory_type`
  - `sensitivity`
  - `subject`
  - `predicate`
  - `object`
  - `value`
  - `slot`
  - `confidence`
  - `source`
- 过滤问题、临时请求、元记忆查询和非用户来源记忆。
- 将第一人称表达规范化为“用户...”。

### 安全信道和模拟 RA

- 客户端通过 `handshake_start` 发起握手。
- Vault 返回模拟 quote、签名、`session_id` 和 vault X25519 公钥。
- 客户端用 `interface/sim_ra_public_key.pem` 验证 quote 签名。
- Vault 用 `trusted/sim_ra_private_key.pem` 签名 quote。
- 双方通过 X25519 计算共享秘密，并用 HKDF 派生 32 字节信道密钥。
- 后续 secure payload 使用 AES-GCM 加密，并把 `session_id` 作为 associated data。
- session 当前 TTL 为 300 秒。

这套流程只证明“对端持有当前项目里的模拟私钥”，不能证明运行在真实 SGX enclave 中。

### Vault Server

`trusted/vault_server.py` 提供本地 JSON line socket 服务，默认监听 `127.0.0.1:8765`。

支持的外层 action：

- `attest`
- `handshake_start`
- `secure_ping`
- `secure_provision_user_key`
- `secure_request`
- `store` 和 `retrieve`，仅在 `VAULT_ALLOW_LEGACY_PLAINTEXT=1|true|yes` 时允许

`secure_request` 支持的内部 action：

- `store`
- `retrieve`
- `list_memories`
- `forget`

### Per-User 记忆隔离

- `VAULT_USER_ID` 决定当前用户，默认是 `default_user`。
- `USER_MEMORY_KEY` 是当前用户的 Fernet key。
- Vault 内部使用 `trusted/user_key_manager.py` 将 `user_id -> user_key` 保存在进程内存。
- 每个用户对应独立密文文件：`vault_data/{user_id}.memories.enc`。
- 当前请求中的 `user_id` 会用于查询已注入的用户 key；后续仍需要把 user_id、session 和客户端身份做更强绑定。

### 记忆存储和检索

`trusted/memory_store.py` 负责：

- 使用用户 Fernet key 加密和解密 per-user memory 文件。
- 为新记忆生成 `id`、时间戳、状态字段、embedding、`fact_key` 和 `conflict_key`。
- 使用 DashScope `text-embedding-v4` 生成记忆 embedding。
- 根据 `fact_key` 合并重复事实。
- 根据 `slot` / `conflict_key` 将互斥旧事实标记为 `superseded`。
- 在同类记忆中用 embedding 相似度合并语义重复项。
- 在加载时把过期 active 记忆标记为 `expired`。
- 支持 soft delete，把 active 记忆标记为 `forgotten`。

`trusted/memory_retriever.py` 负责：

- 只检索 `active` 且有 embedding 的记忆。
- 只对 query 做一次 embedding。
- 使用 numpy 计算余弦相似度。
- 返回 top-k 且超过 threshold 的结果。
- 更新被召回记忆的 `last_accessed_at` 和 `access_count`。

### Context Minimizer

Vault 在返回给 Agent 前会做最小化处理：

- `sensitivity == "high"` 的记忆不返回原文，只返回类别级提示。
- 低敏记忆返回 `content`。
- 去重后拼接为 `memory_context`。

## 架构

```text
[untrusted / TEE 外部]                         [trusted / TEE 目标边界]

untrusted/chat_app.py
  |  建立模拟 RA + 安全信道
  |  注入 user memory key
  v
interface/vault_api.py  <------------------>  trusted/vault_server.py
  |  secure_request(store/retrieve/list/forget)      |
  |                                                  v
untrusted/memory_extractor.py                 trusted/user_key_manager.py
                                                   |
                                                   v
                                           trusted/memory_store.py
                                           trusted/memory_retriever.py
                                                   |
                                                   v
                                           vault_data/{user_id}.memories.enc
```

## 运行要求

- Python 3.10+
- 有效的 `DASHSCOPE_API_KEY`
- Gramine，用于 `gramine-direct` 原型运行
- 如需真实 SGX，需要可用的 Intel SGX 硬件和后续 `gramine-sgx` 配置

Python 依赖见 `requirements.txt`：

- `langgraph`
- `langchain-community`
- `dashscope`
- `numpy`
- `cryptography`

## 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置环境变量

必需：

```bash
export DASHSCOPE_API_KEY="your_dashscope_api_key"
```

推荐配置：

```bash
export VAULT_USER_ID="default_user"
export USER_MEMORY_KEY="a_fernet_key_for_this_user"
export TONGYI_MODEL="qwen-turbo"
export VAULT_HOST="127.0.0.1"
export VAULT_PORT="8765"
export VAULT_LOG_LEVEL="INFO"
export CHAT_LOG_LEVEL="INFO"
```

调试旧版明文 API 时才需要：

```bash
export VAULT_ALLOW_LEGACY_PLAINTEXT="1"
```

生成 Fernet key 的一种方式：

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

如果不设置 `USER_MEMORY_KEY`，程序会为本次运行生成临时 key；这种模式适合临时调试，但重启后无法解密旧记忆。

## 运行项目

### 方式一：直接运行 vault

终端 1：

```bash
python3 trusted/vault_server.py
```

终端 2：

```bash
python3 untrusted/chat_app.py
```

输入 `exit` 或 `quit` 退出聊天。

### 方式二：用 Gramine direct 启动 vault

生成 manifest：

```bash
gramine-manifest -Dproject_dir=$(pwd) trusted/vault.manifest.template trusted/vault.manifest
```

启动 vault：

```bash
gramine-direct trusted/vault
```

另开终端启动 agent：

```bash
python3 untrusted/chat_app.py
```

## 记忆管理

启动 vault 后，可以用管理命令查看或遗忘当前用户的记忆：

```bash
python3 untrusted/memory_management_commands.py
```

支持命令：

```text
/memories              List active memories
/memories all          List all memories
/memories <status>     List active, superseded, forgotten, or expired memories
/forgotten             List forgotten memories
/forget <id> [id...]   Soft-delete one or more memories
/help                  Show help
exit | quit            Exit
```

也可以直接解密查看当前用户记忆：

```bash
python3 decrypt.py <user_id> <fernet_key>
```

或：

```bash
export USER_ID="default_user"
export USER_MEMORY_KEY="a_fernet_key_for_this_user"
python3 decrypt.py
```

## 测试

当前有一个不依赖真实 embedding 服务的生命周期测试：

```bash
python3 tests/test_memory_lifecycle.py
```

它覆盖：

- 新记忆写入
- `fact_key` 合并
- embedding 语义重复合并
- slot 冲突替换为 `superseded`
- 过期标记为 `expired`
- active-only 检索
- 访问计数更新
- list 和 forget

## 项目文件

```text
common/
  sim_secure_channel.py        模拟安全信道工具：X25519、HKDF、AES-GCM、base64 编码

interface/
  vault_api.py                 untrusted 侧 vault API，包含模拟 RA、secure channel 和 secure request
  sim_ra_public_key.pem        客户端验证模拟 quote 的公钥

trusted/
  vault_server.py              vault socket server，处理 attestation、handshake 和 secure_request
  user_key_manager.py          vault 进程内 per-user key 管理
  memory_store.py              per-user 加密存储、合并、冲突、过期、遗忘
  memory_retriever.py          active 记忆向量检索
  vault.manifest.template      Gramine manifest 模板
  sim_ra_private_key.pem       模拟 RA 私钥，仅用于开发原型

untrusted/
  chat_app.py                  命令行对话 Agent
  memory_extractor.py          从用户原话抽取长期记忆
  memory_management_commands.py 记忆查看和遗忘命令

tests/
  test_memory_lifecycle.py     记忆生命周期测试

vault_data/
  {user_id}.memories.enc       per-user 加密记忆文件，运行后生成

decrypt.py                    调试用解密查看脚本
requirements.txt              Python 依赖
```

## Gramine Direct 状态

当前 manifest 模板会把项目目录、Python 环境、系统库和 `/tmp` 挂载进 Gramine，并把 `DASHSCOPE_API_KEY` 作为环境变量传入。

已支持：

1. 生成 `trusted/vault.manifest`
2. 通过 `gramine-direct trusted/vault` 启动 vault server
3. 从 untrusted agent 侧建立模拟 RA 安全信道并访问 vault

仍需改进：

- 收紧 manifest 中的挂载范围。
- 避免在生成后的 manifest 或运行环境中暴露真实 API key。
- 明确 `vault_data/` 在 Gramine direct 和未来 SGX 模式下的挂载策略。
- 在 SGX 硬件环境中迁移到 `gramine-sgx`。

## 安全边界和限制

- `gramine-direct` 不提供 SGX 硬件隔离。
- 当前 quote 是模拟 RA，不是真实 enclave quote。
- 模拟 RA 私钥在项目目录中，不能抵抗恶意宿主机。
- 当前安全信道是应用层协议原型，不等同于 RA-TLS。
- 用户 key 可以来自环境变量，仍然由 TEE 外部提供。
- Vault 重启会丢失内存中的 user key，需要重新注入。
- DashScope LLM 和 embedding 服务仍会看到发送给它们的文本。
- 还没有真实用户认证、KMS、密钥恢复、密钥轮换和密钥吊销。
- Agent 仍是 CLI，不是服务化 HTTP API。

## 后续工作

- 将模拟 RA 替换为真实 SGX remote attestation 或 RA-TLS。
- 将用户 key 注入迁移到 attestation-based secret provisioning。
- 评估 SGX sealing 或 Gramine encrypted files 作为本地 key 保护方案。
- 收紧 Gramine manifest 文件挂载和环境变量暴露。
- 设计正式的用户认证、session 绑定和授权模型。
- 评估 DashScope embedding 是否符合最终威胁模型；必要时改为本地 embedding 或更严格的数据最小化。
- 将 CLI agent 封装为 HTTP API 或服务端接口。
