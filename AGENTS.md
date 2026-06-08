# 项目协作说明

## 角色与权限

你是当前项目的高级安全代码助手，可以修改本仓库内文件，但必须遵循最小权限原则。

- 不要修改项目根目录之外的文件。
- 不要修改 `externalAPI/` 目录下的任何内容；该目录只作为外部工具源码和 API 文档参考。
- 遇到 `rm`、`sudo`、`chmod`、`chown`、全局覆盖、递归删除或依赖安装命令时，必须先向用户确认。
- 不要自动运行 `npm install`、`pip install` 等依赖安装命令，除非用户明确要求。
- 可以修改 `requirements.txt` 或项目文档来记录依赖变化。

## 项目目标

Confidential Agent Memory Vault 是一个具备隐私保护长期记忆能力的对话 Agent 原型。核心目标是把长期记忆的密钥管理、加密存储、向量检索、生命周期管理和隐私最小化放入 vault 边界，后续迁移到真实 TEE/SGX 环境，降低长期用户画像被宿主环境窃取的风险。

当前实现已经包含：

- FastAPI 对话服务和客户端 SDK。
- LangGraph + LangChain 的对话运行时。
- DashScope 通义千问聊天模型和 `text-embedding-v4` embedding。
- per-user Fernet key provisioning。
- 模拟 remote attestation。
- X25519 + HKDF + AES-GCM 应用层安全信道。
- bearer capability 数据面。
- Redis 短期会话历史。
- per-user 加密记忆文件。
- 记忆生命周期管理。
- 微信聊天记录活动报告流水线。

当前 secure channel、quote 和 attestation 都是开发原型，不代表真实 SGX remote attestation。`gramine-direct` 只验证 Gramine 兼容性，不提供 SGX 硬件隔离。

## 当前目录职责

```text
src/client/      用户端 SDK、交互式 CLI、本地 key 保存和 capability 自动刷新
src/common/      模拟安全信道共享代码
src/interface/   Vault socket API、模拟 RA 验证和 FastAPI relay 边界
src/trusted/     Vault server、用户 key 管理、加密存储、检索和最小化
src/untrusted/   FastAPI Agent、LangGraph 对话、记忆抽取和微信报告工具入口
src/tools/       微信聊天记录规范化、LLM 活动提取、汇总、HTML/PDF 渲染
scripts/         调试和冒烟脚本
tests/           自动化测试
deployment/      Gramine manifest 模板
externalAPI/     外部 WeChatDataAnalysis 工具，禁止修改
vault_data/      运行时生成的 per-user 加密记忆文件
```

## Agent 主流程

`src/untrusted/api_server.py` 提供 FastAPI 服务：

- `/health` 返回服务健康状态。
- `/chat` 接收用户消息、session 和可选 `X-Vault-Capability`。
- `/vault/handshake` 和 `/vault/provision` 只转发 vault 握手和密文 provisioning 请求。

`src/untrusted/agent_runtime.py` 是服务端运行时：

- 启动阶段初始化 Redis 和 `ChatTongyi`。
- 使用 Redis 按 capability 指纹和 session 隔离短期历史，不保存 capability 原文。
- 普通聊天会先尝试用 capability 检索 vault 长期记忆。
- capability 缺失时降级为无长期记忆聊天。
- capability 存在但 vault 检索失败时记录日志并继续无记忆回答。
- 回复后后台只从用户原话抽取长期记忆，并通过 capability 写回 vault。
- 微信活动报告请求会优先进入工具子图，不写入长期记忆。

`src/untrusted/chat_app.py` 是本机开发 CLI，仍可用于旧式 secure session 调试。远程用户优先使用 `src/client/chat_cli.py` 或 `src/client/agent_client.py`。

## Vault 与安全边界

`src/trusted/vault_server.py` 提供本地 JSON line socket 服务，默认监听 `127.0.0.1:8765`。

主要 action：

- `attest`
- `handshake_start`
- `secure_ping`
- `secure_provision_user_key`
- `secure_request`
- `capability_request`
- `store` / `retrieve`，仅在 `VAULT_ALLOW_LEGACY_PLAINTEXT=1|true|yes` 时允许

Vault 内部负责：

- 验证请求大小、action、memory 字段、`top_k`、`threshold` 和 capability scope。
- 使用 `src/trusted/user_key_manager.py` 保存进程内 `user_id -> Fernet key`。
- 使用 `vault_data/{user_id}.memories.enc` 保存加密记忆。
- 对 active 记忆进行 embedding 检索、重复合并、冲突替换、过期标记和 soft delete。
- 在返回 Agent 前执行 Context Minimizer：高敏感记忆只返回类别级提示，低敏记忆可以返回内容。

## 微信活动报告流水线

`src/tools/` 下的微信工具链独立于长期记忆系统，不读取也不修改 vault 记忆。

本地文件流水线：

```text
normalize_wechat_export.py      本地规范化导出 JSON
extract_wechat_activities.py    调用 DashScope/Qwen 提取活动
summarize_wechat_activities.py  本地合并和分类活动
render_wechat_summary.py        本地渲染 HTML/PDF
build_wechat_activity_report.py 一键编排以上步骤
```

图片处理策略：

- 不做 OCR。
- 不调用视觉模型。
- 只按相近时间段把图片元信息关联到文本上下文。
- HTML 会以内嵌 base64 方式包含图片，PDF 也会包含图片，适合发群。

对话式入口位于 `src/untrusted/wechat_activity_report_tool.py`，使用 LangGraph 子图：

```text
route_intent -> parse_request -> resolve_contact -> build_report -> final_response
```

该入口支持用户自然表达，例如：

```text
帮我生成卫星互联网研究所（25级）一周聊天内容的活动总结
帮我生成和寻徐今天的聊天内容的活动总结
```

`parse_request` 会同时提取对象和时间范围，并能从短期历史中补齐上一轮缺失槽位。对象可以出现在时间前或时间后。时间支持今天、昨天、前天、本周、上周、近一周和明确日期。

当用户只给出联系人或群名时，工具通过 WeChatDataAnalysis API 查找 username：

```text
GET /api/chat/contacts?keyword=<对象名>&include_friends=true&include_groups=false&include_officials=false
```

常用环境变量：

```bash
WECHAT_EXPORT_API_BASE
WECHAT_REPORT_OUTPUT_ROOT
WECHAT_EXPORT_BACKEND_OUTPUT_DIR
WECHAT_REPORT_TODAY
```

## 开发和测试注意事项

- 优先使用 `rg` / `rg --files` 搜索。
- 修改代码前先阅读现有模式，尽量保持小范围改动。
- 不要提交真实 API key、用户 Fernet key、`vault_data/` 运行数据或微信导出隐私数据。
- `deployment/gramine/vault.manifest` 是生成产物，可能含环境变量，不应公开。
- Windows 和 Linux 间提交时注意 LF/CRLF 提示；通常只是 Git 行尾转换警告。
- 运行测试前确认依赖可用；如果缺少 Redis 或 Python 包，不要自动安装，向用户说明。

常用检查：

```bash
python -m compileall scripts src tests
python tests/test_memory_lifecycle.py
python tests/test_capability_security.py
python tests/test_agent_service_capability.py
python tests/test_client_provisioning.py
python tests/test_high_level_client.py
python tests/test_wechat_activity_report_tool.py
python -m unittest src.tools.wechat_normalizer.tests.test_normalizer
```

## 后续重点

- 在真实 SGX 环境中从 `gramine-direct` 迁移到 `gramine-sgx`。
- 将模拟 RA 替换为真实 remote attestation 或 RA-TLS。
- 将 user key provisioning 绑定真实 attestation 结果和用户认证。
- 评估 DashScope embedding/LLM 看到敏感文本是否符合最终威胁模型。
- 收紧 Gramine manifest 挂载范围和环境变量暴露。
- 为 capability 增加真实身份绑定、吊销和审计机制。
