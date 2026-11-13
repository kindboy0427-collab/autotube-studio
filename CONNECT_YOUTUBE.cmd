@chcp 65001 >nul
@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo 먼저 START_WINDOWS.cmd 를 한 번 실행하세요 & pause & exit /b 1)
if not exist "data\client_secret.json" (
  echo data\client_secret.json 이 없습니다.
  echo Google Cloud 콘솔 - API 및 서비스 - 사용자 인증 정보 - OAuth 클라이언트 ID(데스크톱 앱) 를 만들고
  echo 내려받은 JSON 파일을 data\client_secret.json 이름으로 넣은 뒤 다시 실행하세요.
  pause & exit /b 1
)
set "CH=%~1"
if "%CH%"=="" set /p CH=연결할 채널 ID (예: space, physics - 웹의 채널 탭에서 확인): 
echo 브라우저가 열리면 [%CH%] 채널의 Google 계정으로 로그인하고 권한을 허용하세요.
".venv\Scripts\python.exe" -m autotube.youtube_upload %CH%
pause
