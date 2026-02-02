@echo off
REM Set admin password here
set ADMIN_PASSWORD=admin888

REM Set secret key (optional)
set SECRET_KEY=complex-secret-key-here

echo Starting server with configured password...
python server/app.py
pause
