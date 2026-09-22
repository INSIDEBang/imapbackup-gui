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

- 选中高亮没有消费者（没有绑定、右键菜单或基于选中的动作），只是「刚点过哪一行」的视觉痕迹：鼠标落在邮件行之外的任何地方都清空它。
- 入口也是窗口级：`root.bind("<Button-1>", _on_click_clear_selection)`；用 `identify_row(event.y)` 区分行与空白——空白、列标题、分隔区都返回空串，一次调用覆盖全部非行区域。
- 例外两个：邮件行本身（换选与多选归 Treeview 类绑定，处理器不插手）与邮件列表自己的滚条（拖它多半是在查看选中行）。Alt-Tab 切窗口不清——用 `<FocusOut>` 会破坏这一点。
- 处理器只清理、不拦截：返回 `None`，不返回 `"break"`；控件匹配用路径比较而非对象同一性，理由同滚轮入口。

相关回归：

- `test_row_selection_clears_on_click_outside`

## 单实例与验证

`APP_MUTEX_NAME` 必须与 `packaging/imapbackup-gui.iss` 的 `AppMutex` 完全一致。修改 GUI 后至少运行：

```bash
.venv/Scripts/python.exe -m py_compile imapbackup_gui.py
.venv/Scripts/python.exe -m pytest -m gui -q
```

涉及互斥量、图标路径或打包入口时，另运行 `packaging\build.bat` 并按 ADR 0007 人工验收安装与卸载行为。
