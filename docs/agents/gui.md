# GUI 变更约束

修改 `imapbackup_gui.py` 的任何用户可见行为或交互状态前读取本文，并以 `tests/test_gui_smoke.py` 为回归契约。

## 核心接缝与线程

- GUI 复用 `imapbackup312.py` 的连接、枚举、扫描、判重和下载逻辑，不复制核心实现。
- 工作线程执行连接、枚举、扫描、下载和 logout，通过 `queue` 回投事件；主线程以 `after()` 轮询。工作线程不直接访问 widget。
- GUI 通过结构化 `report` 获取进度，通过 `should_stop` 协作停止；调用核心时保持 `quiet=False`、`nospinner=True`。具体契约见 ADR 0003。
- 停止只在下一封消息或下一个文件夹前生效，当前 I/O 可能持续到 socket 超时。界面只使用“停止”，不引入“暂停/继续”语义。

## 界面设置与恢复

- 应用数据目录是 `%LOCALAPPDATA%\imapbackup-gui\`，由程序首次运行时创建；它承载运行日志与界面设置。安装程序不预创建该目录，卸载时只处理这一目录，不触碰用户选择的备份根目录和其下的账号目录。
- 界面设置按 Windows 用户全局保存，不按账号、邮箱文件夹或备份根目录分层。设置文件使用带版本号的通用结构，同时兼容旧的五列宽度格式。邮件列表五列的宽度在拖动释放后更新内存值并持久化，下一次打开对所有账号与文件夹生效；窗口缩放时所有列按比例调整并遵守各自原生最小宽度。主窗口保存正常状态的宽高和位置、最大化状态；最大化时保留最大化前最后一次正常几何，启动时先恢复正常几何再恢复最大化。窗口关闭时统一保存一次，保存失败只记录 warning 并继续退出。旧位置在当前显示器布局中完全不可见时只回退位置到 Tk 默认位置，保留有效尺寸。滚动位置和排序状态不在本次范围内。
- 账户列表（连接设置、DPAPI 密码、文件夹与格式勾选）、全局备份保存位置与当前选中账户随同一原子写落盘在 `accounts` / `backup_root` / `selected_account`（见下节「多账户」）。文件夹勾选存 IMAP 文件夹名，缺省或全部失效时回落 `INBOX`。密码框显示固定掩码 `PASSWORD_MASK_TEXT`（8 颗星，不回显内容也不透露真实位数）：原样保存 = 不修改已存密文，清空 = 删除已存密码，改内容 = 写入新密码；账户名称不参与任何路径。
- 邮箱密码以系统 DPAPI 加密形式保存（base64 密文进同一文件，仅当前用户、当前机器可解；防配置文件泄露，不防以本用户身份运行的进程）。空密码不落盘；非 Windows 平台密码不持久化、其余字段照常；解密失败（换机器/换账户/文件损坏）视为该账户没有密码，连接时提示去设置页重填，不影响启动与其他账户。
- 设置文件不存在、无法解析、字段类型不符或版本不兼容时按节降级：整份不可解析时用内置默认值启动并继续提供 GUI；坏节丢弃、坏字段回落默认值（`accounts` 非列表整节丢弃，单个账户缺 `id` 跳过该账户）。不得因为设置损坏阻止连接或备份。应把可诊断信息写入运行日志，日志不可用时只向现有 console/状态提示降级，不弹出阻断式错误框。
- 写入失败时保留当前进程中的设置，界面操作和备份继续；下一次启动重新使用上一次成功保存的设置或默认值。写入失败只产生可诊断警告，不覆盖一份仍可读取的旧设置文件，也不触碰备份输出。
- 卸载向导分别提供“删除运行日志”和“删除界面设置”选项，默认均勾选；取消向导或中途取消进度时两者都保留，真静默卸载按默认值删除两者。用户备份永远不在范围内（ADR 0007、ADR 0008）。

## 多账户

改账户模型、备份页版式或批量运行前读这一节；完整决策见 ADR 0010。

- **账户是唯一配置单位**：`self.accounts` 是账户字典列表（`id` / `name` / `user` / `server` / `ssl` / `port` / `password_dpapi` / `folders` / `mbox` / `eml`），引用一律用创建时生成的稳定 `id`（名称可改、邮箱地址可重复）。`parse_account` / `account_record` 是读写这两个方向的唯一入口；`account_display` 给出显示名（名称→邮箱地址→占位名）。
- **账号目录名永远取邮箱地址**：`_backup_account` 用 `core.sanitize_segment(v["user"], …)` 拼 `<备份根>/<邮箱地址>/…`。账户名称只是显示名，任何路径、输出、判重逻辑都不得读它（AGENTS.md 的不变量）。
- **界面分工**：设置页「账户」= 账户增删改 + 连接设置表单 +「保存」（保存是显式动作，校验邮箱地址/收件服务器/端口后落盘）；「备份」= 全局唯一的备份保存位置。备份页 = 左账户列表（`account_list`，固定 160px）+ 右账户备份详情；详情里连接信息与备份位置是**只读信息卡**（行构成照 foxmail：邮箱类型 / 邮箱地址 / 收件服务器 + SSL + 端口同排；**没有密码行**，密码只在设置页表单里维护），唯一可编辑项是格式复选框与文件夹勾选树。备份页不得出现 `tk.Entry`（`test_backup_detail_has_no_entry_widgets` 守着）。
- **账户行是自绘的**：一行 = 复选框 + 名称（`_make_account_row`），不是一个 Listbox——点行切详情、点复选框只翻勾选，两个动作的命中区必须分开。行底色 `ACCOUNT_ROW_BG`（对话框灰），选中行整行 `SystemHighlight`（与原 Listbox 的选中蓝同一个系统色，`_render_account_rows` 上色，选中行连复选框一起换底色）。勾选集合 `self._checked_accounts` 是纯界面态、不落盘：`_refresh_account_list` 里同步——删掉的账户不再占勾选，上一次画列表时还没有的账户默认勾上（「勾」是默认态，`_on_account_add` 只负责开户）。复选框的 `padx/pady` 清零、右侧零留白，名称紧贴控件的右缘（Tk 在复选框控件里固定留一圈给「文字」的内边距，那点空隙收不掉）。
- **账户列表装不下才挂滚条**：列表在画布 `account_canvas` 里（`account_scroll` 贴面板内沿，与「邮箱文件夹」同款）；`_sync_account_scroll` 比内容请求高度与画布高度决定滚条去留——行数变化后延到 `after_idle` 量（当场量到的还是旧值），窗口缩放走画布 `<Configure>`。画布单位 `yscrollincrement` = 实测行距，滚轮一格一行（`_on_wheel` 的账户列表分支，与文件夹面板同节奏）。
- **一套共享控件 + 每账户状态字典**：`self._states[account_id]` 存 `snapshot`（服务器、地址、密码密文）/ `folder_rows` / `open_keys` / `messages` / `row_index` / 计数 / 状态文案 / 进度。`_switch_account` → `_render_account_detail` 负责「收当前账户进度 → 重画目标账户」；`self.folder_rows`、`self._folder_nodes`、`self._folder_clickables` 始终指向可见账户的那一份。`_visible_state()` 对存在的账户按需建立状态。
- **连接不再是用户动作**（ADR 0011）：移除「连接并加载文件夹」按钮与整套连接状态机（`connect_state`、连接 spinner、连接提示行、stale 概念）。文件夹清单靠**静默加载**（`_maybe_load_folders`）按需拉取——打开应用 / 切账户 / 保存账户后各触发一次，同一账户去重、连接参数没变（快照口径）则不重拉。失败在文件夹区提示并给恢复入口：工具栏右上角的 **⟳ 刷新按钮**（icon-only 字形，`_on_refresh_folders` 清掉快照强制重拉）；密码缺失 / 认证失败再弹**密码补录窗**（复用预检的 `_ask_password`，补完自动重拉；同账户同快照只弹一次，手动刷新必再弹，`state["password_prompted"]` 记忆）。真正的连接仍在备份预检时做。
- **每账户独立进度**：事件都带 `account_id`——`folders_loaded` / `totals` / `message` / `account_done` / `account_failed`；批量结束走 `batch_done`，整轮失败 `batch_done(False, [(显示名, 消息), …])` 决定弹不弹错误框，后台隐藏只弹气泡。可见账户实时刷新，其余账户只累积状态，切过去再重画。邮件列表按选中账户过滤（数据本就按账户分槽存于 `state["messages"]`），切账户即换视图。
- **备份 = 预检 + 并发同步**（ADR 0011）：`_on_start` / `_on_start_selected` 取勾选账户后交 `_begin_preflight`，弹预检弹窗按账户顺序串行检测（登录 + LIST + 登出）；密码错误（认证失败）弹密码补录窗（`_ask_password`：重试 / 跳过，带「保存密码」复选框，勾选才写 `password_dpapi` 落盘，不勾只进 `_run_passwords` 本次运行快照）；关闭预检弹窗 = 终止本次备份。全通过后弹窗自动关闭、`_begin_run` 并发同步：`_batch_worker` 为每个账户起一条下载线程（`_backup_account_thread`），各持独立 IMAP 连接，单账户失败不影响其余，`stop_flag` 全局生效。运行日志经 `_account_logger`（每账户真实 Logger + 前缀 Filter）加 `[账户名] ` 前缀，混排但每行可辨。
- **每个账户自己 LIST 文件夹**：`_backup_account` 连接后调 `core.get_names` 并按账户记录的文件夹名过滤，从没连接过的账户在批量里也能直接跑。
- **设置页账户表单**：行序照 foxmail（邮箱类型置顶 + 名称 / 邮箱地址 / 收件服务器 + SSL + 端口同排 / 密码）；右详情三块 body 四边内边距 8px（与面板间距同一基线），分类标题用 `_heading_font`（加粗、正文 +`HEADING_SIZE_DELTA` 磅）。`_account_form_entries` 是表单里的输入框（顺序仍是名称 / 邮箱地址 / 收件服务器 / 端口 / 密码，Enter 提交保存与右键菜单按它索引）；`_acct_ssl_var` 只联动仍是 993/143 的默认端口。密码框存的是掩码哨兵：`_on_acct_password_key` / `_on_acct_password_clip` 保证任何会改文本的动作（打字 / BackSpace / `<<Paste>>` / `<<Cut>>`）先把掩码整框清掉，免得在掩码中间拼出假密码；导航键与控制组合放行。删除账户要确认，且不碰磁盘上已备份的邮件。

相关回归：

- `test_accounts_v1_settings_migrate_to_accounts` / `test_accounts_restore_tolerates_bad_records` / `test_accounts_save_and_restore_round_trip`
- `test_backup_detail_has_no_entry_widgets` / `test_format_checkboxes_are_per_account`
- `test_account_rows_are_gray_until_selected` / `test_account_checkboxes_are_independent_of_selection` / `test_backup_selected_runs_only_checked_accounts`
- `test_account_row_checkbox_sits_next_to_the_name` / `test_account_list_scrolls_only_when_accounts_do_not_fit`
- `test_account_form_password_mask_reflects_the_saved_password` / `test_password_box_saves_keeps_and_clears` / `test_password_mask_guards_typing_and_paste`
- `test_account_form_follows_the_mail_client_layout` / `test_settings_detail_padding_matches_the_8px_gap` / `test_settings_section_titles_use_a_bigger_bold_font`
- `test_batch_worker_runs_accounts_concurrently_and_isolates_failures` / `test_backup_account_uses_email_for_the_account_directory`
- `test_folder_load_fills_the_tree` / `test_editing_credentials_triggers_a_silent_reload`
- `test_refresh_button_reloads_folders` / `test_folder_load_failure_shows_hint_and_prompts_password`

## 按钮与加载状态

- 开始与停止图标由 `PhotoImage` 在代码中生成，不增加图片资产；Python 侧必须持有引用，避免图标被垃圾回收。
- 加载态用 `SPINNER_FRAMES`、`SPINNER_INTERVAL_MS` 和 `_set_loading_frame()`（只有 `_run_spinner_tick` 一条调度链；连接 spinner 已随连接按钮移除）。
- 开始和停止按钮不设置固定 `width`。加载态使用 `_blank_icon()` 占住「有 image」这个高度前提，不把 `image` 设为 `""`，避免按钮高度变化；占位是 1px 宽 × 图标高——只借高度不占宽，转圈符贴近左边框、左右留白与静止态对称（回归自：整图标宽的占位让左边一大块空白、左松右紧）。宽度在加载/静止之间会随文案与图标槽变化，不再是「只随文案走」。
- 点击停止后，转圈移到停止按钮；开始按钮恢复静止外观但保持禁用；状态行只清除文案而不收回布局。
- 完成、停止、失败以及帧链自愈路径都必须恢复两颗按钮。
- 邮箱文件夹工具栏右上角的 **⟳ 刷新按钮**是 icon-only 字形按钮（`RELOAD_GLYPH`；PhotoImage 画不了圆弧，不自绘图标），**正方形**（`_make_square_button`：Button 的 width 是字符单位、height 是行高，单位不同，按实测请求高迭代反推 width，误差 ≤2px），悬停有气泡（`_bind_widget_tip`，无截断语义、悬停即弹），运行中随 `_register` 名单一起禁用。

相关回归：

- `test_start_button_borrows_the_connecting_spinner_while_backing_up`
- `test_loading_buttons_keep_their_height`
- `test_stopping_feedback_covers_buttons_status_line_and_hint`
- `test_stopping_then_failing_restores_the_controls`

## 布局与滚轮

- 主内容左右边距保持同一基线；单个面板不额外增加右边距。邮箱文件夹面板的滚条贴住面板右边框。
- **版式封顶**（图1/2/3 的「分隔太远 / 截断遮挡 / 输入框过长」同根因：内容列无上限拉伸）：「账户信息」「输出」两卡的值列 `weight=0` 不拉伸（窗口再大右侧留白），内容块封顶 `CARD_CONTENT_MAX_WIDTH`（620px），超长值（邮箱地址、收件服务器、备份保存位置）截断显示、悬停 tooltip 显示全文（`_fit_card_values` + `_bind_tip`）；设置页账户表单块封顶 `ACCOUNT_FORM_MAX_WIDTH`（640px，`_fit_account_form_block` 用 `place` 定宽、块内输入框照常拉伸），「备份」分类的备份保存位置行同封顶（`_fit_backup_root_row`，同 place 口径）。**收件服务器行放不下就换行**（`_layout_conn_row`，用户选定「实测宽度 + 迟滞带」）：判定是实时的（每次卡片 `<Configure>` 重算），各段取 `winfo_reqwidth` 实测宽 + 同排 `grid padx` 实际间隙（`CHIP_PADX_SAME_ROW` 等三个常量布局与量宽共用，不是估值），拼上服务器值像素地板 `SERVER_MIN_BUDGET` 与可用宽比；恢复同行比换行多要 `WRAP_HYSTERESIS`（12px）的富余，临界宽度来回拖不闪跳。换行后 SSL chip 与端口进**独立第二行容器** `_conn_wrap_row`（跨列 `columnspan` 不参与列宽计算、左缘贴值列）紧凑排一排——不能塞回第一行的网格列：列宽按第一行定，端口会被推到最右还截断（回归自截图）。换行与否决定截断预算里扣不扣 tails。
- **两卡内容贴左、值列跨卡同线**（用户选定「左对齐 + 统一到同一条线」）：内容块 `inner.pack(..., anchor="w")` 按内容定宽贴左，窗口再大右侧整片留白——**不能** `fill="x"`：grid 会把自己居中在更宽的 master 里，看着又像居中（「内容居中」截图的根因是 pack 默认 anchor=center）。值 Label 统一 `padx=4`（首行曾漏它，「IMAP（仅接收，不发送）」比下一行值左移 4px）；两卡标签列取全部标签最大请求宽做 `columnconfigure(0, minsize=…)`（`_align_card_caption_columns`，标签右对齐、值列贴它起排），「账户信息」与「输出」的值列起点因此落在同一条竖线。标签文案静态，建完两卡量一次即可。
- **SSL 是色块 chip 不是文字值**：启用绿、未启用灰（`_style_ssl_chip`，备份卡只读展示与设置表单同一视觉）；设置表单里 chip 可点击切换（`_on_acct_ssl_chip_click`，command 语义与原 Checkbutton 完全一致，默认端口联动照旧）。
- **上部两栏固定 3:2**（账户信息 : 邮箱文件夹，用户更正后的比例）：两栏**不走 grid 权重**——列宽 = 请求宽 + 按权重分余量，左列内容请求宽几百像素，小窗下余量再怎么分右列都只剩零头（「邮箱文件夹被挤扁」的根因）。改 `place` 定宽（`_fit_top_columns`，同 `_fit_account_form_block` 口径）：净宽扣掉 `TOP_COLUMN_GAP`（8px 栏间隙）后按 `TOP_LEFT_SHARE`（3/5）分，所有窗口尺寸固定比例。
- **底边对齐**：左列（账户信息 + 输出）叠出的自然高度是基准线，「邮箱文件夹」卡的高度、左列自身的高度与上半区容器的高度都去贴它（`_sync_top_region_height`），两卡下边框落在同一条线上，下方运行区排在上半区之下。**高度必须走 `place_configure(height=…)`，不能 `configure(height=…)`**：folder_group 的子控件在 grid 里，grid 传播会在子控件重新请求尺寸时把 height 选项覆盖回内容高（实测 119 掉回 52），卡片矮一截、底边就对不上（回归自「邮箱文件夹底部对不上输出」）；place 的高度选项不受请求变化影响。左列定高后的富余由「输出」卡 `pack(fill="both", expand=True)` 吸收（内容顶部对齐、下方留白），两卡底边因此永远同线。**卡高不得低于内容的最小排布**：兜底取两列请求高的较大者（`winfo_reqheight` 仍反映内容最小高），树画布只给 1px 的最小请求高（`height=1`，实际高由 `row=1` 的 `weight=1` 吃满）——grid 行不会缩到请求高以下，请求大了会把布局顶出卡片、画布盖掉 LabelFrame 底边框（回归自「下边线没了」）。
- **卡片内容内边距统一 `CARD_PAD`（8px）**：「账户信息」「输出」两卡的内容行装进 `inner` 框（`padx=8, pady=5`，行自带 `pady=3` 合成四边 8px），「邮箱文件夹」toolbar 与树区同取 8px（滚条仍贴面板右内沿）。
- **按钮排的按钮间隔统一 `BTN_GAP`（4px，用户选定，基准 = 备份页账户列表 全选/反选/清空）**：文件夹工具栏、设置页「账户」新增/删除、底栏 开始备份/停止、密码补录与关闭询问的按钮对，相邻按钮之间一律 4px（pack 用 `padx=(BTN_GAP, 0)` / `padx=(0, BTN_GAP)`，grid 用两侧各 `BTN_GAP // 2`）。**⟳ 例外**：右对齐在工具栏内沿（右上角，用户选定），与左排按钮之间是弹性余量、只保 `BTN_GAP` 下限——窗口窄到整排放不下时钳制会把右栏放大到装下整排（`_backup_detail_min_width`）。复选框对（mbox/eml 输出）不是按钮排，保留各自语义间隔。
- **文件夹树的层级语言**：**一级行不画层级符号**（用户选定）——没有前缀槽，复选框（名称 Label 里的勾选符）直接贴树区左缘的 `CARD_PAD` 标准内边距（与 toolbar 同一条竖线），有子文件夹时展开箭头（`▾`/`▸`）**跟在名称后**（行内顺序 [名称, 尾随箭头]，测试别按下标取）。二级及以下维持前缀槽（固定 `ARROW_SLOT_W` 宽，名称左缘各级对齐）= 层级连接符（同级末个 `└`、其余 `├`，不画祖先竖线）+ 父行展开箭头（行内顺序 [前缀槽, 名称]）；子层缩进 `TREE_INDENT_W`（12px/级）。点箭头 = 展开/折叠、点名称 = 勾选，命中区不变。**前缀槽定宽但必须 `fill="y"`**：`pack_propagate(False)` 连高度传播也关掉，不给高度槽会塌成 1px，Windows 把子标签裁成一条横切片，`├`/`└`/`▾` 全都显示成「-」（回归自：符号看着像横杠；`node["prefix_label"]` 留了测试口，一级行为 `None`）。
- **长名称截断 + 悬停看全文**：文件夹名与账户名与卡片值同一口径（`_fit_name_label` + `_bind_name_tip`，只在截断时弹气泡）——文件夹名按树区宽扣掉该行实占的缩进与前缀槽（一级行没有槽，改扣名称后的尾随箭头），账户名按列表宽；画布/侧边栏变宽自动恢复全文（双向）。
- **侧边栏拖拽调宽**（三处各自独立持久化）：备份页账户列表、设置页左栏、设置页「账户」列表各有一个 overlay 拖拽手柄（`_make_sash`），**手柄压在「侧栏右边框与右侧内容之间」那条空隙带上**（用户选定）：带就是右侧内容的内边距/格距（备份页 8px = 右栏 `padx=8`、设置左栏 8px = `nav` 的 grid `padx=(0,8)`、账户列表 10px = `form_host` 的 `padx=(10,0)`），手柄挂宿主上、宽 = 带宽、`place(x=偏移+侧栏宽, width=带宽, relheight=1)`，侧栏改宽后 `_place_sash` 重摆。**手柄隐形**：背景与宿主同色（`str(host.cget("background"))`），可拖提示只靠 `sb_h_double_arrow` 光标——曾贴在侧栏内侧压住内容，出一条灰条（回归自截图）。宽度钳在 `[SIDEBAR_MIN_WIDTH, 上限]`（默认 160），上限取硬上限 `SIDEBAR_MAX_WIDTH`（240）与**动态钳制**的较小者（`_sash_max_width`，每次拖动 motion 重算 = 实时判断右侧内容还能不能显示完整）：窗口宽 - 同页其它栏位宽 - 右侧最小完整宽（设置页取账户表单块实测请求宽钳 320~640，备份页取 `_backup_detail_min_width` = 整体最小宽与**「邮箱文件夹」工具栏实测请求宽**反推的宽取大——先前只按 `BACKUP_DETAIL_MIN_WIDTH` 反推，拖宽后右上 ⟳ 被 pack 挤成一条竖缝，回归自截图；文件夹列 = (右栏宽-内边距-栏间隙)×(1-TOP_LEFT_SHARE)，卡内再扣 2×CARD_PAD 与卡边框）。**窗口收窄同口径兜底**：`_clamp_backup_sidebar`（挂在 `_fit_top_columns`）把实际宽度收成 `min(用户选定宽, 拖拽上限)`，窄窗下 ⟳ 不被挤没、拉宽后自动回到用户选定的宽（不动 `_backup_sidebar_width`）。三处宽度互不影响，各自写入界面设置的 `backup_sidebar_width` / `settings_sidebar_width` / `settings_account_list_width`（`parse_sidebar_width` 校验，越界回落默认）。**手柄必须晚于占住这条带的兄弟内容创建**：带常落在右侧内容的内边距上，Tk 同级后建者在上层，早建会被兄弟节点整个盖住点不到（回归自：设置页左栏拖不动）。
- **自绘弹窗一律居中到主窗口**（用户选定）：预检加载窗、密码补录窗、关闭询问窗都走 `_center_on_root`（内容建完后、`grab`/`wait_window` 前调用）。注意 `wm geometry` 的 `+x+y` 落在**外框**、`winfo_rootx` 量的是**客户区**，中间差着一圈装饰（实测 8px 边框 + 31px 标题栏），直接套「root 中心 - 弹窗一半」会整体偏右下一截——所以先 `withdraw` 着摆一次 `+0+0` 量出装饰偏移，再按客户区中心反推外框坐标（实测落点误差 0px，弹窗全程未显示过，不闪）。系统 messagebox/filedialog 是原生对话框、随 owner（`parent=self.root`）居中，不用这里管；悬停气泡跟随鼠标，不居中。
- **智能滚条**：备份页账户列表与设置页「账户」列表的滚条都按内容显隐（`_sync_account_scroll` / `_sync_account_edit_scroll`，行数 × 行高超出可视高才 `grid`，装下 `grid_remove`），触发点是列表 `<Configure>` 与增删/重画；滚条挂出再收窄的二次 `<Configure>` 状态已稳定，不来回抖。设置页两个 Listbox 的滚轮沿用原生节奏（`_on_wheel` 只接管「邮箱文件夹」与备份页账户列表两块画布）。
- 面板与面板之间的间隔横竖都是 **8px**：左栏账户列表 ↔ 右栏、「账户信息」↔「邮箱文件夹」、「账户信息」↔「输出」（`pady=(0, 8)`），以及上半区 ↔ 运行区（配置区下边距 4 + 运行区上边距 4）。
- 两个页签（邮件列表 / 运行日志）各有一层与「邮箱文件夹」面板同款的外包边框（`_panel_style_frame()`：groove + 面板同值 `borderwidth`）：滚条包在框内、贴着边框内侧，日志页签的工具栏也在框内，空态提示挂框内。
- 邮件列表：外框与面板同款（groove）；白色表格**自己带凹陷边**（`tree_wrap`：`tk.Frame(relief="sunken", borderwidth=1, background="#ffffff")`，Tk 经典 3D 绘制——左/上 153 阴影、右/下 230 高光），整块贴着外框内沿、不留灰边距。**只保留右侧那条凹陷线**：树用 `place(x=-1, y=-1, relwidth=1.0, relheight=1.0, width=1, height=2)` 往左上各溢出 1px、右下各多铺 1px（宽那 1px 用来吃掉顶边最后一个像素，否则白板右上角会留下一个深色点），把白板的左/上/下三条线盖住（与外框合成一条线），右边留出的 230 高光正对滚条左缘的 1px 白色反光条（没有对比时白对白会糊成一片，用户口径，探针见 issues/15）。白底是必需的：3D 色阶由控件背景推出，白底才与日志板同色。树自己的 vista field 边框走派生样式 `Mail.Treeview`（layout 只留 `treearea`，全局 `Treeview` 布局不动）压平。
- 运行日志：板面与「打开日志文件夹」按钮同外框之间处处留 **4px** 灰边距（以最短的下边距统一，按钮与板面左缘同一竖线），板面保留 Tk 自带下沉边框、右侧贴住滚条；滚条也带 4px 上下边距（与文件夹面板的滚条同款）。
- 回归：`test_right_edges_share_one_baseline` 断言这些边框 / 留白 / 凹陷边 / 滚条对齐。
- notebook 客户区在 vista 主题里相对自身左缩 1px、右缩 3px、下缩 3px，且会在客户区四周自绘 1px 淡边线（原生绘制，颜色改不了：`TNotebook` 只有 `-tabmargins` 可配，换 background/bordercolor/lightcolor/darkcolor 像素纹丝不动，见 issues/15）。因此容器 `run` 的左右内边距用 7/5 把它补掉（按钮行与状态行用 `padx=(1, 3)` 保留 8px 边距），页签包裹边框的左/右线才与配置区左边线、上方面板右线**精确重合**；`tab_edge_left`/`tab_edge_right`/`tab_edge_bottom` 三条背景色窄条（1px/3px/3px，`place` 在 notebook 上、随其几何走）盖掉客户区内缩带，页签里就只剩包裹边框这一条线。**底边那条必须盖**：它的淡线落在包裹边框外侧，底边看起来会多出一道「突出」的线（用户复核）；顶边那条是页签行底界线，保留。
- `test_right_edges_share_one_baseline` 断言上述三处均为**精确相等**，并断言窄条尺寸（左 1 / 右 3 / 下 3）== 实测内缩：换主题或 DPI 让内缩变了，会在这里炸出来，提醒同步改窄条尺寸与容器内边距。
- 邮箱文件夹与账户列表的滚轮共用窗口级入口 `root.bind("<MouseWheel>", _on_wheel)`；`_wheel_canvas` 沿事件控件的 `.master` 向上认面板（`_widget_in_panel`），落在「邮箱文件夹」或左栏账户列表里才接管，其他区域返回 `None` 放行（设置页那个 Listbox 因此照旧走原生滚动）。
- 面板内滚条也调用 `_on_wheel` 并返回 `"break"`，由该入口完成滚动，避免 `TScrollbar` 默认步长覆盖统一节奏。
- 每格滚动 `FOLDER_WHEEL_ROWS` 行；`yscrollincrement` 使用实测行距。不足一格的 delta 按 `WHEEL_DELTA` 累积，以支持高分辨率滚轮和触摸板。

相关回归：

- `test_right_edges_share_one_baseline`
- `test_folder_panel_wheel_covers_the_whole_panel`
- `test_account_list_scrolls_only_when_accounts_do_not_fit`
- `test_top_columns_split_three_to_two` / `test_conn_row_wraps_when_tight`
- `test_card_content_is_left_aligned_and_values_line_up` / `test_card_value_columns_align_across_cards`
- `test_folder_card_bottom_matches_output_card`
- `test_ssl_chip_renders_and_toggles` / `test_backup_root_row_is_capped`
- `test_settings_sash_is_on_top_and_draggable` / `test_account_edit_list_has_sash_and_smart_scroll`
- `test_sash_sits_in_the_gap_band` / `test_dialogs_are_centered_on_root`
- `test_backup_sash_keeps_the_folder_toolbar_visible`
- `test_button_gaps_are_uniform`
- `test_folder_prefix_labels_are_not_clipped` / `test_folder_card_bottom_border_not_overflow`
- `test_folder_top_rows_have_no_tree_symbol`
- `test_reload_button_is_square` / `test_long_names_are_ellipsized_with_tip`

## 邮件列表选中

- 选中高亮没有动作消费者：基于选中的操作一个也没有（右键菜单复制的是落点单元格，不是选中行），它只是「刚点过哪一行」的视觉痕迹。
- 邮件列表标题分隔线由 GUI 接管：拖动只在相邻两列之间重新分配宽度，并保留各列最小宽度；窗口尺寸变化时五列整体按比例缩放；最右侧「大小」列的右缘不得越过表格与滚条的交界。
- 四条列间分隔线（`from|subject`、`subject|when`、`when|folder`、`folder|size`）可拖动；`size` 右缘不可拖动。拖动期间冻结 `subject` 的 `stretch`，释放后恢复原状态；最小宽度使用 Treeview 原生 `minwidth`，不由拖拽逻辑另行维护。
- 左键入口是窗口级：`root.bind("<Button-1>", _on_click_clear_selection_and_focus)`；用 `identify_row(event.y)` 区分行与空白——空白、列标题、分隔区都返回空串，一次调用覆盖全部非行区域。
- 例外两个：邮件行本身（换选与多选归 Treeview 类绑定，处理器不插手）与邮件列表自己的滚条（拖它多半是在查看选中行）。Alt-Tab 切窗口不清——用 `<FocusOut>` 会破坏这一点。
- 处理器只清理、不拦截：返回 `None`，不返回 `"break"`；控件匹配用路径比较而非对象同一性，理由同滚轮入口。
- 右键走另一条链路（Button-3）：点行时右键菜单先选中该行（Windows 惯例），点空白既不弹菜单也不动选中。

相关回归：

- `test_row_selection_clears_on_click_outside`

## 输入框焦点

- 措辞先立稳：**选中**（selection）是邮件行的高亮，**聚焦**（focus）是输入框的光标归属——两件事共用同一个窗口级左键入口，别混着叫。
- 焦点只归属设置页账户表单的输入框与设置页「备份」里的地址栏（`_account_form_entries` + `backup_root_entry`；备份页没有输入框）。点击后焦点仍在某个输入框、而落点不是这个框时，焦点收归主窗口 `root.focus_set()`——光标消失，Tab 从第一个控件重新开始。
- 收归与否看落点控件是否原生接焦：Tk 只让输入框、列表、日志文本在点击时拿焦点（禁用中的输入框不拿），标签、空白、按钮、勾选框点完焦点原地不动，正需要入口收走；点另一个输入框/列表/日志文本时类绑定先完成转移，处理器不插手也不抢占。
- 例外与上节同口径：点邮件列表自己的滚条时选中与焦点都不动。Alt-Tab 与滚轮不产生 Button-1，天然不清；不用 `<FocusOut>`。
- 测试不赌前台归属：`focus_set` 是「非强制」的，窗口被外部抢走前台时请求会被推迟（`focus_get()` 停在原处）。所以断言分两路——用替身数 `root.focus_set` 的调用（确定性契约：处理器必须发出收归请求），焦点去向只做宽松校验；合成鼠标事件偶发被丢弃时重发一次再判。

相关回归：

- `test_entry_focus_clears_on_click_outside`

## 右键菜单

- 三类区域（输入框 / 邮件列表 / 运行日志）各一张 `tk.Menu`，在 `_build_context_menus` 里启动时各建一次复用；菜单不进 `_register`——那是配置区控件的启停跟踪名单。
- 全部菜单都不含「全选」（用户 2026-09-22 拍板）：输入框三项、日志两项，没有第四项。输入框与日志上的 Ctrl+A 整篇选中是 Tk 平台默认（`<<SelectAll>>` 映射到 `<Control-a>`，Text/Entry 都有类绑定，探针见 issues/13），不是本程序添加的菜单项，也不拦截。
- 输入框（含密码框，一视同仁）：三项 `剪切/复制/粘贴`，动作直通 `<<Cut>>/<<Copy>>/<<Paste>>` 虚拟事件——与 Ctrl+X/C/V 走同一条类绑定路径。弹窗前 `_refresh_entry_menu_state` 按「选区 / 剪贴板可读 / 控件 state」刷新灰化：无选区灰切复，剪贴板空或控件被 `_set_config_enabled` 关掉时灰粘贴。密码框 `show="*"` 下复制出的是显示字符（星号）而非明文——Tk 原生按显示形态导出选区（探针实测，见 issues/13）。
- 邮件列表：`_on_tree_context` 只在行上弹「复制」，复制右键落点的那一格（`identify_row` + `identify_column`，列号 1 起），不依赖选中；空白与列标题不弹、也不动选中；落点行不在选中集里时先选中它。定位、右键选中与记下目标全在 `_prepare_tree_popup` 里，与 `tk_popup` 分离。
- 运行日志：两项——「复制」取划选段（`sel` 标签，只读态照样能鼠标划选），「复制全部」取 `get("1.0", "end-1c")`（以换行结尾）；无选区灰「复制」，日志为空灰「复制全部」。
- 三类菜单的定位/灰化/动作 handler 都与 `tk_popup` 分离；测试里 `tk_popup` 必须换成替身——真菜单会 grab，无前台窗口的 runner 会悬死。

相关回归：

- `test_entry_context_menu_has_three_actions`
- `test_entry_context_menu_items_grey_out`
- `test_tree_context_menu_copies_the_clicked_cell`
- `test_log_context_menu_copies_all`

## 后台驻留、托盘与页面导航

改关闭按钮行为、托盘、菜单栏或设置页前先读这一节；完整决策见 ADR 0009。

- **关闭三态**：`close_action` = `ask`（默认）/ `tray` / `exit`，读写都在既有设置文件里，**自身不升版本号**（新键按字段独立校验，不合法即回退 `ask` + warning；`UI_SETTINGS_VERSION` 只在账户模型这类结构性变化时升，且必须配套迁移）。关闭动作由纯函数 `decide_close(preference, hidden=)` 决定（`force_exit` / `hide` / `exit` / `ask`）；备份是否运行中只影响对话框文案与托盘「退出」的确认，不参与判定。
- **「已隐藏」只认 `root.state() == "withdrawn"`**，不设影子标志；据此定死一条规则：**隐藏状态下收到的关闭请求一律真退出**（那只可能是安装器/卸载器/任务管理器发来的），否则升级安装关不掉旧实例。
- **托盘线程边界与工作线程同款**：`imapbackup_tray.TrayHost` 的回调只往 `self.events` 投 `tray_show` / `tray_stop` / `tray_quit`，由 `_pump` 在 Tk 线程消费；界面侧所有托盘调用都必须走 `TrayHost` 的公开方法（内部转给托盘线程），绝不在托盘线程里碰 widget。托盘为 `None`（非 Windows / 建不起来 / `show()` 返回 False）时「隐藏到后台」退化为 `iconify()`——出口路径必须先成立。
- **图标常驻**：`TrayHost.show()` 是幂等的「确保挂出并刷新 tooltip」，启动时调一次（失败只记 warning），隐藏窗口前再调一次并以它的返回值决定是否隐藏；`hide()` 不存在——唯一移除点是真宿主的 `close()`（`_quit()` 调用）。tooltip 与菜单「停止备份」的可用性由 `_sync_tray()` 在启动、开始备份、运行结束、隐藏、恢复、后台通知处追平。
- **后台结束只发气泡**：`account_done` / `batch_done`（带失败清单）在 `_background_hidden()` 为真时不恢复窗口、不弹错误框，改为 `tray.notify(...)` + `_sync_tray()`；点气泡或左键图标走 `tray_show` 恢复窗口。
- **设置落盘仍是「一次写」**：设置页单选钮与关闭对话框只改内存 `_ui_settings`，隐藏/退出/外部关闭三条路径各调一次 `_save_column_config()`；隐藏时先落盘再 `withdraw()`（此刻几何还读得对）。
- **窗口状态记忆**：`_window_maximized` 由 `_restore_window_settings()` 初始化、`<Configure>` 在 state 为 `zoomed`/`normal` 时维护；`_window_settings()` 与 `_show_window()` 都读它，**不现场读 `wm state`**——最小化（iconic）或已隐藏（withdrawn）时现读会把最大化记丢（托盘菜单退出、外部关闭正是后一种）。`withdraw()`→`deiconify()` **不带回最大化**（真机实测：回来是 normal + 最大化前几何），隐藏恢复必须重放 `state("zoomed")`；iconic 分支保持 `state("normal")`——Tk 自己记得最小化前的状态（含最大化）。
- **页面外壳**：`page_host` 下 `backup_page` / `settings_page` 同格叠放，`_show_page()` 是唯一切页入口（`tkraise` + 菜单栏选中态 + 设置页回填）；菜单栏两项是顶层 radiobutton（不开下拉）。设置页两栏＝左 `Listbox` 分类栏（`SETTINGS_SECTIONS` 表驱动：账户 / 备份 / 窗口与后台，固定 160px，`pack_propagate(False)`）+ 右详情（`tkraise` 切分类，进页默认落在第一个分类），**四边留白等宽**（`outer` 用 `padx=8, pady=8`：备份页的底边距由运行区自带，设置页下方没有内容，漏掉就会「三边等宽、底边贴边」）；设置页控件**不进 `_register`**。备份页则是「左账户列表栏（固定 160px，`pack(before=…)` 插到右栏左侧）+ 右详情」，右栏内部的配置区与运行区沿用同一条 8px 基线。
- **菜单栏的客户区代价**：Windows 原生菜单栏在窗口落地前会吃掉几何高度（实测 736 → 716），`__init__` 末尾 `update()` 落地后再重设一次 `_target_geometry`（由 `_restore_window_settings()` 记录，含位置）；最大化时不再重设，避免碰坏 ADR 0008 的正常几何。

相关回归：

- `test_close_policy.py`（偏好解析与关闭决策矩阵，非 gui）
- `test_close_with_tray_preference_hides_and_keeps_saving` / `test_close_with_exit_preference_closes_tray_and_window` / `test_close_while_hidden_always_exits_without_asking` / `test_ask_preference_dialog_result_drives_the_action` / `test_cancelled_dialog_changes_nothing`
- `test_hide_degrades_to_iconify_without_a_tray` / `test_hide_degrades_when_the_icon_cannot_be_added`
- `test_hide_and_show_keeps_the_maximized_window` / `test_maximized_is_recorded_when_iconic_at_save` / `test_tray_quit_while_hidden_keeps_the_maximized_choice`（窗口状态记忆与三种落盘时刻）
- `test_run_finished_in_background_only_sends_a_balloon` / `test_stopped_run_in_background_sends_the_stopped_balloon` / `test_failed_run_in_background_does_not_pop_a_dialog`
- `test_tray_icon_is_up_from_startup` / `test_tray_state_follows_the_run` / `test_tray_show_event_restores_the_window` / `test_tray_stop_event_only_stops_a_running_backup` / `test_tray_quit_confirms_only_while_running` / `test_tray_host_wiring_is_available_on_windows`
- `test_menubar_navigation_switches_pages` / `test_settings_page_lists_the_sections_and_selects_the_first` / `test_settings_page_offers_the_three_close_options` / `test_changing_the_option_lands_in_memory_then_on_disk_with_the_close`

## 单实例与验证

`APP_MUTEX_NAME` 必须与 `packaging/imapbackup-gui.iss` 的 `AppMutex` 完全一致。二次启动不再只是「打印一行再退出」，而是经 `imapbackup_tray.request_restore()`（`FindWindowW(WINDOW_CLASS_NAME)` + `PostMessageW`）唤起已有实例的窗口；改互斥量名或托盘窗口类名会同时影响安装器的运行实例探测与这条唤起通道。修改 GUI 后至少运行：

```bash
.venv/Scripts/python.exe -m py_compile imapbackup_gui.py
.venv/Scripts/python.exe -m pytest -m gui -q
```

涉及互斥量、图标路径或打包入口时，另运行 `packaging\build.bat` 并按 ADR 0007 人工验收安装与卸载行为。
