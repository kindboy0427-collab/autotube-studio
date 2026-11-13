@chcp 65001 >nul
@echo off
setlocal
title 구독 요금제 연결 (Claude Pro / ChatGPT Plus)
echo 이 작업은 이미 결제 중인 Claude Pro 와 ChatGPT Plus 를 대본 작성에 쓰도록 공식 도구를 설치합니다.
echo  - Claude Code : Anthropic 공식 명령창 도구 (Claude Pro 계정으로 로그인)
echo  - Codex CLI   : OpenAI 공식 명령창 도구 (ChatGPT Plus 계정으로 로그인)
echo.
where node >nul 2>&1 || (
  echo Node.js 가 필요합니다. https://nodejs.org 에서 LTS 버전을 설치한 뒤 다시 실행하세요.
  pause & exit /b 1
)
where claude >nul 2>&1 || (
  echo [1/4] Claude Code 설치...
  call npm install -g @anthropic-ai/claude-code || (echo Claude Code 설치 실패 & pause & exit /b 1)
)
where codex >nul 2>&1 || (
  echo [2/4] Codex CLI 설치...
  call npm install -g @openai/codex || (echo Codex 설치 실패 & pause & exit /b 1)
)
echo.
echo [3/4] Claude 로그인: 창이 열리면 /login 으로 Claude Pro 계정(Claude.ai 구독)을 선택하고, 끝나면 /exit 입력
call claude
echo.
echo [4/4] Codex 로그인: 브라우저에서 "Sign in with ChatGPT" 로 Plus 계정 로그인
call codex login
echo.
echo 완료. AutoTube Studio 대시보드의 준비 상태에서 두 항목이 초록색인지 확인하세요.
pause
