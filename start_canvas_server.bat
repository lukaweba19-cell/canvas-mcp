@echo off
REM Canvas MCP server launcher (Windows).
REM
REM Authentication is your Canvas browser session: no API token. The first
REM Canvas tool call opens a Chrome window so you can sign in; it closes once
REM the session is captured.
REM
REM Default endpoint: http://127.0.0.1:8819/mcp
REM Add --transport stdio if your MCP client spawns the server instead.
setlocal
cd /d "%~dp0"

if not exist ".env" (
    echo Error: .env not found. Create one with CANVAS_API_URL ^(see env.template^).
    exit /b 1
)

if exist ".venv\Scripts\canvas-mcp-server.exe" (
    ".venv\Scripts\canvas-mcp-server.exe" %*
) else (
    where canvas-mcp-server >nul 2>nul
    if errorlevel 1 (
        echo canvas-mcp-server is not installed. Run: uv sync
        exit /b 1
    )
    canvas-mcp-server %*
)
