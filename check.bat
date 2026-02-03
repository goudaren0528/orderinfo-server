@echo off
echo ==========================================
echo Running Lint and Type Check
echo ==========================================
echo.
echo [1/2] Running Pylint...
call lint.bat nopause
echo.
echo [2/2] Running Mypy...
call typecheck.bat nopause
echo.
echo Done.
pause
