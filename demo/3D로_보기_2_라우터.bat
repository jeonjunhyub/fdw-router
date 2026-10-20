@echo off
chcp 65001 >nul
rem Isaac Sim 창을 띄워 [라우터(작업대 고르기)] 판 시연을 실시간으로 본다. 창을 닫거나 Ctrl+C 로 끝낸다. (Isaac Sim 6.1 venv 필요: C:\isaacsim\env)
cd /d "%~dp0"
echo Isaac Sim 창을 띄웁니다. 첫 부팅 40초 안팎, 자산 로드 후 시뮬레이션이 실시간으로 돕니다.
C:\isaacsim\env\Scripts\python.exe -X utf8 run_demo_3d.py --label router --router --gui --every 0
pause
