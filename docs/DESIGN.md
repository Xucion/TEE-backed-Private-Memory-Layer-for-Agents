# Confidential Agent Memory Vault Design

本文档记录当前系统的可信边界、安全假设、原型限制和后续 SGX 迁移方向。当前项目已经完成 Gramine direct 原型验证，并加入了模拟 RA、安全信道、per-user key 注入和用户隔离记忆文件；但 direct 模式和模拟 RA 不提供真实硬件级机密性。

## 1. 设计目标

本项目希望构建一个具备隐私保护长期记忆能力的对话 Agent。核心目标是：

- 将长期记忆的存储、检索、去重和隐私最小化逻辑放入 vault。
- 将 vault 作为未来 TEE 内部组件，降低宿主机长期窃取用户画像的风险。
- 让 untrusted agent 只能通过受控接口访问记忆，而不能直接读取完整记忆库。
- 对高敏感记忆做最小化输出，避免把敏感原文直接注入外部 LLM prompt。
- 为未来 SGX sealing、remote attestation 和 RA-TLS 预留协议边界。

当前阶段的准确定位是：

```text
可运行的 confidential memory vault 原型
而不是已经具备真实 SGX 安全保证的生产系统
```

## 2. 当前组件边界

```text
[TEE 外部 / untrusted 目标]

src/untrusted/chat_app.py
  - 维护对话流程
  - 调用外部聊天模型
  - 调用 memory_extractor
  - 通过 src/interface/vault_api.py 访问 vault

src/untrusted/memory_extractor.py
  - 调用外部 LLM
  - 只从用户原话提取长期记忆候选
  - 输出结构化 memory JSON

src/interface/vault_api.py
  - 建立模拟 RA + secure channel
  - 注入 user key
  - 发送 store/retrieve 请求


[TEE 内部 / trusted 目标]

src/trusted/vault_server.py
  - 接收 socket 请求
  - 处理模拟 RA、握手、安全请求
  - 校验 store/retrieve 请求
  - 执行 Context Minimizer

src/trusted/user_key_manager.py
  - 维护进程内 per-user key
  - 校验 user_id 和 Fernet key 格式

src/trusted/memory_store.py
  - 按 user_id 加密保存记忆
  - 调用 embedding 模型生成记忆向量
  - 执行相似度去重

src/trusted/memory_retriever.py
  - 解密用户记忆
  - 调用 embedding 模型编码 query
  - 执行向量检索
```

当前信任边界是代码结构上的边界，不等同于真实硬件隔离边界。只有在未来进入 `gramine-sgx` 并完成真实 remote attestation 后，`src/trusted/` 才能代表具备硬件保护的执行边界。

## 3. 当前可信边界

当前项目中的可信边界可以分为三层。

### 3.1 逻辑可信边界

`src/trusted/` 被设计为 vault 侧可信逻辑，负责长期记忆的核心处理。`src/untrusted/` 不直接读写记忆文件，只能通过 `src/interface/vault_api.py` 调用 vault。

这已经建立了良好的软件架构边界：

- agent 不直接打开记忆文件。
- 记忆存储和检索集中在 vault。
- 隐私最小化在 vault 内完成。
- 高敏感记忆默认不原文返回给 agent。

但这只是逻辑边界。宿主机进程、调试者或文件系统权限较高的攻击者仍可能观察或篡改 direct 模式下的运行环境。

### 3.2 Gramine direct 运行边界

当前 vault 可以通过 `gramine-direct` 启动。direct 模式说明 vault 能在 Gramine LibOS 下运行，验证了：

- Python vault server 能被 Gramine 托管。
- manifest 的基本入口、路径和环境变量配置可用。
- socket 服务可以在 Gramine 环境下对外提供 store/retrieve 能力。
- 后续切换到 `gramine-sgx` 时，项目已有一个可迁移的 manifest 基础。

direct 模式不能证明：

- 宿主机无法读取 vault 内存。
- 宿主机无法读取或篡改 vault 文件。
- 运行中的 vault 代码没有被宿主替换。
- 客户端连接的一定是 enclave 内部的 vault。
- 密钥只会进入真实可信 enclave。
- `src/trusted/` 具备 SGX 级别机密性或完整性。

因此，direct 模式只能作为功能验证和迁移准备，不能作为安全证明。

### 3.3 模拟 RA 与安全信道边界

当前系统包含模拟 RA、X25519 密钥协商和 AESGCM 加密请求。它提供的是协议结构验证，而不是硬件可信证明。

当前模拟流程大致为：

```text
client 生成 nonce 和 X25519 public key
        |
        v
vault 返回模拟 quote、vault public key 和签名
        |
        v
client 验证模拟 quote 字段和签名
        |
        v
双方用 X25519 shared secret 派生 channel_key
        |
        v
后续 provision_user_key / store / retrieve 走 AESGCM 加密 JSON
```

这个流程的价值是：

- 提前形成“先验证 vault 身份，再建立加密信道，再注入 key”的接口形态。
- 让客户端代码适配未来 remote attestation。
- 让 vault API 从明文 socket 请求过渡到加密请求。
- 为未来 RA-TLS 或真实 RA 握手保留替换点。

它的限制是：

- 模拟 RA 私钥在项目目录中，宿主机可读。
- 模拟 measurement 是固定字符串，不是真实 enclave measurement。
- 签名只能证明“拥有该模拟私钥”，不能证明“运行在 SGX enclave 内”。
- 不能抵抗恶意宿主伪造 vault、替换代码或读取 direct 模式进程内存。

因此，当前模拟 RA 只能被称为学习版或协议原型，不能被称为真实 remote attestation。

## 4. 用户密钥如何进入 vault

当前系统已经从单一全局 `memory.key` 方案，推进到 per-user key 注入方案。

当前流程为：

```text
src/untrusted/chat_app.py
  |
  | 读取 VAULT_USER_ID
  | 读取 USER_MEMORY_KEY；如果不存在，则生成临时 Fernet key
  v
src/interface/vault_api.py
  |
  | open_user_vault_session(user_id, user_key)
  | 1. 建立模拟 RA + secure channel
  | 2. 通过 secure_provision_user_key 注入 user_key
  v
src/trusted/vault_server.py
  |
  | 解密安全信道请求
  | 调用 src/trusted/user_key_manager.py
  v
src/trusted/user_key_manager.py
  |
  | 将 user_id -> user_key 保存在 vault 进程内存
  v
src/trusted/memory_store.py / src/trusted/memory_retriever.py
  |
  | 使用 user_key 加解密 vault_data/{user_id}.memories.enc
```

当前优点：

- 不再使用单一全局 `memory.key`。
- 每个 user_id 有独立加密记忆文件。
- vault 重启后，如果没有重新注入 key，就不能访问该用户记忆。
- 旧版明文 store/retrieve 默认禁用，需要显式设置 `VAULT_ALLOW_LEGACY_PLAINTEXT` 才允许。

当前限制：

- 如果 `USER_MEMORY_KEY` 由普通环境变量提供，它仍然来自 TEE 外部。
- 如果未提供 `USER_MEMORY_KEY`，系统会生成临时 key，重启后无法读取旧记忆。
- 当前还没有真实 KMS、用户认证、密钥恢复、密钥轮换和密钥吊销。
- user_id、session、客户端身份和 key 的绑定还需要进一步收紧。
- 模拟 secure channel 不能替代真实 RA 或 RA-TLS。

当前 per-user key 注入方案适合原型阶段，但最终应迁移到 SGX sealing 或 remote attestation key provisioning。

## 5. 未来 SGX sealing 方案

如果未来部署环境具备 SGX，最直接的本地密钥保护方案是 SGX sealing。

推荐形态：

```text
宿主文件系统:
  vault_data/{user_id}.memories.enc
  vault_data/{user_id}.sealed_key

SGX enclave 内:
  user memory DEK
```

首次创建用户记忆时：

1. enclave 内生成随机 user memory DEK。
2. enclave 使用 SGX sealing key 将 DEK seal 成 `{user_id}.sealed_key`。
3. 宿主磁盘只保存 sealed blob，不保存明文 DEK。
4. enclave 使用 DEK 加密 `{user_id}.memories.enc`。

后续启动时：

1. enclave 读取 `{user_id}.sealed_key`。
2. enclave 内部 unseal 得到 DEK。
3. enclave 使用 DEK 解密用户记忆文件。

这种方案的安全目标是：

- 宿主机可以保存密文和 sealed blob。
- 宿主机无法直接得到明文 DEK。
- 只有符合 sealing policy 的 enclave 能恢复 DEK。

需要进一步设计的问题：

- 使用 `MRENCLAVE` 还是 `MRSIGNER` 作为 sealing policy。
- 如何处理代码升级后旧记忆无法解封的问题。
- 如何防止 sealed key 或 memory file 回滚攻击。
- 如何做密钥轮换和用户删除。
- 如何备份和迁移用户记忆。

## 6. 未来 RA-TLS 或真实 RA key provisioning

如果密钥不由 enclave 本地生成，而是由远程 KMS、用户设备或 agent 服务端注入，则必须使用真实 remote attestation。

推荐最终形态是 RA-TLS：

```text
vault enclave 生成 TLS key pair
        |
        v
SGX quote 绑定 TLS public key 和 enclave measurement
        |
        v
client / KMS 验证 quote
        |
        v
验证通过后建立 TLS 连接
        |
        v
通过 TLS 向 enclave 注入 user memory key 或 wrapping key
```

RA-TLS 要证明的不只是“通信是加密的”，而是：

- TLS 对端运行在预期 enclave 中。
- enclave measurement 或 signer 符合预期。
- TLS public key 与 attestation quote 绑定。
- user key 只释放给通过验证的 vault。

普通 HTTPS/TLS 不能直接替代 remote attestation。普通 TLS 只能证明对端持有某个证书私钥，不能证明对端运行在 TEE 内部，也不能证明运行的是预期 vault 代码。

当前模拟 RA + X25519 + AESGCM 可以被视为 RA-TLS 的教学版替身。未来可以替换为：

- 标准 TLS 传输业务请求。
- TLS certificate extension 或附加证明中携带 SGX quote。
- 客户端验证 quote、measurement、signer 和 TLS public key 绑定。
- 通过 TLS 直接发送 store/retrieve/provision 请求，而不是继续维护自定义 AESGCM JSON 信道。

如果仍然需要应用层 channel key，可以考虑使用 TLS exporter 派生应用层密钥；但默认推荐直接使用经过 RA 绑定的 TLS 连接传输业务数据。

## 7. 外部 embedding 的当前处理

当前系统在 vault 内调用 DashScope `text-embedding-v4`：

```text
store:
  memory content -> DashScope embedding API -> embedding -> encrypted storage

retrieve:
  query -> DashScope embedding API -> query embedding -> vector search
```

这带来一个重要限制：

```text
即使 vault 未来运行在 SGX 中，发送给外部 embedding 服务的文本仍会离开 TEE。
```

因此，当前外部 embedding 只能作为原型阶段选择。最终威胁模型必须明确是否信任外部 embedding 服务。

当前阶段可接受的处理方式：

- 在 README 和设计文档中明确记录该限制。
- 将 DashScope embedding 视为当前原型依赖，而不是最终隐私保护方案。
- 对高敏感数据避免夸大当前保护效果。
- 尽量将 embedding key 与聊天模型 key 分离，便于权限收敛和轮换。

## 8. 未来 TEE 内本地 embedding 方案

为了更接近最终隐私目标，后续可以将 embedding 小模型迁入 vault/TEE 内部。

目标形态：

```text
store:
  memory content
      |
      v
  TEE 内本地 embedding model
      |
      v
  embedding + memory metadata
      |
      v
  encrypted memory file

retrieve:
  query
      |
      v
  TEE 内本地 embedding model
      |
      v
  vector search + context minimizer
      |
      v
  minimized memory_context
```

迁入本地 embedding 后的好处：

- 记忆内容和 query 不再发送给外部 embedding 服务。
- vault 的隐私边界更完整。
- 可以删除 vault manifest 中的 `DASHSCOPE_API_KEY`。
- 记忆检索链路更符合 TEE 保护目标。

需要评估的问题：

- 小模型体积是否适合 enclave 内存限制。
- embedding 推理延迟是否可接受。
- 模型文件如何加入 manifest trusted files。
- 模型文件是否需要完整性校验。
- 是否需要量化模型以降低内存占用。
- 模型升级后如何处理旧 embedding。

一种务实路线是：

1. direct 阶段继续使用 DashScope embedding，保持功能可用。
2. 引入 embedding provider 抽象，支持 `dashscope` 和 `local` 两种后端。
3. 先在普通 Python 环境跑通本地小模型。
4. 再把本地 embedding 模型放入 Gramine direct。
5. 最后在 SGX 环境中评估内存、性能和 manifest 配置。

## 9. 当前 manifest 策略与限制

当前 `deployment/gramine/vault.manifest.template` 主要用于 Gramine direct 原型。它验证了运行链路，但挂载范围仍偏宽。

当前需要注意：

- 生成后的 `deployment/gramine/vault.manifest` 可能包含真实 `DASHSCOPE_API_KEY`。
- 生成后的 manifest 不应提交到版本库。
- direct 阶段挂载整个项目目录便于调试，但 SGX 阶段应收紧。
- `/usr`、`/lib`、`/etc`、venv 等挂载范围后续需要最小化。
- 模拟 RA 私钥不应被视为生产信任根。

未来 SGX manifest 应该区分：

- trusted code files
- Python runtime 和必要依赖
- 本地 embedding 模型文件
- encrypted memory data
- sealed key blob
- 临时目录
- 不应进入 enclave 的调试文件和私钥文件

## 10. 当前安全结论

当前系统已经完成了重要的架构原型：

- agent 与 vault 边界已拆分。
- vault 已可在 Gramine direct 中运行。
- 记忆存储、检索、最小化集中在 vault。
- 已有模拟 RA 和加密请求通道。
- 已有 per-user key 和 per-user encrypted memory file。
- 已避免助手回复污染长期记忆。

但当前系统仍不能声称具备真实 TEE 安全性：

- direct 模式没有硬件隔离。
- 模拟 RA 不能替代真实 SGX quote。
- 外部 embedding 服务仍能看到记忆内容和 query。
- 用户密钥生命周期尚未完整设计。
- user_id、session 和授权身份绑定仍需加强。
- manifest 和依赖挂载范围仍需收紧。

当前最准确的表述是：

```text
本项目已经完成 confidential memory vault 的系统原型和 Gramine direct 验证。
当前实现验证了接口边界和协议形态，但真实机密性依赖未来 SGX sealing、remote attestation / RA-TLS，以及 TEE 内本地 embedding。
```

## 11. 推荐后续路线

建议按以下顺序推进：

1. 保持当前 direct 原型稳定，补充最小测试集。
2. 将 README 与当前代码状态同步，明确模拟 RA 和 per-user key 方案。
3. 收紧 manifest，避免真实 API key 写入生成文件。
4. 为 embedding provider 增加抽象，为本地 embedding 迁移做准备。
5. 设计 memory schema 的 `canonical_content`、`fact_key` 和 merge 逻辑。
6. 增加 memory management CLI，支持 list/delete/export/cleanup。
7. 有 SGX 环境后优先验证 sealing。
8. 再引入真实 remote attestation 或 RA-TLS，用于远程密钥注入。
