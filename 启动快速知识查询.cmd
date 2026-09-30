@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0快速知识查询.exe" (
  start "" "%~dp0快速知识查询.exe"
) else (
  start "" pythonw.exe "%~dp0app.py"
)
