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
; 2026-09-19 最终拍板：不做（CLI 有开始菜单入口可达，受众以 GUI 为主）。
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
const
  // 与 [Setup] AppId（{{ 转义的那个 GUID）逐字对应：Inno 以 <AppId>_is1 为键把卸载信息
  // 写进 HKCU（PrivilegesRequired=lowest）。[Code] 字符串里大括号是普通字符，无需再转义。
  UninstallRegKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{84768e5a-2307-4da3-bc7b-19a75a0a1239}_is1';

var
  DeleteLogs: Boolean;
  DeleteSettings: Boolean;

// 命令行是否带 /NOSTYLE 标记。该开关本是官方惰性开关（禁用自定义样式，本脚本没定义
// 样式，等于无副作用），由安装后钩子把它追加进 UninstallString（见 CurStepChanged），
// 兼作「本次卸载来自控制面板/设置→应用」的标记。
function ArpWizardMode(): Boolean;
begin
  Result := Pos('/NOSTYLE', GetCmdTail()) > 0;
end;

// 向导页面骨架：与官方进度窗（Setup.UninstallProgressForm.dfm）同构的三段式——
// 页首＝白色 TPanel（clWindow、顶部通栏）承载粗体标题与描述（modern 样式下进度窗
// 也没有页首分隔线，靠颜色差分区）；内容＝灰色留白区，各页自放控件；页脚＝TBevel
// 顶线＋按钮条（高 23、底边距 10、右缘 12px）。三页（欢迎/进度/完成）同尺寸同结构。
// 边缘锚定元素（页首通栏、页脚线、按钮右对齐）一律运行时读 Form.ClientWidth 定位：
// CreateCustomForm 对客户区的缩放与全局 ScaleX 的因子在高 DPI/中文字体下不一致，
// 纯 ScaleX(497) 定宽会让页脚线够不着右缘（真机截图「分割线中断」的根因）。
procedure CreateWizardPageBase(var Form: TSetupForm; const TitleText, DescText: String; var ContentTop: Integer);
var
  HeaderPanel: TPanel;
  PageNameLabel, PageDescriptionLabel: TLabel;
  HeaderBevel, FooterBevel: TBevel;
begin
  // CreateCustomForm 自 6.6.0 起为 4 参签名：宽高创建时传入、之后只读
  Form := CreateCustomForm(ScaleX(497), ScaleY(360), False, False);
  Form.Caption := SetupMessage(msgUninstallAppTitle);   // 与进度窗标题一致（「卸载」）
  Form.Position := poScreenCenter;
  Form.BorderStyle := bsDialog;

  // 页首：白色通栏面板
  HeaderPanel := TPanel.Create(Form);
  HeaderPanel.Parent := Form;
  HeaderPanel.BevelOuter := bvNone;
  HeaderPanel.ParentBackground := False;
  HeaderPanel.Color := clWindow;
  HeaderPanel.Left := ScaleX(0);
  HeaderPanel.Top := ScaleY(0);
  HeaderPanel.Width := Form.ClientWidth;
  HeaderPanel.Height := ScaleY(58);

  PageNameLabel := TLabel.Create(Form);
  PageNameLabel.Parent := HeaderPanel;
  PageNameLabel.Left := ScaleX(24);
  PageNameLabel.Top := ScaleY(8);
  PageNameLabel.Transparent := True;
  PageNameLabel.Font.Style := [fsBold];
  PageNameLabel.Caption := TitleText;

  PageDescriptionLabel := TLabel.Create(Form);
  PageDescriptionLabel.Parent := HeaderPanel;
  PageDescriptionLabel.Left := ScaleX(40);
  PageDescriptionLabel.Top := PageNameLabel.Top + PageNameLabel.Height + ScaleY(4);
  PageDescriptionLabel.Width := ScaleX(389);
  PageDescriptionLabel.Height := ScaleY(34);
  PageDescriptionLabel.AutoSize := False;
  PageDescriptionLabel.WordWrap := True;
  PageDescriptionLabel.Caption := DescText;

  // 面板高度贴合内容：中文字体比官方 dfm 基准的 MS Sans 高，固定 58 会裁字
  HeaderPanel.Height := PageDescriptionLabel.Top + PageDescriptionLabel.Height + ScaleY(7);
  ContentTop := HeaderPanel.Height;

  // 页首底边横线：官方进度窗（classic 向导样式）页首下方有这条分隔线，缺了就显得
  // 页首与内容没有分界（真机比对）
  HeaderBevel := TBevel.Create(Form);
  HeaderBevel.Parent := Form;
  HeaderBevel.Shape := bsTopLine;
  HeaderBevel.Left := ScaleX(0);
  HeaderBevel.Top := HeaderPanel.Top + HeaderPanel.Height;
  HeaderBevel.Width := Form.ClientWidth;
  HeaderBevel.Height := ScaleY(2);

  // 页脚：贴底定位（官方 dfm：线在客户区底上 47px、按钮底边距 10px）。垂直锚点同样
  // 不能信 ScaleY 与窗体客户区的等比关系（真机截图「页脚高度过高」即由此来），
  // 运行时按 ClientHeight 反推。
  FooterBevel := TBevel.Create(Form);
  FooterBevel.Parent := Form;
  FooterBevel.Shape := bsTopLine;
  FooterBevel.Left := ScaleX(0);
  FooterBevel.Top := Form.ClientHeight - ScaleY(47);
  FooterBevel.Width := Form.ClientWidth;
  FooterBevel.Height := ScaleY(2);
end;

// 向导首页（InitializeUninstall 位于原生确认弹窗之前、任何删除之前——用户真机截图
// 证实的顺序）。显示条件：非静默（直跑 unins000.exe 的老路径，后面仍跟原生确认/完成
// 弹窗），或带 /NOSTYLE 标记（控制面板/设置→应用路径：UninstallString 已被改写成
// /SILENT /NOSTYLE，原生「确认要完全移除」与「已顺利移除」两个弹窗都被 /SILENT 压掉，
// 交互全由这里接管）。真静默（手动 /SILENT、无标记，如管理员自动化）不弹任何窗体，
// 按默认勾选＝删。页首描述刻意不提「运行日志」：复选框才是选项的本体，描述保持通用，
// 日后加新选项不用改它。
function InitializeUninstall(): Boolean;
var
  Form: TSetupForm;
  ContentTop: Integer;
  DeleteLogsBox, DeleteSettingsBox: TNewCheckBox;
  HintLabel: TLabel;
  UninstallButton, CancelButton: TNewButton;
begin
  Result := True;
  DeleteLogs := True;   // 默认＝删：真静默直接沿用，交互模式被窗体结果覆盖
  DeleteSettings := True;

  if UninstallSilent() and (not ArpWizardMode()) then
    Exit;

  CreateWizardPageBase(Form,
    '欢迎使用 IMAP Backup GUI 卸载向导',
    '此向导将从您的计算机中卸载 IMAP Backup GUI。' + #13#10 +
    '您可在下方选择卸载时要一并执行的操作。',
    ContentTop);
  try
    DeleteLogsBox := TNewCheckBox.Create(Form);
    DeleteLogsBox.Parent := Form;
    DeleteLogsBox.Left := ScaleX(40);
    DeleteLogsBox.Top := ContentTop + ScaleY(20);
    DeleteLogsBox.Width := ScaleX(400);
    DeleteLogsBox.Height := ScaleY(24);
    DeleteLogsBox.Caption := '同时删除运行日志（含服务器地址与账号名，不含密码）';
    DeleteLogsBox.Checked := True;

    DeleteSettingsBox := TNewCheckBox.Create(Form);
    DeleteSettingsBox.Parent := Form;
    DeleteSettingsBox.Left := DeleteLogsBox.Left;
    DeleteSettingsBox.Top := DeleteLogsBox.Top + DeleteLogsBox.Height;
    DeleteSettingsBox.Width := DeleteLogsBox.Width;
    DeleteSettingsBox.Height := DeleteLogsBox.Height;
    DeleteSettingsBox.Caption := '同时删除界面设置';
    DeleteSettingsBox.Checked := True;

    HintLabel := TLabel.Create(Form);
    HintLabel.Parent := Form;
    HintLabel.Left := ScaleX(40);
    HintLabel.Top := DeleteSettingsBox.Top + DeleteSettingsBox.Height + ScaleY(12);
    HintLabel.Transparent := True;
    HintLabel.Caption := '准备好后，单击「卸载(U)」开始卸载。';

    UninstallButton := TNewButton.Create(Form);
    UninstallButton.Parent := Form;
    UninstallButton.Top := Form.ClientHeight - ScaleY(33);
    UninstallButton.Width := ScaleX(88);
    UninstallButton.Height := ScaleY(23);
    UninstallButton.Caption := '卸载(&U)';
    UninstallButton.ModalResult := mrOk;
    UninstallButton.Default := True;

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Top := Form.ClientHeight - ScaleY(33);
    CancelButton.Width := ScaleX(88);
    CancelButton.Height := ScaleY(23);
    CancelButton.Caption := '取消(&C)';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;

    // 页脚右对齐：右缘 12px、键间距 10px（按实际客户区反推，与进度窗「取消」同位）
    CancelButton.Left := Form.ClientWidth - ScaleX(12) - CancelButton.Width;
    UninstallButton.Left := CancelButton.Left - ScaleX(10) - UninstallButton.Width;

    if Form.ShowModal = mrOk then
    begin
      DeleteLogs := DeleteLogsBox.Checked;
      DeleteSettings := DeleteSettingsBox.Checked;
    end
    else
      Result := False;   // 取消/关窗/Esc：中止整个卸载，什么都不动
  finally
    Form.Free;
  end;
end;

procedure ShowFinishDialog();
var
  Form: TSetupForm;
  ContentTop: Integer;
  FinishButton: TNewButton;
begin
  CreateWizardPageBase(Form,
    '正在完成 IMAP Backup GUI 卸载向导',
    'IMAP Backup GUI 已从您的计算机中移除。' + #13#10 +
    '单击「完成(F)」退出卸载向导。',
    ContentTop);
  try
    FinishButton := TNewButton.Create(Form);
    FinishButton.Parent := Form;
    FinishButton.Top := Form.ClientHeight - ScaleY(33);
    FinishButton.Width := ScaleX(88);
    FinishButton.Height := ScaleY(23);
    FinishButton.Caption := '完成(&F)';
    FinishButton.ModalResult := mrOk;
    FinishButton.Default := True;
    FinishButton.Left := Form.ClientWidth - ScaleX(12) - FinishButton.Width;
    // 无取消路径：卸载已完成，Esc/关窗等同「完成」
    Form.ShowModal;
  finally
    Form.Free;
  end;
end;

// 安装完成后把 ARP 的卸载命令改写为「/SILENT /NOSTYLE」：/SILENT 压掉原生「确认要完全
// 移除」与「已顺利移除」两个弹窗（进度窗体保留，官方语义），交互全部由向导窗体接管；
// 每次重装本钩子都会重跑，Inno 重写注册表后标记得以续上。设置→应用优先读
// QuietUninstallString，故两值同写。
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    RegWriteStringValue(HKEY_CURRENT_USER, UninstallRegKey, 'UninstallString',
      ExpandConstant('"{uninstallexe}"') + ' /SILENT /NOSTYLE');
    RegWriteStringValue(HKEY_CURRENT_USER, UninstallRegKey, 'QuietUninstallString',
      ExpandConstant('"{uninstallexe}"') + ' /SILENT /NOSTYLE');
  end;
end;

// 删除决策已在向导首页点「卸载」时锁定。挂 usPostUninstall（官方定义：卸载刚完成、
// 文件已删；进度中途点「取消」走不到这里，日志和设置自然保留）。
// DelTree 的四个开关都要 True：日志目录有子目录，也可能有只读或隐藏文件。
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  LogDir, SettingsFile: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  if DeleteLogs then begin
    LogDir := ExpandConstant('{localappdata}\imapbackup-gui\log');
    if DirExists(LogDir) then
      DelTree(LogDir, True, True, True);
  end;

  if DeleteSettings then begin
    SettingsFile := ExpandConstant('{localappdata}\imapbackup-gui\treeview-columns.json');
    if FileExists(SettingsFile) then
      DeleteFile(SettingsFile);
  end;

  // 完成对话框只在 ARP 向导模式补位——该路径的原生「已顺利移除」弹窗已被 /SILENT
  // 压掉；直跑 unins000.exe 的非静默路径不弹，避免与原生弹窗双弹。
  if ArpWizardMode() then
    ShowFinishDialog();
end;
