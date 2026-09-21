# AGENTS.md

## 项目定位

这是 `rcarmo/imapbackup` 的 Windows GUI fork：保留 CLI，为非技术用户提供只读 IMAP 备份、Tkinter GUI 和安装程序。

- 新功能基于 `imapbackup312.py`（Python 3.12+）。`imapbackup.py` 和 `imapbackup38.py` 是互不导入的遗留版本，除非用户明确要求，否则不修改。
- `imapbackup_gui.py` 复用 `imapbackup312.py` 的核心能力；扫描、判重和下载逻辑只保留一份，不创建 GUI 专用副本。
- 运行时代码保持 Python 标准库依赖。新增运行时依赖或包管理元数据前须获得用户明确批准。
- 产品标识使用 `imapbackup-gui`，用户可见名称使用 `IMAP Backup GUI`。

## 工作原则

<<<<<<< HEAD
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
=======
- 三个并行 CLI 脚本按目标 Python 版本划分，互不 import：
  - `imapbackup.py` — Python 2.x 遗留版（冻结，仍报 `1.4h`，不再更新）
  - `imapbackup38.py` — Python 3.8+ 过渡版（仍报 `1.4h`）
  - `imapbackup312.py` — Python 3.12+ 现代化版（argparse/dataclass/pathlib/logging），**新工作基于此**
- `imapbackup_gui.py` — Tkinter 单窗口 GUI 入口，复用 `imapbackup312.py` 的模块级函数（`connect_and_login`、`get_names`、`scan_file`/`scan_folder`、`download_messages` 等），**不复制核心逻辑**
- `CONTEXT.md` + `docs/adr/`（6 篇）记领域术语与架构决策；`docs/agents/` 是 agent 流程约定
- `tests/` + 根目录 `pytest.ini`：进版回归套件（79 个测试 / 455 项断言），由 `.venv/Scripts/python.exe -m pytest` 跑，配置见 ADR 0006
- `packaging/` — Windows 安装程序构建：PyInstaller spec（GUI `imapbackup-gui.exe` 与 CLI `imapbackup-gui-cli.exe` 双 EXE 共享一份 `_internal/` 运行库）、Inno Setup 中文脚本 `.iss`、`build.bat` 一条龙、图标与中文语言包；产物在 `dist/`（gitignore）
- 无 `setup.py`/`pyproject.toml`/`requirements.txt`（不入 pip 生态，Windows 打包走 `packaging/`）、无 lint 配置；CI 仅有 stale-bot 工作流，测试套件**没有 CI job**（有意，见 ADR 0006）。`.scratch/`（gitignore）是本地一次性探针：视觉/GDI 截图与真账号 e2e，不重复 `tests/` 的回归断言
>>>>>>> 66e44e2 (docs: record the GUI right-edge baseline rule)

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

<<<<<<< HEAD
## Git 与发布

- 未经用户明确要求，不创建分支，不执行 commit、push、tag 或 PR；包括 amend 和强推。
- 分支名使用 `<type>/<full-description>`。PR 只发往 `INSIDEBang/imapbackup-gui`，不得发往上游 `rcarmo/imapbackup`。
- commit message 使用英文 Conventional Commit：`<type>[(<scope>)]: <description>`；正文也使用英文，scope 可省略。
=======
- `python -m py_compile <改动的 .py>`
- `.venv/Scripts/python.exe -m pytest -m "not gui" -q` — 57 个测试，不依赖显示环境。**改核心跑这个**：按格式判重、CLI 终端输出格式、文件夹列表与 `--folders`/`--exclude-folders` 匹配语义、CLI 文件日志、`main()` 控制流与退出码
- `.venv/Scripts/python.exe -m pytest -m gui -q` — 22 个 GUI 冒烟测试（拉起真窗口 + 喂合成事件），**改 GUI 必跑**。几何断言（`winfo_width`、`winfo_rootx`、`winfo_rooty` 序）在当前机器的 DPI/ttk 主题下标定，换环境可能挂，这也是它被 `gui` marker 隔离的原因
- `.venv/Scripts/python.exe -m pytest -q` — 全量 79 个
- 改 GUI 打包面（互斥量、图标路径）或 `packaging/` 时：在 cmd 里跑 `packaging\build.bat` 重建（PyInstaller + ISCC），产出 `dist\installer\imapbackup-gui-setup-<版本>.exe`。本机起不了 Inno 生成的 setup exe，装卸验证只能用户双击
- 视觉确认用 `.scratch/tkinter-gui/*_probe.py`（真窗口 + GDI 截图，不起网）：外观判断走这里，回归断言不再往 `.scratch/` 加
- `.scratch/tkinter-gui/real_gui_e2e.py` 需要真实 IMAP 账号，**默认不跑**（QQ 限流激进）
- 其余：手动跑 `--help` 和小范围 `--folders` 备份

**仓库没有「CLI 终端输出与基线逐字节一致」的回归门。** 原 `.scratch/tkinter-gui/cli_output_parity.py` 及其基线副本已随测试迁移删除；要恢复同等能力需要一个 golden 基线快照测试（把已知良好版本的输出字节提交为 fixture），那是另一个切片。

`.gitignore` 已忽略 `*.mbox`、`*.eml`、`secret.txt`、`dist/`、`build/`、`.scratch/**` — 不要提交本地备份、凭证或探针产物。

**未经用户明确允许，不要执行 `git commit` 或 `git push`**（包括 `--amend` 和强推）。改完代码留在工作树里等用户确认，再按用户指示提交；也不要代用户创建分支或发 PR。分支命名 `<类型>/<完整描述>`（如 `feature/eml-output`，不用缩写）；push 前先确认 VPN 已开；PR 只发 `INSIDEBang/imapbackup-gui` 自己的 fork，**绝不发上游 `rcarmo/imapbackup`**。

**commit message 一律用英文**：标题沿用 `<type>(<scope>): <英文描述>` 前缀（如 `feat(gui): ...`），正文（body）也用英文；需要在描述里提到中文界面文案或中文术语时，放进英文句子内加引号原样引用即可。历史提醒：`bad0e01`（2026-09-16）起的 8 条提交误用了中文，那是偏离、不是先例——该提交之前的历史全为英文。

## 核心机制（改逻辑前必读）

- **增量备份按输出格式独立判重**（ADR 0004）：任一**已选**格式缺这封就算待备份，写盘时只写还缺它的那个格式，每封只 fetch 一次。缺 `Message-Id` 的消息用固定 UUID salt（`UUID` 常量）生成合成 ID — 改动 `UUID` 或 `MSGID_RE` 会破坏既有备份的增量行为
- 只做只读 IMAP 操作（read-only `SELECT` / `BODY.PEEK`），包括缺 `Message-Id` 时的兜底 header 抓取
- `--folders` 与 `--exclude-folders` 互斥；按**分隔符段边界**匹配（精确全名或段前缀，`系统文件夹` 选中整棵子树，`INBOX` 不误中 `INBOX/archive`），中文显示名直接可用。include 0 匹配**报错并非 0 退出**并附可用文件夹列表，exclude 0 匹配只警告不退出
- CLI 的 Spinner 在 stdout 非 TTY 或 quiet 模式自动禁用；GUI 同理——通过 `report` 回调（结构化 dict）拿进度、`should_stop` 回调协作停止，**不要向 stdout 写进度**
- GUI 线程模型：工作线程跑 连接→枚举→扫描→下载→logout，`queue` 回投，UI 用 `after(100ms)` 轮询；**工作线程绝不碰 widget**
- GUI 单实例互斥量：`imapbackup_gui.py` 的 `APP_MUTEX_NAME` 与 `packaging/imapbackup-gui.iss` 的 `AppMutex` **必须逐字一致**（安装/卸载器靠它探测运行中的实例），改一处必须同步另一处
- GUI 按钮图标：彩色图形（开始按钮绿三角 `PLAY_ICON_COLOR`、停止按钮红方块 `STOP_ICON_COLOR`）由代码用 `PhotoImage` 逐像素画，**不引图片资产**（字体字形只有一种颜色、且禁用态会被系统灰盖掉，所以字形做不到）。位图必须由 Python 侧持引用，否则被 GC 后图标变空白。尺寸由 `_icon_size()` 从字体行高推导、不封顶，图标始终比文字矮一点。加载动画只**一套帧与节奏**（`SPINNER_FRAMES` 盲文点阵 + `SPINNER_INTERVAL_MS`，连接与备份都经 `_set_loading_frame()` 出帧，改一处两处一起变），但两个按钮各有一条帧链与 job 槽（`_connect_spinner_tick` / `_run_spinner_tick`），**要改节奏改常量、要改观感改共用出帧函数，别只改其中一条链**（用户要求两处加载图标一致；备份中开始按钮撤图标、文案变「正在备份…」）。两个按钮不设 `-width`：带图标的 `tk.Button` 里该选项按像素解释、纯文字时按字符数，写死会塌成图标宽
- GUI 右边界基线：内容左边距与右边距都是 8px（上半区 `outer` 与下半区 `run` 都 `pack(padx=8)`），**单个面板不要再给自己加右 padx**——「邮箱文件夹」面板曾多留 8px，于是它的边框比下方区域窄一块、面板内滚条又比下方滚条左 13px；面板里的竖直滚条右边不留白、贴住面板边框线（与「邮件列表/运行日志」页签里的滚条一致）。`tests/test_gui_smoke.py::test_right_edges_share_one_baseline` 用控件间相对坐标锁住这三条关系，动布局先跑它
- 「邮箱文件夹」面板的滚轮：**窗口级单入口**——`root.bind("<MouseWheel>", _on_wheel)`（root 的路径就是主窗口内每个控件的 toplevel bindtag），事件控件沿 `.master` 上溯命中 `folder_group` 才接管、否则返回 `None` 放行（节奏与下方「邮件列表」的 1 行/格一致；运行日志约 2.4 行/格是 Tk Text 的默认，未动）。**别改回**绑在画布上：Tk 只把事件投给指针正下方的控件、不冒泡，绑画布时行里的 Label/Frame 收不到，面板 90% 面积滚不动（这正是当初的现象）。面板内滚条另绑同一个入口并 `return "break"`，压掉 `TScrollbar` 自带的 4 units（实测≈98px，比内容区快）——`break` 也会挡掉 root 那条，所以滚条必须由这个函数自己完成滚动。一格 = `FOLDER_WHEEL_ROWS` 行（当前 1 行，与下方邮件列表同节奏；用户 2026-09-21 定），**按行对齐**靠画布 `yscrollincrement`＝行距（`_sync_folder_row_increment` 由 `folder_inner` 的 `<Configure>` 驱动；重建当刻量到的是 Tk 占位值，量不到就归 0），顺带滚条箭头一格也成一整行、拖滚条同样停在行边界；不足一格的 delta 按 `WHEEL_DELTA` 累加（高分辨率鼠标/触摸板）。回归断言 `tests/test_gui_smoke.py::test_folder_panel_wheel_covers_the_whole_panel`，动滚轮/布局先跑它
- **日志：CLI 与 GUI 共用同一日文件**（ADR 0005）。路径/格式常量与 `make_log_file_handler()` 工厂在 `imapbackup312.py`，两个入口**各自安装** handler——共享构造，不共享安装。CLI 把 console 级别下沉到 StreamHandler 自身（`basicConfig(level=DEBUG)` 只是放行 root，否则 `--quiet` 会在 logger 层把 INFO 全丢掉），文件 handler 恒 INFO，所以 `--quiet` 只压 stderr、不削弱文件。失败/异常/中断走**不传播**的 `imapbackup.runlog` logger，只进文件：`SystemExit` 的消息解释器已打到 stderr，再打一遍就是重复。**argparse 阶段在 `configure_logging` 之前，参数校验类失败天然不入日志**，这是有意的。日志目录建不出来时工厂返回 `None`，降级为不写文件并警告到 stderr，**备份照常跑**
- `main(argv)` 返回 int；密码支持 `@/path/to/file` 语法；312 版已移除内置压缩（mbox 事后自行压缩）

## 其他

- `README.md` 是上游英文文档 + Fork Notes；fork 层面的功能改动要同步或在 Fork Notes 里注明，示例里不要写真实账号或路径（用 `your.name@example.com` 这类占位符）
- 版本号全项目统一，4 处同一字面量：`imapbackup312.py` 与 `imapbackup_gui.py` 的 `__version__`、`packaging/imapbackup-gui.iss` 的 `#define MyAppVersion`（同时决定注册表 AppVersion 与安装程序文件名）、`packaging/build.bat` 的完成提示文案（当前 1.6.0；后续优化走 1.6.x，换 GUI 框架才跳 1.7.0/2.0.0）。**任何版本改动必须经用户明确批准，代理不得擅自修改。**
- 命名分层：产品/代码标识符用连字符连写（仓库名、应用数据目录 `%LOCALAPPDATA%\imapbackup-gui\`、`imapbackup-gui.exe` / `imapbackup-gui-cli.exe`），窗口标题等显示名用空格式 `IMAP Backup GUI`；PyInstaller spec、快捷方式名、安装程序 DisplayName 已复用同一显示名
- 日志格式 `时间 | 级别 | 来源 | 函数:行号 | 内容`——`来源` 是 logger 名（`imapbackup`、`imapbackup.runlog`、`gui`），不是 Python 模块名；共享同一文件后它就是「这行来自 CLI 还是 GUI」的判别位。写 `%LOCALAPPDATA%\imapbackup-gui\log\run-YYYY-MM-DD.log`（按天一个，卸载向导首页复选框默认勾选后整目录删除），CLI 与 GUI 同写，文件 handler 恒 INFO（`--quiet` 只压 stderr）。INFO=运行事件，DEBUG=逐封明细（仅 GUI 侧，`IMAPBACKUP_GUI_DEBUG=1`；CLI 核心目前无逐封 DEBUG 日志）。消息含换行时（文件夹列表、traceback）续行不带时间戳，解析要按「时间戳开头 = 新记录」分组
- MIT 许可证，贡献者名单在各脚本头部注释中，新文件保持同样的头部风格

## Agent skills

### Issue tracker

Issue 以 markdown 文件形式保存在仓库内 `.scratch/<feature-slug>/` 目录下。See `docs/agents/issue-tracker.md`.

### Triage labels

使用五个默认角色标签（needs-triage、needs-info、ready-for-agent、ready-for-human、wontfix）。See `docs/agents/triage-labels.md`.

### Domain docs

Single-context：根目录 `CONTEXT.md` + `docs/adr/`。See `docs/agents/domain.md`.
>>>>>>> 66e44e2 (docs: record the GUI right-edge baseline rule)
