# 微信聊天活动报告助手

本项目面向一个直接的实际场景：从自己的微信聊天记录中导出指定联系人或群聊在某段时间内的消息，自动提取活动、通知、截止时间和待办事项，并生成可独立分享的 HTML/PDF 报告。

项目通过 `externalAPI/WeChatDataAnalysis` 访问和导出本机微信数据，通过 DashScope 通义千问提取结构化事项，其余规范化、筛选、合并和渲染步骤均在本地完成。

典型使用方式：

```text
帮我生成卫星互联网研究所（25级）近一周聊天内容的活动总结
帮我生成和寻徐今天的聊天内容的活动总结
```

也可以完全绕过对话 Agent，直接用命令行从微信导出 API 或已有 JSON 导出目录生成报告。

> 本项目只应处理你有权访问和分析的聊天记录。导出文件、HTML 和 PDF 都可能包含个人信息，请勿提交到 Git 或上传到不受信任的位置。

## 能做什么

- 按联系人、群聊和时间范围导出微信聊天记录。
- 从自然语言中识别联系人/群名以及今天、昨天、本周、上周、近一周或明确日期。
- 提取活动通知、必须完成的任务、报名方式、截止时间、地点和证据消息。
- 合并重复通知，识别补充、更新和取消关系。
- 过滤大部分普通闲聊，减少发送给外部模型的文本和调用成本。
- 生成结构化 JSON、可浏览 HTML 和适合发群的 PDF。
- 将相关图片内嵌到 HTML/PDF，但不对图片做 OCR 或视觉分析。
- 支持先检查 LLM 请求载荷、跳过重复提取和只生成 HTML。

## 处理流程

```text
本机微信数据
    |
    v
WeChatDataAnalysis
  联系人查询 / JSON 导出 / 媒体打包
    |
    v
下载 ZIP 并安全解压
    |
    v
本地规范化与候选时间段筛选
    |
    v
DashScope/Qwen 提取活动和待办
    |
    v
本地合并、分类与排序
    |
    v
HTML / PDF / JSON 报告
```

只有“活动和待办提取”会调用外部 LLM。图片本体不会发送给模型。

## 快速开始

### 1. 准备环境

运行要求：

- Python 3.10+
- 有效的 `DASHSCOPE_API_KEY`
- Chrome、Edge 或 Chromium，仅在生成 PDF 时需要
- 直接从本机微信导出时，需要在 Windows 上运行 WeChatDataAnalysis

如果已经有符合格式的 JSON 导出目录，后续报告处理不要求运行在 Windows。

项目依赖记录在 `requirements.txt`。创建并激活虚拟环境后安装：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

设置 DashScope API key：

```powershell
$env:DASHSCOPE_API_KEY="<YOUR_DASHSCOPE_API_KEY>"
```

可选指定模型，默认是 `qwen-turbo`：

```powershell
$env:TONGYI_MODEL="qwen-turbo"
```

### 2. 启动微信数据服务

`externalAPI/WeChatDataAnalysis` 是本项目使用的外部微信解密、浏览和导出工具。本仓库只调用它提供的 HTTP API，不修改其源码。

在 Windows 上按该工具自己的文档完成微信数据解密或导入。源码开发模式的常见启动方式是：

```powershell
cd .\externalAPI\WeChatDataAnalysis
.\start-dev.cmd
```

默认地址：

```text
前端：http://127.0.0.1:3000
API：http://127.0.0.1:10392
文档：http://127.0.0.1:10392/docs
```

检查服务：

```powershell
Invoke-RestMethod http://127.0.0.1:10392/api/health
```

### 3. 一键导出并生成报告

先在 WeChatDataAnalysis 页面或联系人接口中确认：

- `account`：已解密的微信账号目录名，可选。
- `username`：目标联系人或群聊的内部会话 ID。
- `start-time` / `end-time`：Unix 秒级时间戳。

然后在本项目根目录运行：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --wechat-api http://127.0.0.1:10392 `
  --account "<ACCOUNT>" `
  --username "<CONVERSATION_USERNAME>" `
  --start-time <START_UNIX_SECONDS> `
  --end-time <END_UNIX_SECONDS> `
  --export-name "wechat_report_2026-06-01_2026-06-07" `
  --output-root .\src\tools\wechatOutput
```

该命令会依次完成：

1. 创建 WeChatDataAnalysis 导出任务。
2. 轮询任务状态并下载 ZIP。
3. 在 `src/tools/wechatOutput/` 下安全解压。
4. 规范化消息并过滤低相关闲聊。
5. 调用 Qwen 提取活动和待办。
6. 生成 JSON、HTML 和 PDF。

部分 WeChatDataAnalysis 部署需要显式传入后端输出目录：

```powershell
--backend-output-dir "D:\path\to\WeChatDataAnalysis\exports"
```

不需要媒体文件时可以减少导出体积：

```powershell
--no-media
```

需要启用 WeChatDataAnalysis 自身提供的隐私导出模式时：

```powershell
--privacy-mode
```

## 从已有导出生成

如果已经通过 WeChatDataAnalysis 得到 JSON 导出目录，可以不再调用微信导出 API：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\<EXPORT_DIRECTORY>
```

输入目录应包含：

```text
manifest.json
conversations/
media/              # 可选
```

默认在该目录内生成：

```text
normalized_messages.jsonl
normalization_report.json
extracted_activities.jsonl
weekly_activity_summary.json
weekly_activity_summary.html
weekly_activity_summary.pdf
```

其中 `weekly_activity_summary.pdf` 最适合直接分享；HTML 中的关联图片也会以内嵌 base64 形式保存，不依赖原始媒体路径。

## 常用选项

### 检查将发送给 LLM 的内容

`--dry-run-llm` 只生成请求载荷，不调用模型，也不继续汇总和渲染：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\<EXPORT_DIRECTORY> `
  --dry-run-llm
```

检查：

```text
activity_payloads.dryrun.jsonl
```

### 复用已有提取结果

修改分类或渲染逻辑后，可以复用已有 `extracted_activities.jsonl`，避免再次调用 LLM：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\<EXPORT_DIRECTORY> `
  --skip-extract
```

### 只生成 HTML

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\<EXPORT_DIRECTORY> `
  --no-pdf
```

### 调整闲聊过滤

默认只把候选分数不低于 `0.3` 的时间段发送给 LLM：

```powershell
--minimum-score 0.3
```

调试时可以发送所有文本时间段：

```powershell
--include-all
```

日常不建议使用 `--include-all`，因为它会增加 API 成本和隐私暴露范围。

## 对话式生成

项目提供 FastAPI + LangGraph 对话入口。它会从自然语言和短期对话历史中补齐目标会话与时间范围：

```text
用户：帮我生成今天的微信活动总结
助手：还需要群名或联系人名称
用户：寻徐
```

微信报告子图：

```text
route_intent -> parse_request -> resolve_contact -> build_report -> final_response
```

当用户只提供展示名称时，Agent 会调用联系人查询接口解析 `username`，随后自动导出并生成报告。

### 启动条件

对话式模式额外需要：

- Redis，用于短期多轮历史。
- 可访问的 WeChatDataAnalysis API。
- DashScope API key。

长期记忆 Vault 不是生成微信报告的必需组件。只使用微信报告时，可以让客户端以 `--no-vault` 模式运行。

配置：

```powershell
$env:DASHSCOPE_API_KEY="<YOUR_DASHSCOPE_API_KEY>"
$env:REDIS_URL="redis://127.0.0.1:6379/0"
$env:WECHAT_EXPORT_API_BASE="http://127.0.0.1:10392"
$env:WECHAT_REPORT_OUTPUT_ROOT="src/tools/wechatOutput"
```

如果 WeChatDataAnalysis 要求后端绝对输出目录，再设置：

```powershell
$env:WECHAT_EXPORT_BACKEND_OUTPUT_DIR="<WECHAT_BACKEND_EXPORT_DIR>"
```

启动 Agent：

```powershell
python -m uvicorn untrusted.api_server:app `
  --app-dir src `
  --host 127.0.0.1 `
  --port 8000
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

启动无 Vault 客户端：

```powershell
python .\src\client\chat_cli.py `
  --user local-user `
  --url http://127.0.0.1:8000 `
  --no-vault
```

示例请求：

```text
帮我生成卫星互联网研究所（25级）近一周聊天内容的活动总结
帮我生成和寻徐昨天的聊天内容的活动总结
帮我生成今天和寻徐的聊天内容活动总结，不生成 PDF
```

支持的时间表达包括：

- 今天、昨天、前天
- 本周、上周、近一周
- `2026-06-07`
- `2026-06-01 到 2026-06-07`
- 显式 `start_time` 和 `end_time`

## 图片与隐私策略

### 图片

- 不做 OCR。
- 不调用视觉模型。
- 不根据文件名、尺寸或 hash 猜测图片内容。
- 只使用时间距离、消息顺序、发送者和邻近文字判断图片是否与事项相关。
- 关联图片会被内嵌到 HTML/PDF。

### 外部模型

- 规范化、图片元信息提取、汇总和渲染都在本地执行。
- 只有候选时间段中的文本会发送给 DashScope/Qwen。
- 默认闲聊过滤会减少外发文本，但不能构成绝对隐私保证。
- `--dry-run-llm` 可用于调用前人工审查载荷。
- 报告内容和原始导出仍保存在本地明文目录中，应按敏感数据管理。

## 环境诊断

首次部署、切换网络或排查失败时运行：

```powershell
python .\src\tools\check_wechat_report_env.py `
  --wechat-api http://127.0.0.1:10392 `
  --contact-keyword "<CONTACT_OR_GROUP_NAME>" `
  --output-root .\src\tools\wechatOutput `
  --backend-output-dir "<WECHAT_BACKEND_EXPORT_DIR>"
```

诊断项包括：

- WeChatDataAnalysis 健康检查。
- 联系人/群聊查询。
- DashScope API key。
- 报告输出目录。
- PDF 浏览器。
- Redis。
- 可选 Vault socket。

`WARN` 通常表示 PDF 或长期记忆等能力降级；WeChatDataAnalysis、输出目录或 DashScope 相关的 `FAIL` 会阻断主要报告流程。

## 输出数据

### `normalized_messages.jsonl`

规范化后的逐条消息，包含稳定消息 ID、时间、文本、链接、图片元信息、上下文分组和候选分数。

### `extracted_activities.jsonl`

模型提取的结构化事项，主要字段包括：

```text
title
activity_id / thread_id
relation_type / related_activity_id
kind
mandatory
start_date / end_date / start_time
deadline
location
required_action
registration_url
eligibility
evidence_message_ids
evidence_quote
related_images
missing_information
```

### `weekly_activity_summary.json`

本地规则合并后的报告数据：

```text
mandatory_tasks
recommended_activities
other_activities
incomplete_items
cancelled_or_updated
```

### `weekly_activity_summary.html` / `.pdf`

最终可读报告。关联图片已内嵌，适合脱离原始导出目录查看和分享。

## 项目结构

```text
src/tools/
  build_wechat_activity_report.py    一键导出并生成报告
  check_wechat_report_env.py         环境诊断
  normalize_wechat_export.py         本地规范化
  extract_wechat_activities.py       LLM 活动提取
  summarize_wechat_activities.py     本地合并和分类
  render_wechat_summary.py           HTML/PDF 渲染
  wechat_normalizer/                 规范化、提取和渲染核心模块

src/untrusted/
  wechat_activity_report_tool.py     LangGraph 微信报告子图
  api_server.py                      FastAPI 对话服务
  agent_runtime.py                   对话路由和短期历史

src/client/                          对话客户端和 SDK
externalAPI/WeChatDataAnalysis/      外部微信数据工具，只读参考
tests/                               自动化测试
```

更细的微信处理字段和单步命令见
[`src/tools/wechat_normalizer/README.md`](src/tools/wechat_normalizer/README.md)。

## 测试

微信报告主流程：

```powershell
python .\tests\test_wechat_activity_report_tool.py
python -m unittest src.tools.wechat_normalizer.tests.test_normalizer
python -m compileall .\src\tools .\src\untrusted
```

完整项目还包含 Agent capability、客户端 provisioning 和记忆生命周期测试：

```powershell
python .\tests\test_agent_service_capability.py
python .\tests\test_client_provisioning.py
python .\tests\test_high_level_client.py
python .\tests\test_capability_security.py
python .\tests\test_memory_lifecycle.py
```

## 可选：隐私记忆 Vault

仓库最初还包含一个长期记忆保护研究原型。它将 per-user key、Fernet 加密存储、向量检索、生命周期管理和 Context Minimizer 放在独立 Vault 边界中，并提供模拟 remote attestation、X25519 + HKDF + AES-GCM 安全信道和 bearer capability。

这部分能力适合研究“对话助手如何在未来 TEE/SGX 环境中保存长期用户偏好”，但不是微信聊天导出和活动报告流水线的前置条件。

当前必须明确：

- `gramine-direct` 只验证 Gramine 兼容性，不提供 SGX 硬件隔离。
- 当前 quote 和 attestation 是模拟实现，不是真实 remote attestation。
- 模拟私钥位于项目中，不能抵抗恶意宿主机。
- DashScope embedding 和聊天模型仍可能看到发送给它们的文本。
- 真正的 TEE 机密性仍依赖未来的 `gramine-sgx`、真实 RA/RA-TLS、sealing 和更严格的密钥管理。

相关设计文档：

- [`docs/DESIGN.md`](docs/DESIGN.md)
- [`docs/MEMORY_SCHEMA.md`](docs/MEMORY_SCHEMA.md)
- [`deployment/gramine/README.md`](deployment/gramine/README.md)

## 当前限制

- 需要先由 WeChatDataAnalysis 解密或导入本机微信数据。
- 活动提取依赖 DashScope/Qwen，不是完全离线流程。
- 不读取图片文字，因此只存在于海报图片中的日期、地点和二维码不会被提取。
- 联系人同名或群名模糊时，自动解析可能选不到目标会话。
- PDF 依赖本机 Chrome、Edge 或 Chromium；缺失时仍可生成 HTML。
- 对话式模式依赖 Redis；命令行报告模式不依赖 Redis 或 Vault。
- 当前实现是面向原型和内网部署的工具，不包含完整的用户认证、权限审计和生产级数据治理。

## 后续方向

- 提供面向普通用户的报告生成界面，隐藏 `account`、`username` 和 Unix 时间戳。
- 在导出前展示联系人候选和日期范围，减少误选会话。
- 增加报告模板、主题样式和可编辑确认流程。
- 支持完全本地的活动提取模型，减少聊天文本外发。
- 增加导出文件自动清理、脱敏和访问审计。
- 将长期记忆作为报告偏好增强项，而不是主流程依赖。
