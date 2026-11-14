@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto :cwd_failed

set "MCP_HOST=127.0.0.1"
set "MCP_PORT=8819"
set "MCP_PATH=/mcp"
set "MCP_TRANSPORT=streamable-http"
set "MCP_URL=http://%MCP_HOST%:%MCP_PORT%%MCP_PATH%"

title Canvas MCP Server
echo ============================================================
echo   Canvas MCP Server
echo ============================================================
echo.

where uv >nul 2>nul
if errorlevel 1 goto :uv_missing

if not exist ".env" goto :create_env
goto :check_env

:create_env
if not exist "env.template" goto :template_missing
copy /Y "env.template" ".env" >nul
if errorlevel 1 goto :env_copy_failed
echo Created .env from env.template.
echo Set CANVAS_API_URL in .env and save the file, then continue.
echo.
start "" notepad ".env"
pause
goto :check_env

:check_env
findstr /R /C:"CANVAS_API_URL=." ".env" >nul 2>nul
if errorlevel 1 goto :api_url_missing
findstr /C:"CANVAS_API_URL=https://your-institution.instructure.com/api/v1" ".env" >nul 2>nul
if not errorlevel 1 goto :api_url_placeholder
goto :check_port

:check_port
netstat -ano | findstr /C:":%MCP_PORT% " | findstr /I "LISTENING" >nul 2>nul
if not errorlevel 1 goto :port_in_use
goto :sync

:sync
echo Installing or updating dependencies...
uv sync
if errorlevel 1 goto :sync_failed
goto :launch

:launch
echo.
echo ============================================================
echo   Copy this address into your MCP client:
echo.
echo       !MCP_URL!
echo.
echo   Add it as a Streamable HTTP MCP server.
echo   Keep this window open while you use Canvas.
echo ============================================================
echo.
echo Starting Canvas MCP Server...
echo.

uv run canvas-mcp-server --transport "%MCP_TRANSPORT%" --host "%MCP_HOST%" --port "%MCP_PORT%"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "!EXIT_CODE!"=="0" echo ERROR: Canvas MCP Server stopped with exit code !EXIT_CODE!.
if "!EXIT_CODE!"=="0" echo Canvas MCP Server stopped.
echo.
pause
exit /b !EXIT_CODE!

:cwd_failed
echo ERROR: Could not change to the folder containing start.bat.
echo Move the project to a local drive and run start.bat again.
echo.
pause
exit /b 1

:uv_missing
echo ERROR: uv is not installed or is not on PATH.
echo Install it from https://docs.astral.sh/uv/getting-started/installation/
echo then run this file again.
echo.
pause
exit /b 1

:template_missing
echo ERROR: .env is missing and env.template was not found.
echo Create a .env file with CANVAS_API_URL set, then run start.bat again.
echo.
pause
exit /b 1

:env_copy_failed
echo ERROR: Could not create .env from env.template.
echo Check that this folder is writable, then run start.bat again.
echo.
pause
exit /b 1

:api_url_missing
echo ERROR: CANVAS_API_URL is missing or empty in .env.
echo Set it to your institution's Canvas API URL, save the file, and run start.bat again.
echo.
start "" notepad ".env"
pause
exit /b 1

:api_url_placeholder
echo ERROR: CANVAS_API_URL in .env is still the placeholder.
echo Open .env, set your institution's Canvas API URL, save it, and run start.bat again.
echo.
start "" notepad ".env"
pause
exit /b 1

:port_in_use
echo Port %MCP_PORT% is already in use.
echo If Canvas MCP is already running, use this address in your MCP client:
echo.
echo     !MCP_URL!
echo.
echo Otherwise stop the other program using port %MCP_PORT% and run start.bat again.
echo.
pause
exit /b 1

:sync_failed
echo.
echo ERROR: uv sync failed. Read the output above, fix the problem, and try again.
echo.
pause
exit /b 1
