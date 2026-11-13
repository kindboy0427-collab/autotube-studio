@chcp 65001 >nul
@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (echo 먼저 START_WINDOWS.cmd 를 한 번 실행하세요 & pause & exit /b 1)
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AutoTube Studio (background).lnk"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);$s.TargetPath='%~dp0.venv\Scripts\pythonw.exe';$s.Arguments='-m autotube.launch --background';$s.WorkingDirectory='%~dp0';$s.IconLocation='%~dp0assets\autotube.ico';$s.Save()"
echo Windows 로그인 시 AutoTube Studio가 화면 없이 조용히 실행되어 AI 학습을 계속합니다.
echo 창은 바탕화면 아이콘을 누르면 열립니다. (해제: 시작프로그램 폴더에서 바로가기 삭제)
pause
