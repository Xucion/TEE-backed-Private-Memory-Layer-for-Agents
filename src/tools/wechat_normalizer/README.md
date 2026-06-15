# 微信聊天活动报告工具

`src/tools/` 提供一套独立的微信聊天记录处理流水线，将
WeChatDataAnalysis 导出的 JSON 转换为结构化活动、HTML 和 PDF 报告。

工具链不读取或修改 Vault 长期记忆。只有活动提取阶段会把候选文本发送给
DashScope/Qwen；规范化、合并、图片处理和报告渲染均在本地完成。

## 当前工作流

```text
WeChatDataAnalysis API 或已有导出目录
    -> 消息规范化
    -> 通用候选评分和关系消息召回
    -> 局部上下文窗口
    -> Qwen 活动片段提取
    -> 跨窗口活动线程关联
    -> 本地合并、分类和排序
    -> HTML/PDF 渲染
```

上下文窗口只用于限制单次模型输入，不代表活动边界。同一事项在数小时或数天后
出现补充、更正、延期、取消或催办时，可以通过 `activity_id` 和 `thread_id`
重新关联。

## 目录结构

### 命令行入口

```text
src/tools/
  build_wechat_activity_report.py  一键执行完整流水线
  normalize_wechat_export.py       单独执行消息规范化
  extract_wechat_activities.py     单独执行 Qwen 活动提取
  summarize_wechat_activities.py   单独执行本地合并和分类
  render_wechat_summary.py         单独生成 HTML/PDF
  check_wechat_report_env.py       检查运行环境
```

### 实现模块

```text
src/tools/wechat_normalizer/
  normalizer.py          读取导出、清洗消息、候选评分
  activity_extractor.py  上下文窗口、LLM 提取、活动线程关联
  activity_summary.py    跨记录合并、分类和排序
  summary_renderer.py    HTML 和 PDF 渲染
  wechat_export_api.py   调用 WeChatDataAnalysis 导出 API
  models.py              规范化消息和媒体数据结构
  media.py               本地媒体校验、哈希和图片尺寸
  forwarded.py           解析微信合并转发 XML
  preferences.py         用户偏好画像和推荐分数预览
  cli.py                 规范化命令行实现
```

## 快速开始

在项目根目录执行：

```powershell
$env:DASHSCOPE_API_KEY="your_api_key"

python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_export_json
```

输入目录必须包含：

```text
manifest.json
conversations/
media/                 # 可选
```

默认生成：

```text
normalized_messages.jsonl
normalization_report.json
extracted_activities.jsonl
weekly_activity_summary.json
weekly_activity_summary.html
weekly_activity_summary.pdf
```

## 从 WeChatDataAnalysis 导出并生成

如果 WeChatDataAnalysis 已启动，可以直接从 API 导出：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --wechat-api http://127.0.0.1:10392 `
  --account "<ACCOUNT>" `
  --username "<CONVERSATION_USERNAME>" `
  --start-time 1780761600 `
  --end-time 1780847999 `
  --output-root .\src\tools\wechatOutput `
  --backend-output-dir "<WECHAT_BACKEND_EXPORT_DIR>"
```

对话入口的导出名称只由群聊或联系人的展示名称和 UTC+8 下的日期自动生成，例如 `卫星互联网研究所（25级）_20260609_20260615`。无法取得展示名称时才回退到 username；名称不包含时分秒。规范化时间固定按中国时区 UTC+8 处理。

`wechat_export_api.py` 依次执行：

```text
POST /api/chat/exports
-> 轮询导出任务
-> 下载 ZIP
-> 检查路径穿越并安全解压
-> 运行报告流水线
```

## 对话式 Agent 入口

`src/untrusted/wechat_activity_report_tool.py` 使用以下 LangGraph 子图：

```text
route_intent
-> parse_request
-> resolve_contact
-> build_report
-> final_response
```

示例：

```text
帮我生成卫星互联网研究所（25级）一周聊天内容的活动总结
```

当用户没有提供微信 `username` 时，工具会调用联系人查询接口解析群名或联系人：

```text
GET /api/chat/contacts?keyword=<名称>&include_friends=true&include_groups=false
```

未命中时会允许群聊查询重试。

## 活动候选与闲聊过滤

规范化阶段为每条消息计算 `activity_features`。当前主要使用通用语言结构：

```text
可执行动作
通知或安排
强制要求
日期和截止时间
明确参与对象
补充、更正、延期、取消等关系表达
链接和媒体附件
```

比赛、讲座、招聘等领域词只用于弱分类和少量加分，不会单独决定消息是否进入
LLM，避免对特定群聊过拟合。

默认候选阈值：

```text
minimum_score = 0.3
```

普通闲聊如“好的”“收到”“哈哈”通常不会成为候选锚点。

通用关系消息会独立召回。即使分数低于普通阈值，下列消息仍可进入关系判断：

```text
补充一下，地点改为 A 楼
刚才的时间调整为下周一
上述通知延期
前面的安排取消
```

调高阈值会减少模型调用和误报，但可能漏掉表达含蓄的新事项：

```powershell
--minimum-score 0.45
```

调试时可以发送全部文本：

```powershell
--include-all
```

日常不建议使用 `--include-all`，因为它会增加费用和外部模型接触的聊天文本量。

## 局部上下文窗口

每条候选消息作为锚点构建局部窗口：

```text
时间范围：锚点前后最多 30 分钟
消息数量：最多 13 条
文本长度：最多 6000 字符
候选边界：另一个高分候选相距超过 8 分钟时停止扩展
```

重叠窗口只在仍满足消息数和字符预算时合并。

这些限制只控制单次 Qwen 输入，不用于认定活动是否相同。

## 跨窗口活动关联

提取阶段会向模型提供同一会话中此前已经提取的全部活动简要信息。模型需要先判断
当前内容属于：

```text
new
supplement
reminder
correction
postponed
cancelled
```

关联字段：

```text
activity_id
thread_id
relation_type
related_activity_id
```

模型提取完成后，程序还会执行一次全局线程校正，综合以下通用证据：

```text
模型返回的 related_activity_id
明确的补充、更正或取消表达
相同链接
标题或事项对象的直接提及
去除通用动作词后的文本重合度
```

时间距离不是硬边界。不同微信会话之间不会自动关联省略式补充。

不生成或依赖 `topic_key`，也不会把“专业实践”等领域名称当作活动身份。

## LLM 提取与校验

只有 `activity_extractor.py` 调用外部模型。模型需要返回结构化 JSON，并为每项活动
提供来自当前窗口的 `evidence_message_ids`。

程序会再次校验：

```text
标题不能为空
证据消息必须来自输入窗口
evidence_quote 必须出现在证据文本中
活动类型和关系类型必须属于允许值
mandatory 必须有强制语言支撑
日期和截止时间需要符合证据
同一证据上的总事项和子步骤需要合并
```

模型配置：

```powershell
$env:DASHSCOPE_API_KEY="your_api_key"
$env:TONGYI_MODEL="qwen-max"
```

也可以通过参数覆盖：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_export_json `
  --model qwen-max
```

实际代码未设置 `TONGYI_MODEL` 时默认使用 `qwen-max`。

## 图片处理

图片不做 OCR，不调用视觉模型，也不从图片内容推断活动事实。

规范化阶段只读取：

```text
relative_path
mime_type
size_bytes
sha256
width
height
analysis_status
```

图片与文本活动的关联条件：

```text
最长时间距离：15 分钟
最大消息距离：5 条
最低关联分数：0.55
```

评分综合时间距离、消息距离、发送者是否一致、邻近文字是否包含“见图、附件、
二维码”等表达，以及模型是否请求关联该图片。

最终活动中的图片包含：

```text
association_confidence
association_reason
association_role
```

HTML 会将图片编码为 base64，PDF 直接包含渲染结果。

## 活动汇总

`activity_summary.py` 在本地合并活动，主要依据：

```text
相同 thread_id
相同 activity_id
证据消息重叠且标题相关
相同报名链接
非通用的相同标题
文本相似度回退规则
```

最终分类：

```text
mandatory_tasks
recommended_activities
other_activities
cancelled_or_updated
```

信息缺失保留在活动自身的 `missing_information` 中，不单独建立分类。

## 用户偏好

`preferences.py` 保留用户偏好画像能力。可以重复传入：

```powershell
--user-memory "我关注学术讲座"
--user-memory "我不关心体育比赛"
--user-memory "我只需要知道必须完成的任务"
```

当前偏好结果写入规范化消息的 `personalization_preview`，用于后续接入个性化推荐。
它目前不会改变候选窗口、活动线程合并或最终报告分类。

## Dry Run

只生成即将发送给模型的窗口载荷，不调用 DashScope：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_export_json `
  --dry-run-llm `
  --no-pdf
```

输出：

```text
activity_payloads.dryrun.jsonl
```

适合检查：

```text
哪些消息会发送给模型
闲聊是否被过滤
远距离补充消息是否被召回
图片是否只以元信息出现
```

`--dry-run-llm` 执行到载荷生成后停止，不生成新的活动汇总和报告。

## 复用已有提取结果

如果 `extracted_activities.jsonl` 已存在，只重新执行汇总和渲染：

```powershell
python .\src\tools\build_wechat_activity_report.py `
  --input .\src\tools\wechatOutput\wechat_export_json `
  --skip-extract
```

该命令仍会重新规范化原始导出，但不会调用 Qwen。

如果只想重新渲染现有汇总：

```powershell
python .\src\tools\render_wechat_summary.py `
  --input .\src\tools\wechatOutput\wechat_export_json\weekly_activity_summary.json
```

如果已经有 HTML，也可以直接使用 Edge/Chrome 的无头打印功能重新生成 PDF。

## 单独运行各阶段

规范化：

```powershell
python .\src\tools\normalize_wechat_export.py `
  --input .\src\tools\wechatOutput\wechat_export_json
```

活动提取：

```powershell
python .\src\tools\extract_wechat_activities.py `
  --input .\src\tools\wechatOutput\wechat_export_json\normalized_messages.jsonl
```

只检查活动提取载荷：

```powershell
python .\src\tools\extract_wechat_activities.py `
  --input .\src\tools\wechatOutput\wechat_export_json\normalized_messages.jsonl `
  --dry-run-payloads .\src\tools\wechatOutput\wechat_export_json\activity_payloads.dryrun.jsonl
```

汇总：

```powershell
python .\src\tools\summarize_wechat_activities.py `
  --input .\src\tools\wechatOutput\wechat_export_json\extracted_activities.jsonl
```

渲染：

```powershell
python .\src\tools\render_wechat_summary.py `
  --input .\src\tools\wechatOutput\wechat_export_json\weekly_activity_summary.json
```

只生成 HTML：

```powershell
python .\src\tools\render_wechat_summary.py `
  --input .\src\tools\wechatOutput\wechat_export_json\weekly_activity_summary.json `
  --no-pdf
```

## 输出文件

### `normalized_messages.jsonl`

逐条规范化消息，包括稳定 ID、时间、文本、链接、媒体元信息、候选特征、
`context_group_id` 和可选的 `personalization_preview`。

### `normalization_report.json`

规范化统计和警告，例如消息数量、媒体数量、未知类型及上下文组数量。

### `activity_payloads.dryrun.jsonl`

Dry Run 生成的模型请求。每行对应一个局部上下文窗口。

### `extracted_activities.jsonl`

结构化活动片段，主要字段：

```text
title
activity_id
thread_id
relation_type
related_activity_id
kind
mandatory
start_date
end_date
start_time
deadline
location
required_action
registration_url
eligibility
confidence
evidence_message_ids
evidence_quote
related_images
missing_information
```

### `weekly_activity_summary.json`

本地合并和分类后的报告数据。

### `weekly_activity_summary.html`

包含内嵌图片的独立 HTML 文件。

### `weekly_activity_summary.pdf`

通过本地 Chrome、Edge 或 Chromium 打印生成。

## 环境变量

```text
DASHSCOPE_API_KEY              DashScope API Key
TONGYI_MODEL                   活动提取模型，默认 qwen-max
WECHAT_EXPORT_API_BASE         WeChatDataAnalysis API 地址
WECHAT_REPORT_OUTPUT_ROOT      报告输出根目录
WECHAT_EXPORT_BACKEND_OUTPUT_DIR
                               WeChatDataAnalysis 后端导出目录
WECHAT_PDF_BROWSER             Chrome/Edge/Chromium 可执行文件
```

## 环境诊断

```powershell
python .\src\tools\check_wechat_report_env.py `
  --wechat-api http://127.0.0.1:10392 `
  --contact-keyword "<CONTACT_OR_GROUP_NAME>" `
  --output-root .\src\tools\wechatOutput
```

诊断内容包括：

```text
WeChatDataAnalysis health
联系人查询
DashScope API Key
输出目录写权限
PDF 浏览器
Redis
Vault socket
```

## PDF 浏览器

PDF 导出需要 Chrome、Edge 或 Chromium。自动查找失败时：

```powershell
$env:WECHAT_PDF_BROWSER="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

浏览器导出失败时 HTML 仍会保留，命令结果中的 `pdf_created` 和 `pdf_error` 会说明
PDF 是否生成成功。

## 测试

微信工具测试：

```powershell
python -m unittest src.tools.wechat_normalizer.tests.test_normalizer
```

对话式报告入口测试：

```powershell
python .\tests\test_wechat_activity_report_tool.py
```

编译检查：

```powershell
python -m compileall -q .\src\tools
```
