# GUI 变更约束

修改 `imapbackup_gui.py` 的任何用户可见行为或交互状态前读取本文，并以 `tests/test_gui_smoke.py` 为回归契约。

## 核心接缝与线程

- GUI 复用 `imapbackup312.py` 的连接、枚举、扫描、判重和下载逻辑，不复制核心实现。
- 工作线程执行连接、枚举、扫描、下载和 logout，通过 `queue` 回投事件；主线程以 `after()` 轮询。工作线程不直接访问 widget。
- GUI 通过结构化 `report` 获取进度，通过 `should_stop` 协作停止；调用核心时保持 `quiet=False`、`nospinner=True`。具体契约见 ADR 0003。
- 停止只在下一封消息或下一个文件夹前生效，当前 I/O 可能持续到 socket 超时。界面只使用“停止”，不引入“暂停/继续”语义。

## 按钮与加载状态

- 开始与停止图标由 `PhotoImage` 在代码中生成，不增加图片资产；Python 侧必须持有引用，避免图标被垃圾回收。
- 连接与备份共用 `SPINNER_FRAMES`、`SPINNER_INTERVAL_MS` 和 `_set_loading_frame()`。修改帧或节奏时同时覆盖 `_connect_spinner_tick` 与 `_run_spinner_tick` 两条调度链。
- 开始和停止按钮不设置固定 `width`。加载态使用 `_blank_icon()` 占住图标槽，不把 `image` 设为 `""`，避免按钮高度变化。
- 点击停止后，转圈移到停止按钮；开始按钮恢复静止外观但保持禁用；状态行只清除文案而不收回布局；配置区显示停止提示。
- 完成、停止、失败以及帧链自愈路径都必须恢复两颗按钮和配置区提示。

相关回归：

- `test_start_button_borrows_the_connecting_spinner_while_backing_up`
- `test_loading_buttons_keep_their_height`
- `test_stopping_feedback_covers_buttons_status_line_and_hint`
- `test_stopping_then_failing_restores_the_controls`

## 布局与滚轮

- 主内容左右边距保持同一基线；单个面板不额外增加右边距。邮箱文件夹面板的滚条贴住面板右边框，并与下方区域右边线对齐。
- 邮箱文件夹滚轮使用窗口级入口 `root.bind("<MouseWheel>", _on_wheel)`；沿事件控件的 `.master` 向上判断是否位于 `folder_group`，其他区域返回 `None` 放行。
- 面板内滚条也调用 `_on_wheel` 并返回 `"break"`，由该入口完成滚动，避免 `TScrollbar` 默认步长覆盖统一节奏。
- 每格滚动 `FOLDER_WHEEL_ROWS` 行；`yscrollincrement` 使用实测行距。不足一格的 delta 按 `WHEEL_DELTA` 累积，以支持高分辨率滚轮和触摸板。

相关回归：

- `test_right_edges_share_one_baseline`
- `test_folder_panel_wheel_covers_the_whole_panel`

## 邮件列表选中

- 选中高亮没有动作消费者：基于选中的操作一个也没有（右键菜单复制的是落点单元格，不是选中行），它只是「刚点过哪一行」的视觉痕迹。
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

## 单实例与验证

`APP_MUTEX_NAME` 必须与 `packaging/imapbackup-gui.iss` 的 `AppMutex` 完全一致。修改 GUI 后至少运行：

```bash
.venv/Scripts/python.exe -m py_compile imapbackup_gui.py
.venv/Scripts/python.exe -m pytest -m gui -q
```

涉及互斥量、图标路径或打包入口时，另运行 `packaging\build.bat` 并按 ADR 0007 人工验收安装与卸载行为。
