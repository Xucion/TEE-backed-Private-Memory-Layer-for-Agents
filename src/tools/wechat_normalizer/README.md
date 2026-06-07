# WeChat Activity Report Tools

这组工具把微信 JSON 导出目录转换成可发群的事项汇总报告。工具全部位于 `src/tools/`，不读取或修改 Agent/Vault 主流程。

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

## 闲聊过滤

工具先按 `context_group_id` 聚合同一时间段消息，再用启发式分数判断是否送 LLM。

默认：

```powershell
--minimum-score 0.3
```

会保留包含这些信号的时间段：

```text
报名、截止、提交、填写、缴费、讲座、比赛、招聘、时间、地点
```

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
- 图片只作为同时间段附件元信息出现。
- prompt 明确要求模型不能根据图片文件名、尺寸、hash 或存在性推测事实。

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
