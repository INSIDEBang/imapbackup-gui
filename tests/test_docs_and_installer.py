"""文档与安装器的静态契约：相对链接可解析，卸载器只删应用数据，身份与程序保持一致。

不碰 Tk、不读真实账号或备份：守的是 AGENTS.md 的跨文件一致性（AppMutex、AppId、版本号）
与「卸载器绝不触碰备份目录」这条硬边界。安装/卸载的人工验收仍按 ADR 0007 由用户执行。
"""
import re
from pathlib import Path

import imapbackup312 as core
import imapbackup_gui as gui

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "packaging" / "imapbackup-gui.iss"
APP_DATA_DIR = "{localappdata}\\imapbackup-gui\\"
RELATIVE_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _installer_text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _define(text: str, name: str) -> str:
    match = re.search(rf'^#define\s+{name}\s+"([^"]+)"', text, re.MULTILINE)
    assert match, name
    return match.group(1)


def test_relative_document_links_resolve():
    paths = [ROOT / "README.md", ROOT / "GLOSSARY.md", *sorted((ROOT / "docs").rglob("*.md"))]
    assert paths
    for path in paths:
        for link in RELATIVE_LINK_RE.findall(path.read_text(encoding="utf-8")):
            if "://" in link or link.startswith(("#", "mailto:")):
                continue
            target = (path.parent / link.split("#", 1)[0]).resolve()
            assert target.exists(), f"{path.relative_to(ROOT)}: dead link {link}"


def test_installer_identity_mutex_and_version_match_the_app():
    text = _installer_text()
    assert _define(text, "MyAppMutexName") == gui.APP_MUTEX_NAME
    assert _define(text, "MyAppVersion") == core.__version__ == gui.__version__
    # 升级/卸载身份：改了它，老版本的卸载与覆盖安装就断了
    assert "AppId={{84768e5a-2307-4da3-bc7b-19a75a0a1239}" in text


def test_uninstaller_deletes_only_app_data_with_the_expected_switches():
    text = _installer_text()
    logs_start = text.index("if DeleteLogs then begin")
    settings_start = text.index("if DeleteSettings then begin")
    assert logs_start < settings_start
    logs_branch = text[logs_start:settings_start]
    assert "DelTree(LogDir" in logs_branch and "index" not in logs_branch

    settings_branch = text[settings_start:]
    assert "treeview-columns.json" in settings_branch
    assert "DelTree(IndexDir" in settings_branch

    caption = re.search(r"DeleteSettingsBox\.Caption := '([^']+)'", text)
    assert caption and "界面设置" in caption.group(1) and "本地扫描索引" in caption.group(1)

    targets = set(re.findall(r"(?:DelTree|DeleteFile)\((\w+)", text))
    assert targets == {"LogDir", "SettingsFile", "IndexDir"}
    for variable in targets:
        assignment = re.search(rf"{variable} := ExpandConstant\('([^']+)'\)", text)
        assert assignment and assignment.group(1).startswith(APP_DATA_DIR), variable
