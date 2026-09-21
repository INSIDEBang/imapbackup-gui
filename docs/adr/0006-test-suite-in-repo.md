# 0006 - 回归测试进仓库（`tests/` + pytest）

Status: accepted

五个可重复的回归脚本从 `.scratch/`（gitignore，只存在于各人本机）迁到仓库根目录的 `tests/`，用 pytest 组织，配置放在根目录 `pytest.ini`。断言总量基本不变（约 410 项），但从此任何 clone 都能一键跑，PR 里能看得到。

起因是断言只存在于维护者本机。`.scratch/.gitignore = *` 意味着那 400 多条断言不在任何 diff 里、不在任何 CI 里、换台机器就消失——而其中不少守护的是明确写下来的不变量（ADR 0004 的按格式判重、ADR 0003 的 `report` 回调契约、`--folders` 的段边界匹配）。一个 fork 的唯一价值是它相对上游的行为，这些行为没有任何进版的东西在守卫。

组织方式：**一个原始编号组（`# ---` 分隔的一段）对应一个 pytest 测试函数**。原来的脚本是「跑完全部、末尾汇总 N/M failed」，pytest 是「首条失败即停」；按组切分既保留了原本可读的分组结构，又让失败定位到组内。`check(name, cond, detail)` 直接变成 `assert cond, f"{name}: {detail}"`，名称进了断言消息，失败信息比原来更具体。

`tests/fakes.py` 把原先在七个脚本里逐字复制的 `FakeServer` 变体、`NullLog` 和 modified-UTF-7 编码器收成唯一实现。`tests/conftest.py` 只管 pytest 侧的管道（basetemp、日志态复位、console 捕获），不碰被测代码。

迁移过程中修掉了两处旧脚本的既有缺陷，不是改写断言：

- `.scratch/cli-folder-listing/folder_listing_probe.py` 从未重定向 `core.LOG_DIR`，它的 26 次 `main()` 调用把 INFO 写进了 `%LOCALAPPDATA%\imapbackup-gui\log\` 的真实当日日志。现在每个测试模块都重定向。
- `smoke_test.py:78` 的 `timestamp.dateheader` 断言自带 `# depends on local tz` 注释，用「日在 11 或 12、时在 2 或 10」的猜测区间绕开了时区——在 UTC-5 的机器上是 21 点，区间不成立。换成与时区无关的瞬间比较（`dt.astimezone(timezone.utc) == 常量`），断言变强而不是变松。
- 同一文件里 `config.legacy` 断言 `str(cfg.basedir).endswith("imapbackup-gui")`——那是仓库自己的名字带来的巧合，换个 cwd 跑就假失败。改为 `cfg.basedir == Path.cwd()`。
- `check("wiring.compact-nolines", True)` 是恒真断言，替换为真正的 `assert recs == []`。

## Considered Options

- **继续用 gitignored 的一次性脚本**：零改动，但断言仍然只在本机、仍然不在 diff 里。这正是本条要解决的。否决。
- **只把现有脚本从 `.scratch/` 挪到仓库里**：改动最小，但那些脚本是 `check()` + 手写骨架 + 末尾汇总，不进 pytest 就没有失败定位、没有 marker 隔离、没有 fixture 清理，而且 `.scratch/eml-output/smoke_test.py` 这类文件名恰好命中 pytest 默认的 `*_test.py` 收集规则，裸跑 `pytest` 直接报错。否决。
- **建 `pyproject.toml`**：可以把 Python 版本下限写成机器可读的 `requires-python`，测试发现规则也能写进去。但仓库不是可安装包，这个文件会凭空引入一份没人消费的构建配置。改放 `pytest.ini`——10 行、只服务 pytest。**接受的代价**：Python 版本要求仍然只存在于 README 与 docstring 的散文里，任何自动化工具读不到。
- **这次一起做 CI job**：`pytest -m "not gui"` 是不依赖显示环境的命令，加 workflow 只需新建一个文件、测试一行不改。但没有先谈清 runner 形态（GitHub Actions 的 ubuntu 跑不了 Tk，Windows runner 有配额问题），做出来大概率要返工。**接受的代价**：测试套件现在只在本机验证，没有机器替你跑。将来加 workflow 是纯增量。

## Consequences

- **`.scratch/` 与 `tests/` 双轨存在**，边界是「回归断言 vs 一次性观察」：`.scratch/` 里保留 17 个视觉/GDI 探针、4 个真账号探针与 15 张截图，那些是判断外观与真服务的观察工具，不是回归；往 `.scratch/` 里加断言会重新掉进「断言只在本机」的洞。
- **GUI 套件被 `@pytest.mark.gui` 隔离**，57 个非 GUI 测试 + 20 个 GUI 测试。几何断言（`winfo_width == 960/736`、缩进量落在 10..40、`winfo_rooty` 的先后序）是在当前机器的 DPI 与 ttk 主题下标定的，marker 内**不做**环境放宽——放宽等于把断言写松，换环境才挂的问题会一直潜伏到换环境那天。
- **`gui_headless_smoke` 的 20 个测试按定义顺序共享同一个 module 级 `app` 实例**，前一节的界面状态是后一节的前置条件（连接 → 文件夹树 → 过期 → 工具栏 → 级联 → … → 状态条 → 气泡）。这是原脚本的既有结构，迁移时保留并在文件 docstring 里写明「别重排」。代价是重排测试函数需要理解这个链条；收益是不必把 17 段状态机拆成 17 个各自的搭建。
- **`pytest.ini` 的 `testpaths = tests` 是硬需求**，不是整洁性设置：pytest 默认收集 `*_test.py`，历史上 `.scratch/` 里三个已退役脚本恰好命中该命名，不指向 `tests/` 会直接报错。现在 `.scratch/` 里已无匹配文件，但规则仍然必要。
- **没有 CLI 输出逐字节的回归门。** 原 `.scratch/tkinter-gui/cli_output_parity.py` 与它的基线副本 `.scratch/old_head/imapbackup312.py`（不入库、跑之前要 `git show` 重建）已删除。要恢复同等能力需要一个 golden 基线快照测试：把已知良好版本的 CLI 输出字节提交为 fixture，用假 IMAP 服务器跑出当前输出逐字节比对。这是另一个切片，本次未做。
- **临时目录不再进仓库**：`conftest.py` 在 `pytest_configure(tryfirst=True)` 注入 `config.option.basetemp` 指向 `%TEMP%\imapbackup-gui-tests`。不是可选的优化——旧配置留下的 `%TEMP%\pytest-of-<用户>\pytest-current` 是指向别处的系统符号链接、ACL 只授 SYSTEM 与 Administrators，pytest 9 的 `cleanup_dead_symlinks()` 在会话收尾对它调 `Path.resolve()` 会抛 `PermissionError`，**整个套件以退出码 1 结束（即使全部测试通过）**。旧配置把 basetemp 指到仓库内的 `.pytest-tmp/` 是孤儿残留，18 个空目录已删除。注意 `basetemp` 不是 pytest 的 ini 选项，写进 `pytest.ini` 会被静默忽略；必须在 hook 里注入，且要在 `pytest_sessionstart` 之前（`tryfirst=True`），因为 `TempPathFactory` 那时已经构造完毕并捕获了 `_given_basetemp=None`。
- **`logging.basicConfig` 在 root 已有 handler 时是 no-op**，这是测试里最反直觉的一层：pytest 的 `LogCaptureHandler` 占着 root，所以 `configure_logging` 里的 `basicConfig` 从不安装 stderr 的 StreamHandler，CLI 的 INFO 记录只进了 pytest 的日志缓冲，`listing()` 之类的 stderr 解析一律拿到空串。`conftest.py` 的 `clean_logging` 因此只摘 **FileHandler**、保留 pytest 自己的捕获 handler——失败时还能看到 `Captured log call`；console 输出由 `console_capture` 显式装一条 `StreamHandler` 供解析。
- 版本号未改动，仍为 1.6.0。
- CI 缺席期间，`AGENTS.md` 的验证顺序是本套件的唯一执行约定。
- 本篇里的测试数、断言数与 `.scratch/` 清单都是写下时的快照（套件还在长，数字会漂）：当前数值以 `AGENTS.md` 为准，ADR 只负责记录当时的决策与代价。
