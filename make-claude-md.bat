@echo off
REM Create/refresh CLAUDE.md so it mirrors AGENTS.md.
REM Re-runnable: run it again any time the link needs rebuilding.
REM   1) symbolic link  (needs Developer Mode or Administrator)
REM   2) hard link      (no elevation needed, same NTFS volume)
REM   3) plain copy     (last resort - re-run after editing AGENTS.md)
setlocal
cd /d "%~dp0"

if not exist "AGENTS.md" (
    echo [error] AGENTS.md not found in %CD%
    exit /b 1
)

if exist "CLAUDE.md" (
    del /f /q "CLAUDE.md" >nul 2>&1
    if exist "CLAUDE.md" rmdir /s /q "CLAUDE.md" >nul 2>&1
)

mklink "CLAUDE.md" "AGENTS.md" >nul 2>&1 && (
    echo [ok] symlink: CLAUDE.md -^> AGENTS.md
    exit /b 0
)

mklink /H "CLAUDE.md" "AGENTS.md" >nul 2>&1 && (
    echo [ok] hardlink: CLAUDE.md == AGENTS.md
    exit /b 0
)

copy /y "AGENTS.md" "CLAUDE.md" >nul 2>&1 && (
    echo [warn] copied instead of linked - re-run this script after editing AGENTS.md
    exit /b 0
)

echo [error] could not create CLAUDE.md
exit /b 1
