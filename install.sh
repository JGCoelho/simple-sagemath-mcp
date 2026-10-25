#!/usr/bin/env bash
# Install this MCP server into the Python of a SageMath that is already on the machine.
#
#   ./install.sh                          find Sage, install the dependencies, print the config
#   SAGE_PYTHON=/path/to/python ./install.sh
#
# The script changes no config file. It prints the block to paste.
#
# Simplified Technical English: short sentences, one instruction per line.

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Test a python candidate. Put its own bin directory first in PATH, because Sage
# needs PATH to find Singular and other programs.
works() { PATH="$(dirname "$1"):$PATH" "$1" -c "$2" >/dev/null 2>&1; }

find_python() {
    if [ -n "${SAGE_PYTHON:-}" ]; then
        echo "$SAGE_PYTHON"
        return
    fi
    # A sage command in PATH knows its own python. Add the directory of that
    # command to PATH, because Sage needs it to find Singular.
    if command -v sage >/dev/null 2>&1; then
        local bindir
        bindir=$(cd "$(dirname "$(command -v sage)")" && pwd)
        PATH="$bindir:$PATH" sage -c 'import sys; print(sys.executable)'
        return
    fi
    # Look in the usual places of conda and of a source install.
    local p
    for p in "$HOME"/miniconda3/envs/*/bin/python "$HOME"/anaconda3/envs/*/bin/python \
             /opt/miniconda3/envs/*/bin/python /opt/conda/envs/*/bin/python \
             /opt/sage*/local/bin/python3; do
        if [ -x "$p" ] && works "$p" 'import sage.all'; then
            echo "$p"
            return
        fi
    done
    # Last try: a python in PATH that already has Sage.
    for p in python3 python; do
        if command -v "$p" >/dev/null 2>&1 && works "$p" 'import sage.all'; then
            command -v "$p"
            return
        fi
    done
    return 1
}

if ! PY=$(find_python); then
    echo "SageMath not found."
    echo "Install Sage first, or give its python:"
    echo "  SAGE_PYTHON=/path/to/sage/python $0"
    exit 1
fi
echo "Sage python: $PY"

if ! works "$PY" 'import sage.all'; then
    echo "Error: this python cannot import sage.all: $PY"
    exit 1
fi

# The server needs fastmcp and nbformat. ipykernel and jupyter_client come with Sage.
if ! works "$PY" 'import fastmcp, nbformat'; then
    echo "Installing fastmcp and nbformat ..."
    "$PY" -m pip install --quiet --disable-pip-version-check fastmcp nbformat
fi

for mod in fastmcp nbformat jupyter_client ipykernel; do
    if ! works "$PY" "import $mod"; then
        echo "Error: cannot import $mod in $PY"
        echo "Try: $PY -m pip install fastmcp nbformat ipykernel"
        exit 1
    fi
done

cat <<EOF

Ready. Add this block to the MCP config of your host, then restart the host:

  "sage": {
    "command": "$PY",
    "args": ["$HERE/server.py"]
  }

For pi, the file is ~/.pi/agent/mcp.json. Put the block inside "mcpServers".
Other hosts use ~/.claude.json, .mcp.json, or a similar file.

The server starts one Sage kernel on the first call. To run the server itself on
a different python, add the same python as SAGE_PYTHON in the environment of the
server:

  "env": { "SAGE_PYTHON": "$PY" }
EOF
