@echo off
echo Running flake8 linting...
flake8 . --exclude=main.py,launcher.py --count --select=E9,F63,F7,F82 --show-source --statistics
flake8 . --exclude=main.py,launcher.py --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics
pause
