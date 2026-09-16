# AGENTS.md

## 项目概况

`rcarmo/imapbackup` 的 fork：给这套只读 IMAP 备份工具加图形界面，最终打包成带安装路径选择的 Windows 安装程序，分发给公司非技术员工。

已完成：CLI 的 GUI 接缝（结构化 `report` 回调 + `should_stop` 协作停止，ADR 0003）、按输出格式独立判重（ADR 0004）、`--eml-dir` 逐封 `.eml` 输出、`imapbackup_gui.py` 单窗口 GUI。计划中：PyInstaller + Inno Setup 安装程序。

## 仓库结构

- 三个并行 CLI 脚本按目标 Python 版本划分，互不 import：
  - `imapbackup.py` — Python 2.x 遗留版（冻结，仍报 `1.4h`，不再更新）
  - `imapbackup38.py` — Python 3.8+ 过渡版（仍报 `1.4h`）
  - `imapbackup312.py` — Python 3.12+ 现代化版（argparse/dataclass/pathlib/logging），**新工作基于此**
- `imapbackup_gui.py` — Tkinter 单窗口 GUI 入口，复用 `imapbackup312.py` 的模块级函数（`connect_and_login`、`get_names`、`scan_file`/`scan_folder`、`download_messages` 等），**不复制核心逻辑**
- `CONTEXT.md` + `docs/adr/`（4 篇）记领域术语与架构决策；`docs/agents/` 是 agent 流程约定
- 无 `setup.py`/`pyproject.toml`/`requirements.txt`、无打包配置、无进版的测试与 lint 配置；CI 仅有 stale-bot 工作流。GUI 的验证脚本在 `.scratch/tkinter-gui/`（gitignore，不进 PR）

## 运行与验证

系统 `python` 是 Microsoft Store 占位符，**一律用 `.venv/Scripts/python.exe`**。

```bash
python imapbackup312.py --help
python imapbackup312.py --server imap.example.com:993 --ssl --user x@y.com \
  --pass @secret.txt --mbox-dir ./out --folders INBOX
python imapbackup_gui.py        # GUI，没有命令行参数
```

改完后的验证顺序（前四条都不需要联网）：

- `python -m py_compile <改动的 .py>`
- `.scratch/tkinter-gui/gui_headless_smoke.py` — GUI 无头冒烟（拉起真窗口 + 喂合成事件，140+ 项断言），改 GUI 必跑
- `.scratch/tkinter-gui/cli_output_parity.py` — CLI 终端输出与基线逐字节一致（`PARITY OK`），改核心必跑
- `.scratch/tkinter-gui/per_format_incremental_test.py` — 按格式判重回归（30 个场景全过）
- `.scratch/cli-folder-listing/folder_listing_probe.py` — 用 fake IMAP 服务器跑完整 `main()`，验文件夹列表与 `--folders`/`--exclude-folders` 匹配语义（26 项断言），改文件夹选择或输出排序必跑
- 视觉确认用 `.scratch/tkinter-gui/*_probe.py`（真窗口 + GDI 截图，不起网）
- `.scratch/tkinter-gui/real_gui_e2e.py` 需要真实 IMAP 账号，**默认不跑**（QQ 限流激进）
- 其余：手动跑 `--help` 和小范围 `--folders` 备份

`.gitignore` 已忽略 `*.mbox`、`*.eml`、`secret.txt`、`dist/`、`build/`、`.scratch/**` — 不要提交本地备份、凭证或探针产物。

**未经用户明确允许，不要执行 `git commit` 或 `git push`**（包括 `--amend` 和强推）。改完代码留在工作树里等用户确认，再按用户指示提交；也不要代用户创建分支或发 PR。分支命名 `<类型>/<完整描述>`（如 `feature/eml-output`，不用缩写）；push 前先确认 VPN 已开；PR 只发 `INSIDEBang/imapbackup-gui` 自己的 fork，**绝不发上游 `rcarmo/imapbackup`**。

## 核心机制（改逻辑前必读）

- **增量备份按输出格式独立判重**（ADR 0004）：任一**已选**格式缺这封就算待备份，写盘时只写还缺它的那个格式，每封只 fetch 一次。缺 `Message-Id` 的消息用固定 UUID salt（`UUID` 常量）生成合成 ID — 改动 `UUID` 或 `MSGID_RE` 会破坏既有备份的增量行为
- 只做只读 IMAP 操作（read-only `SELECT` / `BODY.PEEK`），包括缺 `Message-Id` 时的兜底 header 抓取
- `--folders` 与 `--exclude-folders` 互斥；按**分隔符段边界**匹配（精确全名或段前缀，`系统文件夹` 选中整棵子树，`INBOX` 不误中 `INBOX/archive`），中文显示名直接可用。include 0 匹配**报错并非 0 退出**并附可用文件夹列表，exclude 0 匹配只警告不退出
- CLI 的 Spinner 在 stdout 非 TTY 或 quiet 模式自动禁用；GUI 同理——通过 `report` 回调（结构化 dict）拿进度、`should_stop` 回调协作停止，**不要向 stdout 写进度**
- GUI 线程模型：工作线程跑 连接→枚举→扫描→下载→logout，`queue` 回投，UI 用 `after(100ms)` 轮询；**工作线程绝不碰 widget**
- `main(argv)` 返回 int；密码支持 `@/path/to/file` 语法；312 版已移除内置压缩（mbox 事后自行压缩）

## 其他

- `README.md` 是上游英文文档 + Fork Notes；fork 层面的功能改动要同步或在 Fork Notes 里注明，示例里不要写真实账号或路径（用 `your.name@example.com` 这类占位符）
- 版本号全项目统一：`imapbackup312.py` 与 `imapbackup_gui.py` 的 `__version__` 保持相同字面量（当前 1.6.0；后续优化走 1.6.x，换 GUI 框架才跳 1.7.0/2.0.0）。**任何版本改动必须经用户明确批准，代理不得擅自修改。**
- 命名分层：产品/代码标识符用连字符连写（仓库名、日志目录 `%LOCALAPPDATA%\imapbackup-gui\`、计划中的 `imapbackup-gui.exe`），窗口标题等显示名用空格式 `IMAP Backup GUI`；将来 PyInstaller `--name`、快捷方式名、安装程序 DisplayName 复用同一显示名
- 日志格式 `时间 | 级别 | 模块 | 函数:行号 | 内容`，写 `%LOCALAPPDATA%\imapbackup-gui\log\run-YYYY-MM-DD.log`（按天一个，卸载器整目录删除）；INFO=运行事件，DEBUG=逐封明细（`IMAPBACKUP_GUI_DEBUG=1`）
- MIT 许可证，贡献者名单在各脚本头部注释中，新文件保持同样的头部风格

## Agent skills

### Issue tracker

Issue 以 markdown 文件形式保存在仓库内 `.scratch/<feature-slug>/` 目录下。See `docs/agents/issue-tracker.md`.

### Triage labels

使用五个默认角色标签（needs-triage、needs-info、ready-for-agent、ready-for-human、wontfix）。See `docs/agents/triage-labels.md`.

### Domain docs

Single-context：根目录 `CONTEXT.md` + `docs/adr/`。See `docs/agents/domain.md`.
