@echo off
REM imapbackup-gui 构建一条龙：PyInstaller 打运行库 -> Inno Setup 编译安装程序
REM 在仓库根目录运行：packaging\build.bat
REM
REM 前置条件：
REM   .venv\Scripts\python.exe 里装了 PyInstaller 6.x（系统 python 是 Store 占位符，不能用）
REM   Inno Setup 6.x 已安装；若装别处，改下面的 ISCC

setlocal
set PY=.venv\Scripts\python.exe
set ISCC=D:\Applications\Inno Setup 6\ISCC.exe
set SCRIPT_DIR=%~dp0
set REPO_DIR=%SCRIPT_DIR%..

if not exist "%PY%" (
    echo [错误] 找不到 %PY%
    echo        先建虚拟环境并装 PyInstaller：
    echo          python -m venv .venv
    echo          .venv\Scripts\pip install pyinstaller
    exit /b 1
)
if not exist "%ISCC%" (
    echo [错误] 找不到 %ISCC%
    echo        安装 Inno Setup 6，或把上面那行改成你的 ISCC.exe 实际路径。
    exit /b 1
)

cd /d "%REPO_DIR%"
if errorlevel 1 exit /b 1

echo.
echo == 1/2 PyInstaller（两个 exe 共享一份运行库）==
"%PY%" -m PyInstaller "%SCRIPT_DIR%imapbackup-gui.spec" --noconfirm --clean
if errorlevel 1 (
    echo [失败] PyInstaller 未通过
    exit /b 1
)

echo.
echo == 2/2 Inno Setup ==
"%ISCC%" "%SCRIPT_DIR%imapbackup-gui.iss"
if errorlevel 1 (
    echo [失败] ISCC 未通过
    exit /b 1
)

echo.
echo == 完成 ==
echo 安装程序：dist\installer\imapbackup-gui-setup-1.6.0.exe
echo 散包目录：dist\imapbackup_gui\
endlocal
