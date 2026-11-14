#!/bin/bash

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
echo "Script directory: $SCRIPT_DIR" >&2
echo "Starting Canvas MCP Server..." >&2

# Load environment variables from .env file
ENV_FILE="$SCRIPT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    echo "Loading environment variables from .env file: $ENV_FILE" >&2
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
else
    echo "Error: .env file not found at $ENV_FILE. Create one with CANVAS_API_URL (see env.template)." >&2
    exit 1
fi

if [ -z "$CANVAS_API_URL" ]; then
    echo "Error: CANVAS_API_URL must be set in .env file" >&2
    exit 1
fi

# Authentication is a Canvas browser session captured from Chrome. No token is
# required; the first Canvas tool call opens the login window if needed.
cd "$SCRIPT_DIR" || exit 1
echo "Changed directory to: $(pwd)" >&2

# Default transport is streamable HTTP at http://127.0.0.1:8819/mcp.
VENV_SERVER="$SCRIPT_DIR/.venv/bin/canvas-mcp-server"
if [ -x "$VENV_SERVER" ]; then
    echo "Starting server with $VENV_SERVER ..." >&2
    "$VENV_SERVER" "$@"
else
    echo "Starting server with canvas-mcp-server from PATH..." >&2
    canvas-mcp-server "$@"
fi

echo "Server stopped" >&2
