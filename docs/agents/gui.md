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
- 配置区字段（账号、收件服务器、SSL、端口、备份保存位置）随同一原子写落盘在 `config` 节（端口存字符串，校验在连接时做）。恢复顺序 ssl→端口；恢复出的端口视为用户确认过的值（置 `_port_touched`），挡住 SSL 联动改端口。字段逐个校验类型，坏字段跳过、坏节整节丢弃。
- 邮箱密码以系统 DPAPI 加密形式保存（base64 密文进同一文件，仅当前用户、当前机器可解；防配置文件泄露，不防以本用户身份运行的进程）。空密码不落盘；非 Windows 平台密码不持久化、其余字段照常；解密失败（换机器/换账户/文件损坏）视为未保存，记一条 warning 后正常启动，不弹窗。会话令牌和邮件内容不属于界面设置，任何路径都不得写入。
- 设置文件不存在、无法解析、字段类型不符或版本不兼容时，整份设置视为不可恢复：使用内置默认值启动并继续提供 GUI；不得因为设置损坏阻止连接或备份。应把可诊断信息写入运行日志，日志不可用时只向现有 console/状态提示降级，不弹出阻断式错误框。
- 写入失败时保留当前进程中的设置，界面操作和备份继续；下一次启动重新使用上一次成功保存的设置或默认值。写入失败只产生可诊断警告，不覆盖一份仍可读取的旧设置文件，也不触碰备份输出。
- 卸载向导分别提供“删除运行日志”和“删除界面设置”选项，默认均勾选；取消向导或中途取消进度时两者都保留，真静默卸载按默认值删除两者。用户备份永远不在范围内（ADR 0007、ADR 0008）。

## 按钮与加载状态

- 开始与停止图标由 `PhotoImage` 在代码中生成，不增加图片资产；Python 侧必须持有引用，避免图标被垃圾回收。
- 连接与备份共用 `SPINNER_FRAMES`、`SPINNER_INTERVAL_MS` 和 `_set_loading_frame()`。修改帧或节奏时同时覆盖 `_connect_spinner_tick` 与 `_run_spinner_tick` 两条调度链。
- 开始和停止按钮不设置固定 `width`。加载态使用 `_blank_icon()` 占住「有 image」这个高度前提，不把 `image` 设为 `""`，避免按钮高度变化；占位是 1px 宽 × 图标高——只借高度不占宽，转圈符贴近左边框、左右留白与静止态对称（回归自：整图标宽的占位让左边一大块空白、左松右紧）。宽度在加载/静止之间会随文案与图标槽变化，不再是「只随文案走」。
- 点击停止后，转圈移到停止按钮；开始按钮恢复静止外观但保持禁用；状态行只清除文案而不收回布局；配置区显示停止提示。
- 完成、停止、失败以及帧链自愈路径都必须恢复两颗按钮和配置区提示。

相关回归：

- `test_start_button_borrows_the_connecting_spinner_while_backing_up`
- `test_loading_buttons_keep_their_height`
- `test_stopping_feedback_covers_buttons_status_line_and_hint`
- `test_stopping_then_failing_restores_the_controls`

## 布局与滚轮

- 主内容左右边距保持同一基线；单个面板不额外增加右边距。邮箱文件夹面板的滚条贴住面板右边框。
- 两个页签（邮件列表 / 运行日志）各有一层与「邮箱文件夹」面板同款的外包边框（`_panel_style_frame()`：groove + 面板同值 `borderwidth`）：滚条包在框内、贴着边框内侧，日志页签的工具栏也在框内，空态提示挂框内。
- 邮件列表：外框与面板同款（groove）；白色表格**自己带凹陷边**（`tree_wrap`：`tk.Frame(relief="sunken", borderwidth=1, background="#ffffff")`，Tk 经典 3D 绘制——左/上 153 阴影、右/下 230 高光），整块贴着外框内沿、不留灰边距。**只保留右侧那条凹陷线**：树用 `place(x=-1, y=-1, relwidth=1.0, relheight=1.0, width=1, height=2)` 往左上各溢出 1px、右下各多铺 1px（宽那 1px 用来吃掉顶边最后一个像素，否则白板右上角会留下一个深色点），把白板的左/上/下三条线盖住（与外框合成一条线），右边留出的 230 高光正对滚条左缘的 1px 白色反光条（没有对比时白对白会糊成一片，用户口径，探针见 issues/15）。白底是必需的：3D 色阶由控件背景推出，白底才与日志板同色。树自己的 vista field 边框走派生样式 `Mail.Treeview`（layout 只留 `treearea`，全局 `Treeview` 布局不动）压平。
- 运行日志：板面与「打开日志文件夹」按钮同外框之间处处留 **4px** 灰边距（以最短的下边距统一，按钮与板面左缘同一竖线），板面保留 Tk 自带下沉边框、右侧贴住滚条；滚条也带 4px 上下边距（与文件夹面板的滚条同款）。
- 回归：`test_right_edges_share_one_baseline` 断言这些边框 / 留白 / 凹陷边 / 滚条对齐。
- notebook 客户区在 vista 主题里相对自身左缩 1px、右缩 3px、下缩 3px，且会在客户区四周自绘 1px 淡边线（原生绘制，颜色改不了：`TNotebook` 只有 `-tabmargins` 可配，换 background/bordercolor/lightcolor/darkcolor 像素纹丝不动，见 issues/15）。因此容器 `run` 的左右内边距用 7/5 把它补掉（按钮行与状态行用 `padx=(1, 3)` 保留 8px 边距），页签包裹边框的左/右线才与配置区左边线、上方面板右线**精确重合**；`tab_edge_left`/`tab_edge_right`/`tab_edge_bottom` 三条背景色窄条（1px/3px/3px，`place` 在 notebook 上、随其几何走）盖掉客户区内缩带，页签里就只剩包裹边框这一条线。**底边那条必须盖**：它的淡线落在包裹边框外侧，底边看起来会多出一道「突出」的线（用户复核）；顶边那条是页签行底界线，保留。
- `test_right_edges_share_one_baseline` 断言上述三处均为**精确相等**，并断言窄条尺寸（左 1 / 右 3 / 下 3）== 实测内缩：换主题或 DPI 让内缩变了，会在这里炸出来，提醒同步改窄条尺寸与容器内边距。
- 邮箱文件夹滚轮使用窗口级入口 `root.bind("<MouseWheel>", _on_wheel)`；沿事件控件的 `.master` 向上判断是否位于 `folder_group`，其他区域返回 `None` 放行。
- 面板内滚条也调用 `_on_wheel` 并返回 `"break"`，由该入口完成滚动，避免 `TScrollbar` 默认步长覆盖统一节奏。
- 每格滚动 `FOLDER_WHEEL_ROWS` 行；`yscrollincrement` 使用实测行距。不足一格的 delta 按 `WHEEL_DELTA` 累积，以支持高分辨率滚轮和触摸板。

相关回归：

- `test_right_edges_share_one_baseline`
- `test_folder_panel_wheel_covers_the_whole_panel`

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
- 焦点只归属 5 个输入框（帐号/服务器/端口/密码/保存路径）。点击后焦点仍在某个输入框、而落点不是这个框时，焦点收归主窗口 `root.focus_set()`——光标消失，Tab 从第一个控件重新开始。
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

- **关闭三态**：`close_action` = `ask`（默认）/ `tray` / `exit`，读写都在既有设置文件里，**不升 `UI_SETTINGS_VERSION`**（升版本会让老设置整份失效），非法值回退 `ask` + warning。关闭动作由纯函数 `decide_close(preference, hidden=)` 决定（`force_exit` / `hide` / `exit` / `ask`）；备份是否运行中只影响对话框文案与托盘「退出」的确认，不参与判定。
- **「已隐藏」只认 `root.state() == "withdrawn"`**，不设影子标志；据此定死一条规则：**隐藏状态下收到的关闭请求一律真退出**（那只可能是安装器/卸载器/任务管理器发来的），否则升级安装关不掉旧实例。
- **托盘线程边界与工作线程同款**：`imapbackup_tray.TrayHost` 的回调只往 `self.events` 投 `tray_show` / `tray_stop` / `tray_quit`，由 `_pump` 在 Tk 线程消费；界面侧所有托盘调用都必须走 `TrayHost` 的公开方法（内部转给托盘线程），绝不在托盘线程里碰 widget。托盘为 `None`（非 Windows / 建不起来 / `show()` 返回 False）时「隐藏到后台」退化为 `iconify()`——出口路径必须先成立。
- **图标常驻**：`TrayHost.show()` 是幂等的「确保挂出并刷新 tooltip」，启动时调一次（失败只记 warning），隐藏窗口前再调一次并以它的返回值决定是否隐藏；`hide()` 不存在——唯一移除点是真宿主的 `close()`（`_quit()` 调用）。tooltip 与菜单「停止备份」的可用性由 `_sync_tray()` 在启动、开始备份、运行结束、隐藏、恢复、后台通知处追平。
- **后台结束只发气泡**：`run_done` / `run_failed` 在 `_background_hidden()` 为真时不恢复窗口、不弹错误框，改为 `tray.notify(...)` + `_sync_tray()`；点气泡或左键图标走 `tray_show` 恢复窗口。
- **设置落盘仍是「一次写」**：设置页单选钮与关闭对话框只改内存 `_ui_settings`，隐藏/退出/外部关闭三条路径各调一次 `_save_column_config()`；隐藏时先落盘再 `withdraw()`（此刻几何还读得对）。
- **窗口状态记忆**：`_window_maximized` 由 `_restore_window_settings()` 初始化、`<Configure>` 在 state 为 `zoomed`/`normal` 时维护；`_window_settings()` 与 `_show_window()` 都读它，**不现场读 `wm state`**——最小化（iconic）或已隐藏（withdrawn）时现读会把最大化记丢（托盘菜单退出、外部关闭正是后一种）。`withdraw()`→`deiconify()` **不带回最大化**（真机实测：回来是 normal + 最大化前几何），隐藏恢复必须重放 `state("zoomed")`；iconic 分支保持 `state("normal")`——Tk 自己记得最小化前的状态（含最大化）。
- **页面外壳**：`page_host` 下 `backup_page` / `settings_page` 同格叠放，`_show_page()` 是唯一切页入口（`tkraise` + 菜单栏选中态 + 设置页回填）；菜单栏两项是顶层 radiobutton（不开下拉）。设置页两栏＝左 `Listbox` 分类栏（`SETTINGS_SECTIONS` 表驱动，固定 160px，`pack_propagate(False)`）+ 右详情（`tkraise` 切分类），**四边留白等宽**（`outer` 用 `padx=8, pady=8`：备份页的底边距由运行区自带，设置页下方没有内容，漏掉就会「三边等宽、底边贴边」）；设置页控件**不进 `_register`**。
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
