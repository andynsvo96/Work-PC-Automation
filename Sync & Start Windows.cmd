@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" safe_sync.py start
) else (
  python safe_sync.py start
)
