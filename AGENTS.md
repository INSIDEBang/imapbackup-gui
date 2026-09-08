# AGENTS.md

## 项目概况

`rcarmo/imapbackup` 的 fork，目前尚未改动任何代码。Fork 目标（无法从代码推断）：

- 加 Tkinter 图形界面，最终打包成带安装路径选择的 Windows 安装程序，分发给公司非技术员工。
- 输出除 `mbox` 外还要支持 `eml`（eml 是新增能力，原项目只有 mbox）

## 仓库结构

- 三个并行脚本按目标 Python 版本划分，互不 import：
  - `imapbackup.py` — Python 2.x 遗留版（冻结，不再更新）
  - `imapbackup38.py` — Python 3.8+，过渡版
  - `imapbackup312.py` — Python 3.12+，现代化版（argparse/dataclass/pathlib/logging），**新工作基于此**
- 命名约定（上游贡献指南）：新主脚本以目标 Python 版本命名（如 `imapbackup39.py`）。GUI 应作为独立入口文件，复用 `imapbackup312.py` 的模块级函数（`scan_file`、`download_messages` 等），不要复制核心逻辑
- 无 `setup.py` / `pyproject.toml` / `requirements.txt` / 测试 / lint 配置。CI 仅有 stale-bot 工作流，没有构建或测试流水线

## 运行与验证

```bash
# 无需安装任何东西
python imapbackup312.py --help
python imapbackup312.py --server imap.example.com:993 --ssl --user x@y.com \
  --pass @secret.txt --mbox-dir ./out --folders INBOX
```

- 没有测试套件。验证改动：`python -m py_compile <file>`，再手动跑 `--help` 和小范围 `--folders` 备份
- `.gitignore` 已忽略 `*.mbox`、`dist/`、`build/` — 不要提交本地测试产生的 mbox 或打包产物

## 核心机制（改逻辑前必读）

- **增量备份**：`scan_file()` 解析本地 mbox 已有的 `Message-Id` 集合，只下载服务器上不存在的消息。缺失 `Message-Id` 的消息用固定 UUID salt（`UUID` 常量）生成合成 ID — 改动 `UUID` 或 `MSGID_RE` 会破坏既有备份的增量行为
- 只做只读 IMAP 操作（read-only `SELECT` / `BODY.PEEK`）
- `--folders` 与 `--exclude-folders` 互斥
- Spinner 在 stdout 非 TTY 或 quiet 模式自动禁用。GUI 线程调用核心函数时同理：不要向 stdout 写进度，用回调或 logging 替代
- `main(argv)` 返回 int；密码支持 `@/path/to/file` 语法；312 版已移除内置压缩（mbox 事后自行压缩）

## 其他

- `README.md` 是上游英文文档；fork 层面的功能改动需注意同步或注明 fork 特性。
- 版本号嵌在 `imapbackup312.py` 的 `__version__`。
- MIT 许可证，贡献者名单在各脚本头部注释中，新文件保持同样的头部风格。

## Agent skills

### Issue tracker

Issue 以 markdown 文件形式保存在仓库内 `.scratch/<feature-slug>/` 目录下。See `docs/agents/issue-tracker.md`.

### Triage labels

使用五个默认角色标签（needs-triage、needs-info、ready-for-agent、ready-for-human、wontfix）。See `docs/agents/triage-labels.md`.

### Domain docs

Single-context：根目录 `CONTEXT.md` + `docs/adr/`。See `docs/agents/domain.md`.