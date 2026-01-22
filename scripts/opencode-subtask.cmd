@echo off
REM Windows wrapper for Codex skill execution
set SCRIPT_DIR=%~dp0
python "%SCRIPT_DIR%opencode_subtask.py" %*
