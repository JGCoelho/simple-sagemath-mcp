#!/usr/bin/env bash
# Install this MCP server into the Python of a SageMath that is already on the machine.
#
#   ./install.sh                        find Sage, install the dependencies, print the config
#   SAGE_BIN=/path/to/sage ./install.sh
#
# The script changes no config file. It prints the block to paste.
#
# Simplified Technical English: short sentences, one instruction per line.

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Test a python candidate. Put its own bin directory first in PATH, because Sage
# needs PATH to find Singular and other programs.
works() { PATH="$(dirname "$1"):$PATH" "$1" -c "$2" >/dev/null 2>&1; }

# Find the sage executable. SAGE_BIN wins. Look in the usual places only when
# PATH has no sage.
find_sage() {
    if [ -n "${SAGE_BIN:-}" ]; then
        echo "$SAGE_BIN"
        return
    fi
    if command -v sage >/dev/null 2>&1; then
        command -v sage
        return
    fi
    local p
    for p in "$HOME"/miniconda3/envs/*/bin/sage "$HOME"/anaconda3/envs/*/bin/sage \
             /opt/miniconda3/envs/*/bin/sage /opt/conda/envs/*/bin/sage \
             /opt/sage*/sage /opt/sage*/local/bin/sage; do
        if [ -x "$p" ]; then
            echo "$p"
            return
        fi
    done
    return 1
}

# The server runs on the python of the sage executable: that python has both
# fastmcp and the Sage preparser. Prefer the sibling python3/python, else ask
# sage for sys.executable.
find_python() {
    local bindir name
    bindir=$(cd "$(dirname "$1")" && pwd)
    for name in python3 python; do
        if [ -x "$bindir/$name" ]; then
            echo "$bindir/$name"
            return
        fi
    done
    PATH="$bindir:$PATH" "$1" -c 'import sys; print(sys.executable)'
}

if ! SAGE=$(find_sage); then
    echo "SageMath not found."
    echo "Install Sage first, or name its executable:"
    echo "  SAGE_BIN=/path/to/sage $0"
    exit 1
fi
SAGE=$(cd "$(dirname "$SAGE")" && pwd)/$(basename "$SAGE")
echo "Sage: $SAGE"

if ! PY=$(find_python "$SAGE"); then
    echo "Error: cannot find the python of this Sage: $SAGE"
    exit 1
fi
echo "Server and kernel python: $PY"

if ! works "$PY" 'import sage.all'; then
    echo "Error: this python cannot import sage.all: $PY"
    exit 1
fi

# The server needs fastmcp and nbformat. ipykernel and jupyter_client come with Sage.
if ! works "$PY" 'import fastmcp, nbformat'; then
    echo "Installing fastmcp and nbformat ..."
    "$PY" -m pip install --quiet --disable-pip-version-check fastmcp nbformat
fi

for mod in fastmcp nbformat jupyter_client ipykernel sage.all; do
    if ! works "$PY" "import $mod"; then
        echo "Error: cannot import $mod in $PY"
        exit 1
    fi
done

# A host that hides PATH cannot find sage on its own. Name it in the config only
# in that case.
if command -v sage >/dev/null 2>&1; then
    ENV_LINE=
else
    ENV_LINE=",
    \"env\": { \"SAGE_BIN\": \"$SAGE\" }"
fi

cat <<EOF

Ready. Add this block to the MCP config of your host, then restart the host:

  "sage": {
    "command": "$PY",
    "args": ["$HERE/server.py"]$ENV_LINE
  }

For pi, the file is ~/.pi/agent/mcp.json. Put the block inside "mcpServers".
Other hosts use ~/.claude.json, .mcp.json, or a similar file.

Add "--directory", "/path/to/your/work" to "args" to start the kernel in that
directory. The server starts one Sage kernel on the first call and keeps it.
EOF
