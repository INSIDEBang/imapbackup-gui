# -*- coding: utf-8 -*-
"""imapbackup-gui 打包规格 —— 两个入口共享同一份运行库（onedir）。

    imapbackup-gui.exe      GUI 入口，无控制台窗口
    imapbackup-gui-cli.exe  CLI 入口，带控制台；就是 imapbackup312.py，参数与直接跑脚本一致

两个 EXE 收进同一个 COLLECT，所以 _internal/ 里的运行库只打一次（不是一份 exe 配一套 DLL）。
onedir 而不是 onefile：onfile 每次启动都要解包到临时目录，慢，还常被杀软误报；
Inno 的 [Files] 段也更需要一个目录而不是单个文件。

图标既嵌进两个 exe（文件图标/控制面板），又以数据文件形式放进 _internal/，
供运行时的 root.iconbitmap() 用 —— 见 imapbackup_gui._window_icon_path()。

用法（仓库根目录）：
    .venv/Scripts/python.exe -m PyInstaller packaging/imapbackup-gui.spec --noconfirm --clean
"""
from pathlib import Path

SPECDIR = Path(SPECPATH).resolve()          # packaging/
REPO = SPECDIR.parent                       # 仓库根
ICON = str(SPECDIR / "imapbackup-gui.ico")

a = Analysis(
    [str(REPO / "imapbackup_gui.py"), str(REPO / "imapbackup312.py")],
    pathex=[str(REPO)],
    binaries=[],
    # 第二项 "." = 数据目录根；onedir 下即 _internal/
    datas=[(ICON, ".")],
    # imapbackup312 同时是 CLI 的脚本条目和 GUI 的 import 目标；脚本条目本身不进可导入模块表，
    # 这里补一份，否则冻结后的 GUI 一启动就 ImportError。
    hiddenimports=["imapbackup312"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)


def script_entry(name: str) -> list:
    """取「运行时钩子 + 指定应用脚本」的组合条目。

    a.scripts = rthook_toc + program_toc：
      - 不能按下标切，切出来是 pyi_rth_* 钩子，应用脚本会被整个漏掉，exe 静默空跑；
      - 也不能只留应用脚本，丢掉 pyi_rth__tkinter 就会没人给 Tcl 设置库路径，
        GUI 启动时报 "Can't find a usable init.tcl"。
    所以钩子必须原样带上、顺序不变（钩子在前，脚本在后）。
    """
    rthooks = [e for e in a.scripts if e[0].startswith("pyi_rth_")]
    hits = [e for e in a.scripts if e[0] == name]
    if len(hits) != 1:
        raise SystemExit(f"spec: 脚本条目 {name!r} 命中 {len(hits)} 条，期望 1 条")
    return rthooks + hits


exe_gui = EXE(
    pyz, script_entry("imapbackup_gui"), [],
    exclude_binaries=True,
    name="imapbackup-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
exe_cli = EXE(
    pyz, script_entry("imapbackup312"), [],
    exclude_binaries=True,
    name="imapbackup-gui-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
coll = COLLECT(
    exe_gui, exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="imapbackup_gui",
)
