# 0003 - 核心以结构化逐封回调与协作式停止对接 GUI

Status: accepted

GUI 需要在"每备份完一封邮件"时拿到发件人、主题、时间、大小等元数据来填充邮件列表，并需要"停止"按钮能干净地中断下载循环。核心（`imapbackup312.py` 的 `download_messages`）因此改为：`report` 回调接收**结构化 dict**（`folder/index/total/timestamp/from/subject/size/msg_id`，头部值为未解码的原始值，由消费方自行 `decode_mime_words`）；新增 `should_stop` 回调形参，在逐封循环顶部检查，命中即写完当前封后干净跳出。CLI 的终端一行由 `format_message_line` 从同一组字段格式化，输出逐字节不变。

## Considered Options

- **report 继续传格式化字符串、GUI 反向解析**：字符串是给人看的展示格式，解析它等于把展示层当数据契约，改文案即破坏 GUI。否决。
- **两个回调（字符串给 CLI + dict 给 GUI）**：同一事实两处表达，冗余且易漂移。否决——CLI 路径 `report=None` 时本就自行打印，无需回调。
- **停止用"掐断 IMAP 连接"实现**：核心零改动，但 fetch 抛异常终止，日志留下 traceback，对非技术用户是惊吓。否决。
- **暂停/续传**：挂起的连接会超时死亡，恢复语义复杂；增量判重使"停止→重新备份"的代价≈0。否决，GUI 只提供停止。

## Consequences

- `report` 仅在 `quiet=False` 时触发（沿用既有门控）；GUI 必须传 `quiet=False` 且 `nospinner=True`。
- 停止粒度为"每封之间"：当前封写完后才退出，不做半途而废的文件写入。
- fork 核心 API 契约变更：未来任何核心消费方（如打包后的 CLI 变体）都按 dict 契约对接；CLI 终端行为经逐字节 parity 校验（`.scratch/tkinter-gui/cli_output_parity.py`）。
- 增量判重取 mbox 与 eml 已扫描消息的**并集**：待备份数按"任一格式已存在即跳过"计算，GUI 的"跳过"计数与之一致。
