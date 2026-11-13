@chcp 65001 >nul
@echo off
setlocal
cd /d "%~dp0"
title AutoTube Studio
echo AutoTube Studio 시작 준비 중...
echo.

rem ---- 0. 이 폴더에 쓸 수 있는지 확인 ----
(echo test> "_write_test.tmp") >nul 2>&1
if not exist "_write_test.tmp" goto NOWRITE
del "_write_test.tmp" >nul 2>&1

rem ---- 1. Python 찾기 (py 런처 우선, Microsoft Store 가짜 python 제외) ----
set "PY="
where py >nul 2>&1 && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY goto NOPY
for /f "delims=" %%v in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo [확인] Python %PYVER% 사용

rem ---- 2. 전용 환경 만들기 (실패했던 반쪽짜리 환경은 지우고 다시) ----
if exist ".venv" if not exist ".venv\Scripts\python.exe" rmdir /s /q ".venv"
if not exist ".venv\Scripts\python.exe" (
  echo [1/2] 처음 실행: 전용 Python 환경을 만듭니다...
  %PY% -m venv .venv > install_log.txt 2>&1
)
if not exist ".venv\Scripts\python.exe" goto VENVFAIL

rem ---- 3. 구성요소 설치 (오류 내용은 install_log.txt 에 저장) ----
echo [2/2] 필요한 구성요소 확인 중... 처음에는 몇 분 걸립니다.
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade pip >> install_log.txt 2>&1
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt >> install_log.txt 2>&1
if errorlevel 1 goto PIPFAIL
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, PIL" >nul 2>&1 || goto PIPFAIL

echo.
echo [3/3] 바탕화면과 시작 메뉴에 "AutoTube Studio" 아이콘을 만듭니다...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w=New-Object -ComObject WScript.Shell; foreach($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))){ $s=$w.CreateShortcut((Join-Path $d 'AutoTube Studio.lnk')); $s.TargetPath='%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments='-m autotube.launch'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0assets\autotube.ico'; $s.Description='AutoTube Studio'; $s.Save() }"
echo.
echo 준비 완료. 앞으로는 바탕화면의 "AutoTube Studio" 아이콘만 누르면 됩니다.
echo 업데이트는 자동으로 받습니다. 이 창은 3초 뒤 닫힙니다.
start "" "%~dp0.venv\Scripts\pythonw.exe" -m autotube.launch
timeout /t 3 >nul
exit /b 0

:NOWRITE
echo [오류] 이 폴더에 파일을 만들 권한이 없습니다: %CD%
echo   해결 1 - 추천: 폴더를 C:\AutoTube 처럼 C 드라이브로 옮긴 뒤 거기서 실행
echo   해결 2: Windows 보안 - 바이러스 및 위협 방지 - 랜섬웨어 방지 - 제어된 폴더 액세스 에서 차단 기록 확인
echo   해결 3: START_WINDOWS.cmd 우클릭 - 관리자 권한으로 실행
pause
exit /b 1

:NOPY
echo [오류] Python 3.11 이상을 찾지 못했습니다. 아래를 순서대로 확인하세요.
echo   1. python.org 에서 설치할 때 첫 화면의 "Add python.exe to PATH" 를 체크했나요?
echo      - 안 했다면: 설치 파일을 다시 실행 - Modify - Next - "Add Python to environment variables" 체크 - Install
echo   2. Windows 설정 - 앱 - 고급 앱 설정 - 앱 실행 별칭 에서 python.exe, python3.exe 를 끄기
echo   3. 이 창과 파일 탐색기를 모두 닫고 START_WINDOWS.cmd 를 다시 실행 - 그래도 안 되면 PC 재시작
echo.
echo 참고 - 지금 PC에서 찾은 python:
where python 2>nul
where py 2>nul
pause
exit /b 1

:VENVFAIL
echo [오류] 전용 환경을 만들지 못했습니다. 아래 내용을 캡처해서 보내주세요.
type install_log.txt
pause
exit /b 1

:PIPFAIL
echo [오류] 구성요소 설치에 실패했습니다. 아래 마지막 부분을 캡처해서 보내주세요.
echo   - 회사/학교 네트워크, 백신 프로그램이 막는 경우도 있습니다.
echo ------------------------------------------------------------
powershell -NoProfile -Command "Get-Content -Path 'install_log.txt' -Tail 25"
echo ------------------------------------------------------------
pause
exit /b 1
