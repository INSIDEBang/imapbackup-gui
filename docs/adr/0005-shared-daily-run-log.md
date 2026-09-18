# 0005 - CLI 与 GUI 共用同一日运行日志文件

Status: accepted

命令行也写 `%LOCALAPPDATA%\imapbackup-gui\log\run-YYYY-MM-DD.log`，与图形界面同一个文件、同一套格式串。共享的是**构造**——路径与格式常量、`make_log_file_handler()` 工厂放在 `imapbackup312.py`，两个入口各自把 handler 装到自己的 logger 上；共享的不是**安装动作**，所以不存在两个入口各装一份 FileHandler 往同一日文件重复写的问题。

起因是 CLI 完全没有盘上痕迹。它只有 `logging.basicConfig` 到 stderr，不写日志、不写配置、不写临时文件；跑完就没了。而 `sys.excepthook = cli_exception` 有个 `sys.stdin.isatty()` 门控，计划任务、CI、任何非交互跑根本没装这个 hook。于是「计划任务里的备份半夜失败了，为什么」这个问题，在现有结构里一处都查不到——而这类失败恰恰是最需要事后回答的。

`--quiet` 的语义随之需要重新划分。原本 quiet 把 root logger 压到 WARNING，任何挂在 root 上的文件 handler 都收不到 INFO。所以 console 级别下沉到 StreamHandler 自身：`basicConfig(level=DEBUG)` 只是放行 root，`quiet`/`verbose` 算出的级别只设在那一个 StreamHandler 上。stderr 可见的记录与改动前完全一致，文件 handler 则恒 INFO，不受 quiet 影响。

失败记录走一个**不传播**的专用 logger `imapbackup.runlog`，它只挂文件 handler。原因是解释器对 `SystemExit` 已经把消息打到 stderr 了，再用普通 logger 记一次就是同一条消息打两遍。降级时（工厂返回 `None`）不给它挂 handler，记录顺着传播回落到 root 的 StreamHandler——只剩 stderr 时正好用上 stderr。

## Considered Options

- **CLI 用独立文件名**（如 `run-cli-YYYY-MM-DD.log`）：CLI 与 GUI 的日志不交错，但排查一次运行要开两个文件去猜是哪个，AGENTS.md 里「`run-YYYY-MM-DD.log` 是唯一日志」的说法也得改。否决。
- **CLI 继续不写文件**：零改动，但计划任务与 CI 里的失败仍然是零盘上痕迹——这正是本条要填的洞。否决。
- **共享整个 `configure_logging`**（安装动作也共享）：代码最少，但「装几个 handler」变成了共享契约。当前 GUI 从不调用 CLI 的 configure，所以只是理论风险；可一旦某进程真跑两个入口，同一日文件上就会挂两个 FileHandler，每行记两次。共享构造把这道防线留在结构上而不是纪律上。
- **失败也照常用普通 logger 记**：三行代码，但 `SystemExit` 的消息会在 stderr 上出现两次（一次时间戳带前缀、一次解释器原始输出），失败路径恰好是最不该有噪音的地方。否决。

## Consequences

- **argparse 阶段的失败天然不入日志**，且这是有意为之：参数解析在 `configure_logging` 之前，那时还没有任何 handler。`--help`、`--folders` 与 `--exclude-folders` 互斥报错、`--keyfile` 不配对这类都在此列。
- `--quiet` 仍会减少文件记录，但方式不同：文件拿到的是 INFO 级别的记录，而核心里约 18 处日志调用被 `if not quiet:` 包在**调用点**上，quiet 时这些记录根本不会被创建。所以 quiet 运行里文件仍能查到连接与登录，但查不到逐文件夹与断开。
- `%LOCALAPPDATA%` 按 Windows 账号计算。计划任务以 SYSTEM 或另一个管理员账号跑时，日志写到那个账号的目录，不在交互用户的目录里。GUI 一直有这个特性，CLI 继承它。
- 一条日志记录可能占多个物理行：文件夹列表与 traceback 都含换行，续行不带时间戳。任何解析这个文件的工具都应按「时间戳开头 = 新记录」分组，不能按行。
- 来源列现在可能出现三个 logger 名：`imapbackup`（运行事件）、`imapbackup.runlog`（失败）、`gui`（界面）。该列是 logger 名而非 Python 模块名，`%(name)-10s` 是最小宽度，`imapbackup.runlog` 会超出 10 字符使分隔符错位——只发生在失败行上。
- 不轮转、不截断、不归档：按天一个文件、append、永不清理，卸载器整目录删除是唯一清理机制。
- 每次运行第一行是版本横幅（`imapbackup v<版本> starting`）。共享文件里 CLI 与 GUI 的运行交错在一起，没有这个标记就无法判断一段记录属于哪次运行；它同时是版本归属的唯一凭据（此前 CLI 的 `__version__` 是死数据，从不打印）。`--quiet` 下横幅只进文件、不上 stderr，与 quiet 语义一致。
- CLI 核心的 `imapbackup312.py` 目前没有任何逐封 DEBUG 日志（全文件只有 `Logger initialized` 一条），所以 `-v` 对文件内容没有可见效果。「文件恒 INFO」与「文件恒 DEBUG」在当前代码下输出完全相同；要逐封明细需另开切片。
- CLI 侧日志消息为英文，与该模块其余日志一致；GUI 侧为中文。同一文件里两种语言并存，靠来源列区分。
- `cli_output_parity.py` 的基线 `.scratch/old_head/imapbackup312.py` 不入库，跑之前需要 `git show HEAD:imapbackup312.py` 重建。
