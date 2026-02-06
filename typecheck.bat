@echo off
echo Running mypy type checking...
mypy --explicit-package-bases --ignore-missing-imports .
pause
