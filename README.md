# Confidential Agent Memory Vault

## 微信活动报告流水线

`src/tools/` 中包含一套独立的微信活动报告工具链，可以把微信 JSON 导出目录转换为适合分享的活动/待办报告。该工具链独立于 Agent/Vault 运行时，不读取也不修改 vault 记忆文件。

在项目根目录一键生成：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json
```

默认会在导出目录下生成：

```text
normalized_messages.jsonl
normalization_report.json
extracted_activities.jsonl
weekly_activity_summary.json
weekly_activity_summary.html
weekly_activity_summary.pdf
```

只有活动提取步骤会调用外部大模型：

```text
normalize_wechat_export.py      仅本地处理
extract_wechat_activities.py    通过 DashScope 调用通义千问/Qwen
summarize_wechat_activities.py  仅本地处理
render_wechat_summary.py        仅本地处理
```

默认会在调用大模型前过滤疑似闲聊的时间段：

```text
minimum_score = 0.3
include_all = false
```

常用安全和调试选项：

```powershell
# 复用已有 extracted_activities.jsonl，不调用大模型。
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json `
  --skip-extract

# 只生成待发送给大模型的 payload，便于人工检查，不调用大模型。
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json `
  --dry-run-llm
```

图片不会做 OCR，也不会交给视觉模型分析。工具只记录图片元信息，并通过 `context_group_id` 将图片和相近时间段的文本关联起来。渲染出的 HTML 会把相关图片以内嵌 base64 的形式写入，生成的 PDF 也会包含图片，因此推荐将 PDF 作为最终发群文件。

详细说明见 `src/tools/wechat_normalizer/README.md`。

Agent 侧也提供了对话式微信报告入口，实现在 `src/untrusted/wechat_activity_report_tool.py`。用户可以直接说：

```text
帮我生成卫星互联网研究所（25级）一周聊天内容的活动总结
帮我生成和寻徐今天的聊天内容的活动总结
```

该入口会通过 LangGraph 子图执行：

```text
route_intent -> parse_request -> resolve_contact -> build_report -> final_response
```

`parse_request` 会从自然语言和短期对话历史中同时提取对象和时间范围；对象可以是联系人或群名，时间支持今天、昨天、前天、本周、上周、近一周和明确日期。缺少对象或时间时，Agent 会追问缺失槽位，并在下一轮复用上一轮上下文。

当用户没有直接给出 `username` 时，`resolve_contact` 会调用 WeChatDataAnalysis 的联系人接口：

```text
GET /api/chat/contacts?keyword=<对象名>&include_friends=true&include_groups=false&include_officials=false
```

内网部署时可通过环境变量配置外部微信导出工具：

```bash
export WECHAT_EXPORT_API_BASE="http://10.1.151.71:10392"
export WECHAT_REPORT_OUTPUT_ROOT="src/tools/wechatOutput"
export WECHAT_EXPORT_BACKEND_OUTPUT_DIR="D:\\srcVersionWechatAnalysis\\WeChatDataAnalysis\\exports"
```

一个具备隐私保护长期记忆能力的对话 Agent 原型。项目提供 FastAPI Agent 服务和保留的 CLI 入口，把外部 LLM 调用、短期会话和记忆抽取放在 `src/untrusted/`，把长期记忆的密钥管理、加密存储、向量检索、记忆生命周期管理和隐私最小化放在 `src/trusted/` vault 侧。

欢迎联系讨论。

当前代码已经从早期的单一全局记忆文件推进到：

- per-user 记忆文件：`vault_data/{user_id}.memories.enc`
- per-user Fernet key 注入：通过安全信道进入 vault 进程内存
- 模拟 remote attestation：固定 measurement + 开发私钥签名 quote
- 应用层加密信道：X25519 派生会话密钥，AES-GCM 加密请求和响应
- 客户端端到端 key provisioning：FastAPI 只转发握手和密文 envelope
- Agent capability 数据面：FastAPI 不读取用户 key，也不保存 client-vault channel key
- Redis 短期会话历史：按 capability 指纹和 conversation session 隔离
- 记忆生命周期：active、superseded、expired、forgotten
- 记忆管理命令：列出和遗忘指定 memory

注意：当前 secure channel 和 attestation 是开发原型，不是真实 SGX remote attestation。`gramine-direct` 可以托管 vault server，但不提供 SGX 硬件隔离。

## 当前状态

- `src/untrusted/api_server.py` 提供 FastAPI `/chat`、`/health` 和 provisioning relay。
- `src/client/vault_client.py` 在客户端本地验证模拟 RA、派生信道密钥并加密注入用户 key。
- FastAPI `/chat` 只接收 `X-Vault-Capability`，不接收 `USER_MEMORY_KEY`。
- Agent 使用 capability 调用 vault 的 `retrieve/store` 数据面。
- 旧 CLI 仍可使用 secure session；旧版明文 `store` / `retrieve` 默认禁用。
- Vault 使用用户 key 加密对应用户的 `vault_data/{user_id}.memories.enc`。
- Vault 进程重启后，用户 key 需要重新注入。
- 客户端没有提供 key 时会生成新 Fernet key；客户端必须自行安全保存，否则重启后无法读取旧记忆。
- 当前仍依赖 DashScope 的 Tongyi 模型和 `text-embedding-v4` embedding 服务。

## 主要功能

### 对话 Agent

- 使用 FastAPI 暴露 `POST /chat`。
- `AgentService` 在 FastAPI lifespan 中初始化 Redis 和 LLM。
- 使用 LangGraph 构建 `retrieve_memory -> chatbot` 两节点流程。
- 使用 `ChatTongyi` 调用通义千问模型。
- Redis 保存带 TTL 的短期多轮历史。
- 每轮用户输入前通过 capability 从 vault 检索长期记忆上下文。
- 每轮回复后异步抽取并存储用户长期记忆。
- capability 缺失时可以降级为无长期记忆聊天；capability 存在但 vault 检索失败时会继续无记忆回答，并记录日志。
- 微信活动报告请求会优先进入工具子图，不写入长期记忆。

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
- 客户端用 `src/interface/sim_ra_public_key.pem` 验证 quote 签名。
- Vault 用 `src/trusted/sim_ra_private_key.pem` 签名 quote。
- 双方通过 X25519 计算共享秘密，并用 HKDF 派生 32 字节信道密钥。
- 后续 secure payload 使用 AES-GCM 加密，并把 `session_id` 作为 associated data。
- session 当前 TTL 为 300 秒。
- key 注入成功后，Vault 返回只具备 `store/retrieve` scope 的 capability，TTL 为 3600 秒。
- FastAPI provisioning relay 不解密 payload，也不持有 channel key。

这套流程只证明“对端持有当前项目里的模拟私钥”，不能证明运行在真实 SGX enclave 中。

### Vault Server

`src/trusted/vault_server.py` 提供本地 JSON line socket 服务，默认监听 `127.0.0.1:8765`。

支持的外层 action：

- `attest`
- `handshake_start`
- `secure_ping`
- `secure_provision_user_key`
- `secure_request`
- `capability_request`
- `store` 和 `retrieve`，仅在 `VAULT_ALLOW_LEGACY_PLAINTEXT=1|true|yes` 时允许

`secure_request` 支持的内部 action：

- `store`
- `retrieve`
- `list_memories`
- `forget`

### Per-User 记忆隔离

- FastAPI 数据面不再使用 `VAULT_USER_ID` 或服务端 `USER_MEMORY_KEY`。
- 客户端通过加密 provisioning payload 提交 `user_id` 和 Fernet key。
- Vault 内部使用 `src/trusted/user_key_manager.py` 将 `user_id -> user_key` 保存在进程内存。
- 每个用户对应独立密文文件：`vault_data/{user_id}.memories.enc`。
- secure session 在第一次 provisioning 后绑定 user_id，后续不能切换用户。
- capability 在 Vault 内绑定 user_id，数据面忽略调用者提供的 user_id。
- 已存在 user_id 不能被静默覆盖成另一个 Fernet key。

### 记忆存储和检索

`src/trusted/memory_store.py` 负责：

- 使用用户 Fernet key 加密和解密 per-user memory 文件。
- 为新记忆生成 `id`、时间戳、状态字段、embedding、`fact_key` 和 `conflict_key`。
- 使用 DashScope `text-embedding-v4` 生成记忆 embedding。
- 根据 `fact_key` 合并重复事实。
- 根据 `slot` / `conflict_key` 将互斥旧事实标记为 `superseded`。
- 在同类记忆中用 embedding 相似度合并语义重复项。
- 在加载时把过期 active 记忆标记为 `expired`。
- 支持 soft delete，把 active 记忆标记为 `forgotten`。

`src/trusted/memory_retriever.py` 负责：

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
Client SDK
  |  本地验证模拟 RA / 派生 channel_key / 加密 user key
  v
FastAPI relay ---------------------------------> src/trusted/vault_server.py
  |  只转发 handshake 和 encrypted envelope              |
  |                                                       v
  |<---------------- encrypted capability -------- src/trusted/user_key_manager.py
  |
  |  POST /chat + X-Vault-Capability
  v
src/untrusted/agent_runtime.py
  |-- Redis：短期 history
  |-- Tongyi：生成回复
  |-- capability_request(retrieve/store)
  v
src/trusted/vault_server.py
  |
  +--> src/trusted/memory_store.py
  +--> src/trusted/memory_retriever.py
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
- `fastapi`
- `redis`
- `uvicorn`

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
export TONGYI_MODEL="qwen-turbo"
export VAULT_HOST="127.0.0.1"
export VAULT_PORT="8765"
export REDIS_URL="redis://127.0.0.1:6379/0"
export CHAT_HISTORY_TTL_SECONDS="86400"
export VAULT_LOG_LEVEL="INFO"
export API_LOG_LEVEL="INFO"
export WECHAT_EXPORT_API_BASE="http://127.0.0.1:10392"
export PYTHONPATH="$(pwd)/src"
```

调试旧版明文 API 时才需要：

```bash
export VAULT_ALLOW_LEGACY_PLAINTEXT="1"
```

## 运行项目

下面以两台内网机器为例：

- 服务器：运行 Redis、Vault 和 FastAPI Agent。
- 用户端：运行封装后的聊天客户端。
- 服务器内网地址示例：`192.168.1.100`。

服务器只需向内网开放 FastAPI 的 `8000` 端口。Redis 的 `6379` 和 Vault 的
`8765` 必须只允许服务器本机访问。

### 一、服务器端首次准备

进入项目目录并配置 DashScope：

```bash
cd /home/xzq2628/confidentialAgentMemoryVault
export DASHSCOPE_API_KEY="your_dashscope_api_key"
```

项目使用的 Python：

```text
/home/xzq2628/myenv/bin/python
```

确认依赖已经安装：

```bash
/home/xzq2628/myenv/bin/python -m pip install -r requirements.txt
```

#### 首次创建 Redis 容器

当前服务器环境推荐让 Redis 容器使用 host network，但明确限制 Redis 只监听
`127.0.0.1`：

```bash
docker run -d \
  --name camv-redis \
  --network host \
  redis:7-alpine \
  redis-server --bind 127.0.0.1 --protected-mode yes
```

该命令只在首次创建容器时执行。以后再次执行会出现
`container name "/camv-redis" is already in use`，这表示容器已经存在，应改用
`docker start`。

检查 Redis：

```bash
docker exec camv-redis redis-cli ping
```

正常结果：

```text
PONG
```

### 二、服务器端启动

每次运行项目使用三个终端，按以下顺序启动。

#### 终端 1：启动 Redis

```bash
docker start camv-redis
docker exec camv-redis redis-cli ping
```

如果 `docker start` 提示容器已经运行，可以直接忽略。

#### 终端 2：启动 Vault

普通 Python 模式：

```bash
cd /home/xzq2628/confidentialAgentMemoryVault
export DASHSCOPE_API_KEY="your_dashscope_api_key"
export VAULT_HOST="127.0.0.1"
export VAULT_PORT="8765"

/home/xzq2628/myenv/bin/python src/trusted/vault_server.py
```

正常日志：

```text
Vault 服务器正在监听 127.0.0.1:8765
```

也可以使用 Gramine direct 启动 Vault：

```bash
cd /home/xzq2628/confidentialAgentMemoryVault
export DASHSCOPE_API_KEY="your_dashscope_api_key"

gramine-manifest -Dproject_dir=$(pwd) deployment/gramine/vault.manifest.template deployment/gramine/vault.manifest
gramine-direct deployment/gramine/vault
```

`gramine-direct` 只验证 Gramine 运行兼容性，不提供真实 SGX 硬件隔离。生成的
`deployment/gramine/vault.manifest` 可能包含 API key，不要提交或公开。

#### 终端 3：启动 FastAPI Agent

为了允许另一台内网机器访问，Agent 监听 `0.0.0.0:8000`：

```bash
cd /home/xzq2628/confidentialAgentMemoryVault
export DASHSCOPE_API_KEY="your_dashscope_api_key"
export REDIS_URL="redis://127.0.0.1:6379/0"

/home/xzq2628/myenv/bin/python -m uvicorn \
  untrusted.api_server:app \
  --app-dir src \
  --host 0.0.0.0 \
  --port 8000
```

正常日志：

```text
Application startup complete.
Uvicorn running on http://0.0.0.0:8000
```

在服务器本机进行健康检查：

```bash
curl http://127.0.0.1:8000/health
```

正常结果：

```json
{"ok":true}
```

获取服务器内网地址：

```bash
hostname -I
```

如果启用了防火墙，只向实际内网网段开放 `8000`。例如：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp
```

不要向内网或公网开放 `6379` 和 `8765`。

### 三、内网用户端调用

用户端需要拥有项目中的 `src/client/`、`src/common/` 和 `src/interface/` 代码及 Python
依赖。进入用户端项目目录：

```bash
cd /path/to/confidentialAgentMemoryVault
```

先测试网络连通性，地址替换为服务器的真实内网 IP：

```bash
curl http://192.168.1.100:8000/health
```

然后启动封装后的交互式客户端：

```bash
/home/xzq2628/myenv/bin/python src/client/chat_cli.py \
  --user alice \
  --url http://192.168.1.100:8000
```

如果只想测试普通聊天、不连接 vault，可以显式使用无 vault 模式：

```bash
/home/xzq2628/myenv/bin/python src/client/chat_cli.py \
  --user alice \
  --url http://192.168.1.100:8000 \
  --no-vault
```

如果用户端机器上的虚拟环境路径不同，将
`/home/xzq2628/myenv/bin/python` 替换为用户端实际的 Python 路径。

客户端会自动完成：

- 首次生成用户 Fernet key。
- 在本地验证模拟 Vault quote。
- 建立安全信道并注入用户 key。
- 获取和自动刷新 capability。
- 创建并维护当前对话的 `session_id`。
- 调用 Agent `/chat` 接口。

首次运行后，用户密钥默认保存在：

```text
~/.config/confidential-agent-memory-vault/keys/alice.key
```

该文件是读取 `alice` 历史长期记忆所需的主密钥。不要删除、覆盖、提交到 Git
或发送给服务器管理员。Vault 重启后，客户端会使用该文件中的同一把 key
重新执行 provisioning。

需要继续指定的对话时，可以显式设置 session：

```bash
/home/xzq2628/myenv/bin/python src/client/chat_cli.py \
  --user alice \
  --session conversation-1 \
  --url http://192.168.1.100:8000
```

在 Python 程序中调用：

```bash
export PYTHONPATH="$(pwd)/src"
```

```python
from client import ConfidentialAgentClient

client = ConfidentialAgentClient(
    user_id="alice",
    api_base_url="http://192.168.1.100:8000",
)
print(client.chat("我喜欢喝粥"))
print(client.chat("根据我的偏好推荐早餐"))
```

内网测试可以暂时使用 HTTP，但聊天内容和 bearer capability 仍可能被内网监听。
正式部署必须使用 HTTPS。

### 四、关闭项目

按照启动顺序的反方向关闭。

1. 在用户端输入 `exit` 或 `quit`，也可以按 `Ctrl+C`。
2. 在 FastAPI Agent 终端按 `Ctrl+C`。
3. 在 Vault 终端按 `Ctrl+C`。
4. 停止 Redis：

```bash
docker stop camv-redis
```

查看服务器上的运行状态：

```bash
docker ps --filter name=camv-redis
ps -ef | grep -E 'vault_server|uvicorn' | grep -v grep
```

### 五、下次重新启动

不需要再次执行 `docker run`，依次执行：

```bash
docker start camv-redis
```

然后重新启动 Vault 和 FastAPI Agent。用户端仍使用原来的 `--user alice`
命令，客户端会自动读取已保存的用户 key 并获取新的 capability。

### 六、常见问题

#### Redis 容器名称冲突

如果出现：

```text
container name "/camv-redis" is already in use
```

说明 Redis 容器已经创建，使用：

```bash
docker start camv-redis
```

不要再次执行 `docker run`。

#### Agent 启动时报 Redis 连接错误

确认 Redis 返回 `PONG`：

```bash
docker exec camv-redis redis-cli ping
```

然后确认 Agent 使用：

```bash
export REDIS_URL="redis://127.0.0.1:6379/0"
```

#### 用户端无法连接

依次检查：

```bash
curl http://服务器内网IP:8000/health
hostname -I
sudo ufw status
```

确认 Agent 使用 `--host 0.0.0.0`，并且防火墙只对正确的内网网段开放
`8000`。

### 七、开发兼容 CLI

`src/untrusted/chat_app.py` 仍保留为服务器本机开发入口。它不经过远程用户端封装，
并使用旧的本地 secure session 模式：

```bash
cd /home/xzq2628/confidentialAgentMemoryVault
export DASHSCOPE_API_KEY="your_dashscope_api_key"
export VAULT_USER_ID="alice"
export USER_MEMORY_KEY="existing_fernet_key"

/home/xzq2628/myenv/bin/python src/untrusted/chat_app.py
```

## 记忆管理

启动 vault 后，可以用管理命令查看或遗忘当前用户的记忆：

```bash
/home/xzq2628/myenv/bin/python src/untrusted/memory_management_commands.py
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
/home/xzq2628/myenv/bin/python scripts/decrypt_memories.py <user_id> <fernet_key>
```

或：

```bash
export USER_ID="default_user"
export USER_MEMORY_KEY="a_fernet_key_for_this_user"
/home/xzq2628/myenv/bin/python scripts/decrypt_memories.py
```

## 测试

当前测试：

```bash
/home/xzq2628/myenv/bin/python tests/test_memory_lifecycle.py
/home/xzq2628/myenv/bin/python tests/test_capability_security.py
/home/xzq2628/myenv/bin/python tests/test_agent_service_capability.py
/home/xzq2628/myenv/bin/python tests/test_client_provisioning.py
/home/xzq2628/myenv/bin/python tests/test_high_level_client.py
```

测试覆盖：

- 新记忆写入
- `fact_key` 合并
- embedding 语义重复合并
- slot 冲突替换为 `superseded`
- 过期标记为 `expired`
- active-only 检索
- 访问计数更新
- list 和 forget
- 模拟 RA 握手和 AES-GCM key provisioning
- session 与 user_id 绑定
- capability 与 user_id、scope、过期时间绑定
- 禁止不同 Fernet key 覆盖同一 user_id
- AgentService 只使用 capability，不读取用户 key
- Redis key/value 不保存 capability 原文
- Client SDK -> FastAPI relay -> Vault provisioning 调用链
- 高级客户端自动保存 key、调用 `/chat` 和刷新 capability
- 微信活动报告工具的自然语言对象和时间范围提取

## 项目文件

```text
src/
  common/                      模拟安全信道共享代码
  client/                      用户端 SDK 和交互式 CLI
  interface/                   Vault 边界 API 和模拟 RA 公钥
  trusted/                     Vault 服务、密钥管理、存储和检索
  untrusted/                   Agent API、运行时和记忆抽取
  tools/                       微信聊天记录规范化、活动提取、汇总和渲染

deployment/
  gramine/
    vault.manifest.template    Gramine manifest 模板
    vault.manifest             生成产物，不提交 Git

docs/
  DESIGN.md                    架构与安全设计
  MEMORY_SCHEMA.md             记忆数据模型
  notes/                       问题记录

scripts/
  decrypt_memories.py          调试用解密查看脚本
  inspect_memory_extraction.py 记忆抽取检查脚本
  smoke_test_vault_handlers.py Vault handler 冒烟脚本

tests/                         自动化测试
vault_data/                    per-user 加密记忆文件，运行后生成
pyproject.toml                 测试路径配置
requirements.txt               Python 依赖
```

## Gramine Direct 状态

当前 manifest 模板会把项目目录、Python 环境、系统库和 `/tmp` 挂载进 Gramine，并把 `DASHSCOPE_API_KEY` 作为环境变量传入。

已支持：

1. 生成 `deployment/gramine/vault.manifest`
2. 通过 `gramine-direct deployment/gramine/vault` 启动 vault server
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
- FastAPI provisioning relay 看不到 user key，但 bearer capability 会经过 FastAPI。
- capability_request 当前通过本地 socket 传输；恶意宿主仍可能观察或盗用 capability。
- provisioning 尚未绑定 JWT/OAuth 等真实用户认证，恶意调用者可能抢先占用某个 user_id。
- Vault 重启会丢失内存中的 user key，需要重新注入。
- DashScope LLM 和 embedding 服务仍会看到发送给它们的文本。
- 还没有真实用户认证、KMS、密钥恢复、密钥轮换和密钥吊销。
- 多 Uvicorn worker 会各自持有 AgentService，部署前需要明确 worker 和 Redis/vault 策略。

## 后续工作

- 将模拟 RA 替换为真实 SGX remote attestation 或 RA-TLS。
- 将用户 key 注入迁移到 attestation-based secret provisioning。
- 评估 SGX sealing 或 Gramine encrypted files 作为本地 key 保护方案。
- 收紧 Gramine manifest 文件挂载和环境变量暴露。
- 设计正式的用户认证、session 绑定和授权模型。
- 将 capability 与已认证客户端身份或 mTLS 服务身份绑定，并增加吊销机制。
- 评估 DashScope embedding 是否符合最终威胁模型；必要时改为本地 embedding 或更严格的数据最小化。
