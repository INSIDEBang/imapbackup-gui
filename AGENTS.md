# AGENTS.md

## 项目定位

这是 `rcarmo/imapbackup` 的 Windows GUI fork：保留 CLI，为非技术用户提供只读 IMAP 备份、Tkinter GUI 和安装程序。

- 新功能基于 `imapbackup312.py`（Python 3.12+）。`imapbackup.py` 和 `imapbackup38.py` 是互不导入的遗留版本，除非用户明确要求，否则不修改。
- `imapbackup_gui.py` 复用 `imapbackup312.py` 的核心能力；扫描、判重和下载逻辑只保留一份，不创建 GUI 专用副本。
- 运行时代码保持 Python 标准库依赖。新增运行时依赖或包管理元数据前须获得用户明确批准。
- 产品标识使用 `imapbackup-gui`，用户可见名称使用 `IMAP Backup GUI`。

## 工作原则

- 使用 `CONTEXT.md` 中的规范术语；请求若与已接受的 ADR 冲突，先指出冲突和影响。
- 保持改动聚焦；同步更新受影响的测试、用户文档和调用点。用户可见行为、CLI 参数或安装行为变化时，更新 `README.md` 的 Fork Notes。
- 默认使用假服务器和占位账号。未经用户明确授权，不连接真实 IMAP 服务，也不读取凭证、真实 `.eml`/`.mbox`、运行日志或真实账号探针。
- 可重复的回归断言放入 `tests/`；`.scratch/` 只存本地 issue、一次性探针和人工观察材料，正常开发与验证不得依赖其中的文件。
- 不提交凭证、邮件、备份、运行日志、构建产物或探针产物。示例统一使用虚构账号与路径。

## 核心不变量

- IMAP 操作保持只读：使用 read-only `SELECT` 和 `BODY.PEEK`，不得改变服务器上的消息状态。
- 增量判重按已选输出格式独立计算：任一格式缺失即为待备份；每封消息只 fetch 一次，只补写缺失格式（ADR 0004）。
- `UUID` 盐值和 `MSGID_RE` 属于存量备份兼容契约；修改时必须说明迁移方案并补回归测试。
- `--folders` 与 `--exclude-folders` 互斥，并按服务器分隔符的段边界匹配。include 零匹配以非零状态退出并列出可用文件夹；exclude 零匹配只警告。
- 保持现有 CLI 参数、退出码及 quiet/console 行为，除非用户明确要求破坏性变更。`main(argv)` 返回整数退出码。
- 卸载与清理逻辑不得触碰用户选择的备份目录。应用数据目录新增非日志内容时，必须同步修改卸载文案或收窄删除范围。

## 按任务读取

- 修改 eml/mbox 布局、账号目录、消息标识或判重：读取 `CONTEXT.md` 和 ADR 0001、0002、0004。
- 修改 `report`、`should_stop` 或核心与 GUI 的接缝：读取 ADR 0003。
- 修改任何 GUI 行为：读取 `docs/agents/gui.md` 和相关 `tests/test_gui_smoke.py` 用例。
- 修改日志路径、级别、handler 或失败记录：读取 ADR 0005 和 `tests/test_cli_file_logging.py`。
- 修改 pytest 配置、fixture 或测试组织：读取 ADR 0006、`pytest.ini` 和 `tests/conftest.py`。
- 修改安装、卸载、AppId 或应用数据删除行为：读取 ADR 0007、`CONTEXT.md` 和 `packaging/imapbackup-gui.iss`。
- 创建或更新本地 issue/spec：读取 `docs/agents/issue-tracker.md`；执行 triage 时再读取 `docs/agents/triage-labels.md`。

## 跨文件一致性

- `imapbackup_gui.py` 的 `APP_MUTEX_NAME` 必须与 `packaging/imapbackup-gui.iss` 的 `AppMutex` 完全一致。
- 版本变更须经用户明确批准，并同步 `imapbackup312.py`、`imapbackup_gui.py`、`packaging/imapbackup-gui.iss` 和 `packaging/build.bat`。
- 保留已有许可证头；新增入口脚本时参考相邻入口文件的头部风格。

## 运行与验证

所有 Python 命令使用 `.venv/Scripts/python.exe`。先运行与改动最相关的检查，再扩大范围：

```bash
# 修改 Python 文件
.venv/Scripts/python.exe -m py_compile <改动的.py>

# 核心或 CLI
.venv/Scripts/python.exe -m pytest -m "not gui" -q

# GUI（需要可用的 Windows Tk 显示环境）
.venv/Scripts/python.exe -m pytest -m gui -q

# 完整回归
.venv/Scripts/python.exe -m pytest -q
```

修改 `packaging/`、互斥量或图标路径时，在 `cmd` 中运行 `packaging\build.bat`；安装与卸载行为按 ADR 0007 人工验收。

## Git 与发布

- 未经用户明确要求，不创建分支，不执行 commit、push、tag 或 PR；包括 amend 和强推。
- 分支名使用 `<type>/<full-description>`。PR 只发往 `INSIDEBang/imapbackup-gui`，不得发往上游 `rcarmo/imapbackup`。
- commit message 使用英文 Conventional Commit：`<type>[(<scope>)]: <description>`；正文也使用英文，scope 可省略。
