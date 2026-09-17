; imapbackup-gui 安装脚本 —— 包装 packaging/imapbackup-gui.spec 的 PyInstaller 产物
; 编译：iscc packaging\imapbackup-gui.iss     （仓库根目录；或直接用 build.bat 一条龙）
;
; 路径规则（踩过一次坑）：[Setup] 的相对路径和 [Files] Source 都相对「本脚本所在目录」解析，
; 不是相对当前工作目录。所以 SetupIconFile 写裸文件名，OutputDir/Source 用 ..\ 上跳到仓库根。

#define MyAppName       "IMAP Backup GUI"
#define MyAppVersion    "1.6.0"
#define MyAppPublisher  "INSIDEBang"
#define MyAppExeName    "imapbackup-gui.exe"
#define MyAppCliExeName "imapbackup-gui-cli.exe"
; 必须与 imapbackup_gui.APP_MUTEX_NAME 逐字一致：卸载时 Inno 靠它探测仍在运行的应用实例。
#define MyAppMutexName  "INSIDEBang.imapbackup-gui.single-instance"

[Setup]
AppId={{84768e5a-2307-4da3-bc7b-19a75a0a1239}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\imapbackup-gui
PrivilegesRequired=lowest
AppMutex={#MyAppMutexName}
OutputDir=..\dist\installer
OutputBaseFilename=imapbackup-gui-setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
DefaultGroupName={#MyAppName}
SetupIconFile=imapbackup-gui.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "Languages\ChineseSimplified.isl"

; 没有「把命令行工具加入 PATH」的任务。技术上在 6.7.3 上做得到——Pascal 有
; RegQueryStringValue(Root, Subkey, ValueName, var Out): Boolean 能读出原始 PATH
; （%VARIABLE% 不展开），配合 RegWriteExpandStringValue 写回能保住 REG_EXPAND_SZ 类型。
; 先不做的原因不是能力：本机只能编译安装程序、起不了 Inno 生成的 setup exe
; （Start-Process 一律「拒绝访问」，重试与换目录都无效），「装完 PATH 里真多了一条」
; 这一步只能靠用户双击安装器来验。这是唯一一个碰用户全局 PATH 的步骤，没在真机上
; 验过就先不发。CLI 目前走开始菜单入口 + 说明文档可达。
[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\imapbackup_gui\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} CLI"; Filename: "{app}\{#MyAppCliExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// 卸载时询问是否顺带删除运行日志。挂在 usUninstall：此时程序文件已删完，AppData 目录还在。
// DelTree 的四个开关都要 True：日志目录有子目录，也可能有只读或隐藏文件。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDataDir, Message: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;

  AppDataDir := ExpandConstant('{localappdata}\imapbackup-gui');
  if not DirExists(AppDataDir) then
    Exit;

  Message := '卸载 IMAP Backup GUI。' + #13 + #10 + #13 + #10 +
             '本机还留有该程序的运行日志：' + #13 + #10 +
             AppDataDir + #13 + #10 + #13 + #10 +
             '日志记录了 IMAP 服务器地址和登录用户名（不含密码）。' + #13 + #10 +
             '要连同日志一起删除吗？';
  if MsgBox(Message, mbConfirmation, MB_YESNO) = idYes then
    DelTree(AppDataDir, True, True, True);
end;
