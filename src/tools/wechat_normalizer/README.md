# WeChat Activity Report Tools

这组工具把微信 JSON 导出目录转换成可发群的事项汇总报告。工具全部位于 `src/tools/`，不读取或修改 Agent/Vault 主流程。

## 像 Agent 工具一样调用 WeChatDataAnalysis

如果本地已经启动 `externalAPI/WeChatDataAnalysis` 后端，可以让报告流水线先调用它的 HTTP API 导出聊天记录，再继续生成活动报告。这个封装位于：

```text
src/tools/wechat_normalizer/wechat_export_api.py
```

它对 Agent 暴露的是业务参数：账号、会话 username、时间范围、导出名称和是否包含媒体；内部才处理 `POST /api/chat/exports`、轮询任务、下载 zip 和安全解压。工具名是 `export_wechat_chat`，可用 `WECHAT_EXPORT_TOOL_SCHEMA` 注册参数 schema，用 `call_wechat_export_tool(arguments)` 执行。

示例：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --wechat-api http://127.0.0.1:10392 `
  --account wxid_3own0jvr3p9k12 `
  --username wxid_a6aq0g1v2g7f22 `
  --start-time 1780761600 `
  --end-time 1780847999 `
  --export-name wechat_chat_xunxu_2026-06-07_json `
  --output-root .\src\tools\wechatOutput `
  --backend-output-dir D:\srcVersionWechatAnalysis\WeChatDataAnalysis\exports
```

导出完成后会在 `--output-root` 下得到同名目录，然后自动继续执行 normalize、extract、summarize 和 render。调试页面或复用已有提取结果时仍可加：

```powershell
--skip-extract
```

只检查 LLM 请求载荷、不真正调用模型时仍可加：

```powershell
--dry-run-llm
```

## 通过主对话 Agent 调用

主对话入口现在通过一个 LangGraph 子图识别真实用户风格的微信活动报告请求。子图节点为：

```text
route_intent -> parse_request -> resolve_contact -> build_report -> final_response
```

请求里至少需要包含：

```text
群名或联系人名称
时间范围，例如一周、本周、上周、具体日期，或 start_time/end_time
```

示例：

```text
帮我生成卫星互联网研究所（25级）一周聊天内容的活动总结
```

如果用户没有直接提供 `username`，Agent 会先调用 WeChatDataAnalysis 的联系人接口按名称查找会话：

```text
GET /api/chat/contacts?keyword=<url编码后的群名或联系人名>&include_friends=true&include_groups=false&include_officials=false
```

如果这个查询没有命中，会再允许 `include_groups=true` 重试一次，因为活动总结通常来自群聊。找到会话后，Agent 会调用 WeChatDataAnalysis API 导出聊天记录，并继续生成 HTML/PDF 报告。默认 API 地址是 `http://127.0.0.1:10392`，可用环境变量覆盖：

```text
WECHAT_EXPORT_API_BASE
WECHAT_REPORT_OUTPUT_ROOT
WECHAT_EXPORT_BACKEND_OUTPUT_DIR
```

## 一键生成

在项目根目录运行：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json
```

默认会生成：

```text
normalized_messages.jsonl
normalization_report.json
extracted_activities.jsonl
weekly_activity_summary.json
weekly_activity_summary.html
weekly_activity_summary.pdf
```

默认设置：

```text
minimum_score = 0.3
include_all = false
```

这表示工具会先按时间段分组，再只把疑似包含活动或待办的时间段发送给 LLM。普通闲聊时间段默认不会送到外部模型。

## 哪一步调用 LLM

完整流水线包含四个处理阶段：

```text
1. normalize_wechat_export.py
2. extract_wechat_activities.py
3. summarize_wechat_activities.py
4. render_wechat_summary.py
```

只有第 2 步 `extract_wechat_activities.py` 会调用外部 LLM。

不调用 LLM 的步骤：

- `normalize_wechat_export.py`：本地读取 JSON、清洗 URL、提取图片元信息、按时间分组。
- `summarize_wechat_activities.py`：本地规则合并、去重、分类、排序。
- `render_wechat_summary.py`：本地渲染 HTML/PDF，图片会内嵌到 HTML/PDF。

调用 LLM 的步骤：

- `extract_wechat_activities.py`：把候选时间段中的文本发送给通义千问，提取活动、截止时间、地点、报名动作、是否必须、证据消息 ID。

## 避免重复调用 LLM

如果已经有 `extracted_activities.jsonl`，只想重新生成 summary/HTML/PDF：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json `
  --skip-extract
```

`--skip-extract` 不调用 LLM，会复用已有：

```text
extracted_activities.jsonl
```

如果该文件不存在，脚本会报错，避免误以为已经完成提取。

## Dry Run 检查 LLM 输入

只生成将要发送给 LLM 的 payload，不调用 API：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json `
  --dry-run-llm
```

输出：

```text
activity_payloads.dryrun.jsonl
```

用途：

- 人工检查哪些时间段会被送给 LLM。
- 确认闲聊是否被过滤。
- 确认图片没有作为内容发送给模型。

## 闲聊过滤与上下文窗口

工具先用通用语言结构计算候选分数，再围绕候选消息构建有界上下文窗口。
`context_group_id` 只作为来源线索，不再直接作为事项边界。

默认：

```powershell
--minimum-score 0.3
```

主要保留包含这些通用结构的消息：

```text
可执行动作、明确指令、时间或截止、参与对象、通知或更新句式
```

比赛、讲座、招聘等领域类别词只用于标签和弱加分，不能单独决定是否进入 LLM。

候选窗口同时受以下条件限制：

- 候选消息前后最多 30 分钟。
- 单个窗口最多 13 条记录、约 6000 个文本字符。
- 相隔超过 8 分钟的另一条高相关消息会开启新窗口，避免不同通知混在一起。
- 重叠窗口只有在仍满足消息数和字符预算时才合并。
- 同一事项跨窗口或跨日期的补充通过已有 `activity_id` 显式关联，并归入内部 `thread_id`。
- 不生成或依赖领域名称形式的主题键。

会过滤普通闲聊：

```text
好的、收到、哈哈、谢谢、在吗
```

调试时可以强制所有文本时间段都送 LLM：

```powershell
--include-all
```

日常不建议使用 `--include-all`，因为它会增加 API 成本，也会把闲聊发送给外部模型。

## 图片处理策略

图片不做 OCR，不做视觉模型分析，不从图片中提取事实。

规范化阶段只记录图片元信息：

```text
relative_path
mime_type
size_bytes
sha256
width
height
analysis_status
```

LLM 提取阶段：

- 图片本体不会发送给模型。
- 图片只作为候选窗口内的附件元信息出现。
- prompt 明确要求模型不能根据图片文件名、尺寸、hash 或存在性推测事实。
- 最终关联综合时间距离、消息序列距离、发送者、邻近文字中的媒体引用表达和模型请求。
- 图片候选最长不超过 15 分钟、最多间隔 5 条消息；缺少其他关联信号时不会仅因时间接近而挂载。
- 每个关联结果包含 `association_confidence`、`association_reason` 和 `association_role`。
- `association_role` 只能由邻近文字推断为报名入口、文档附件、海报、辅助图片或未确定，不代表分析了图片内容。

渲染阶段：

- HTML 会把相关图片转成 base64 `data:image/...` 内嵌。
- PDF 会包含图片。
- 因此最终的 HTML/PDF 不依赖 `media/images/...` 本地路径。

## 输出文件说明

`normalized_messages.jsonl`

本地规范化后的逐条消息。包含稳定消息 ID、时间、文本、链接、图片元信息、`context_group_id` 和候选分数。

`normalization_report.json`

规范化统计，例如原始消息数、图片数、上下文分组数、warning。

`activity_payloads.dryrun.jsonl`

`--dry-run-llm` 时生成。每行是一个将要发送给 LLM 的时间段 payload。

`extracted_activities.jsonl`

LLM 提取后的逐条活动。核心字段：

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

`weekly_activity_summary.json`

规则化合并和分类后的周报数据：

```text
mandatory_tasks
recommended_activities
other_activities
incomplete_items
cancelled_or_updated
```

`weekly_activity_summary.html`

可浏览的报告页面。图片已内嵌，单独打开也能看到图片。

`weekly_activity_summary.pdf`

最适合直接发到微信群的文件。图片已包含在 PDF 内。

## 单独运行各步骤

规范化：

```powershell
python .\src\tools\normalize_wechat_export.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json
```

LLM 提取：

```powershell
python .\src\tools\extract_wechat_activities.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json\normalized_messages.jsonl
```

汇总：

```powershell
python .\src\tools\summarize_wechat_activities.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json\extracted_activities.jsonl
```

渲染 HTML/PDF：

```powershell
python .\src\tools\render_wechat_summary.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json\weekly_activity_summary.json
```

PDF 导出需要本机存在 Chrome、Edge 或 Chromium。若浏览器不在常见路径，可通过
`WECHAT_PDF_BROWSER` 指定可执行文件；导出失败时只返回 HTML，不会报告不存在的 PDF 路径。

## 可选环境诊断

第一次部署、换网络、修改 `WECHAT_EXPORT_API_BASE` 或排查报告生成失败时，可以运行诊断脚本：

```powershell
python .\src\tools\check_wechat_report_env.py `
  --wechat-api http://<WECHAT_WINDOWS_HOST>:10392 `
  --contact-keyword "<CONTACT_OR_GROUP_NAME>" `
  --output-root .\src\tools\wechatOutput `
  --backend-output-dir "<WECHAT_BACKEND_EXPORT_DIR>"
```

该脚本检查 WeChatDataAnalysis health、联系人查询、DashScope key、输出目录、PDF 浏览器、Redis 和 Vault socket。它是部署验收和故障排查工具，不需要每次生成报告前都运行。

只生成 HTML，不导出 PDF：

```powershell
python .\src\tools\render_wechat_summary.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json\weekly_activity_summary.json `
  --no-pdf
```

## LLM 环境变量

真实调用 LLM 前需要设置：

```powershell
$env:DASHSCOPE_API_KEY="your_api_key"
```

可选指定模型：

```powershell
$env:TONGYI_MODEL="qwen-turbo"
```

也可以在命令行指定：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_chat_xunxu_2026-06-07_json `
  --model qwen-turbo
```

## 测试

```powershell
python -m unittest discover -s .\src\tools\wechat_normalizer\tests
```

编译检查：

```powershell
python -m compileall .\src\tools
```
