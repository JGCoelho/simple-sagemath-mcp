#!/usr/bin/env bash
# install.sh -- register this Sage MCP server with an MCP client (Pi included).
#
#   ./install.sh                          register this folder in place
#   ./install.sh /some/other/dir          copy the tree there and register that
#   ./install.sh --check                  re-test an existing registration
#   PI_AGENT_DIR=/tmp/fake ./install.sh   register into another agent dir
#   SAGE_PYTHON=/path/to/python ./install.sh   use a specific Sage interpreter
#
# Two interpreters (see DESIGN.md): the *server* needs `mcp` and any Python
# >= 3.10; the *kernel* needs Sage, which the server itself discovers at
# runtime ($SAGE_PYTHON -> `sage -c` -> the python beside `sage`).  This script
# finds both only to fail early, and bakes the discovered Sage interpreter into
# the registration as SAGE_PYTHON -- an MCP client often starts a server with a
# minimal PATH, and a stale SAGE_PYTHON degrades back to discovery.
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CHECK_ONLY=0
if [ "${1:-}" = "--check" ]; then
  CHECK_ONLY=1
  shift
fi
PI_AGENT_DIR="${PI_AGENT_DIR:-$HOME/.pi/agent}"
DST="${1:-$PI_AGENT_DIR/sage-mcp}"
MCP_JSON="$(dirname "$DST")/mcp.json"

die() {
  echo "error: $*" >&2
  exit 1
}

# 1. Sage's interpreter -- the same three routes as sage_client.resolve_python().
find_sage_python() {
  local sage_bin="" reported="" dir="" name=""
  if [ -n "${SAGE_PYTHON:-}" ] && [ -x "${SAGE_PYTHON}" ]; then
    printf '%s\n' "${SAGE_PYTHON}"
    return 0
  fi
  sage_bin="$(command -v sage || true)"
  [ -n "$sage_bin" ] || return 1
  reported="$("$sage_bin" -c 'import sys;print(sys.executable)' 2>/dev/null | tail -n 1 || true)"
  if [ -n "$reported" ] && [ -x "$reported" ]; then
    printf '%s\n' "$reported"
    return 0
  fi
  dir="$(dirname "$(readlink -f "$sage_bin")")"
  for name in python python3; do
    if [ -x "$dir/$name" ]; then
      printf '%s\n' "$dir/$name"
      return 0
    fi
  done
  return 1
}

# 2. The server's interpreter: one that can `import mcp`.
find_server_python() {
  local candidate=""
  for candidate in "${PYTHON:-}" "$(command -v python3 || true)" \
                   "$(command -v python || true)" "${1:-}"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if "$candidate" -c 'import mcp' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

SAGE_PY="$(find_sage_python || true)"
SPY="$(find_server_python "$SAGE_PY" || true)"

if [ "$CHECK_ONLY" = 1 ]; then
  CONFIG="${1:-$MCP_JSON}"
  NAME="${2:-sage}"
  [ -f "$CONFIG" ] || die "nothing to check: $CONFIG does not exist.
       Run ./install.sh first, or pass the config: ./install.sh --check <mcp.json>"
  # Re-testing a registration means starting what the client would start: the
  # entry's own `command`, not whatever python this shell happens to have. A
  # bare shell usually has no Sage on PATH, and a python3 without `mcp` turns
  # the check into a ModuleNotFoundError traceback instead of a verdict.
  json_py="$(command -v python3 || true)"
  registered=""
  if [ -n "$json_py" ]; then
    registered="$("$json_py" -c 'import json,sys;d=json.load(open(sys.argv[1]));e=(d.get("mcpServers") or {}).get(sys.argv[2]) or {};sys.stdout.write(e.get("command",""))' "$CONFIG" "$NAME" 2>/dev/null || true)"
  fi
  if [ -n "$registered" ] && [ -x "$registered" ] && "$registered" -c 'import mcp' 2>/dev/null; then
    SPY="$registered"
  fi
  [ -n "$SPY" ] || die "no Python here can 'import mcp' (the MCP SDK), and the
       command in $CONFIG could not be used to run the check.
       Install it:  python3 -m pip install 'mcp>=2.3,<3'   or set SAGE_PYTHON."
  echo "config : $CONFIG"
  echo "checker: $SPY"
  [ -n "$SAGE_PY" ] && export SAGE_PYTHON="$SAGE_PY"
  exec "$SPY" "$SRC/sage_mcp.py" --check --config "$CONFIG" ${2:+--name "$2"}
fi

[ -n "$SAGE_PY" ] || die "no interpreter that can 'import sage.all' was found.
       Install SageMath, or set SAGE_PYTHON to Sage's python:
       SAGE_PYTHON=\$(sage -c 'print(__import__(\"sys\").executable)') ./install.sh"
[ -n "$SPY" ] || die "no Python here can 'import mcp' (the MCP SDK).
       Install it for one of them:  python3 -m pip install 'mcp>=2.3,<3'
       ...or point PYTHON at an interpreter that already has it."
SAGE_PY="$(readlink -f "$SAGE_PY")"
SPY="$(readlink -f "$SPY")"

echo "source : $SRC"
echo "target : $DST"
echo "server : $SPY  (imports mcp)"
echo "sage   : $SAGE_PY  (runs the kernel)"

if [ "$SRC" != "$DST" ]; then
  mkdir -p "$DST"
  install -m 0644 "$SRC/sage_mcp.py" "$SRC/sage_client.py" "$SRC/sage_kernel.py" \
    "$SRC/subproc.py" "$DST/"
  chmod 0755 "$DST/sage_mcp.py" "$DST/sage_kernel.py"
fi

mkdir -p "$(dirname "$MCP_JSON")"
"$SPY" - "$MCP_JSON" "$DST/sage_mcp.py" "$SPY" "$SAGE_PY" <<'EOF'
import json
import os
import shutil
import sys

path, script, python, sage = sys.argv[1:5]
entry = {
    "command": python,
    "args": [script],
    "env": {"SAGE_CWD": "/tmp", "SAGE_PYTHON": sage},
    "exposure": "direct",
}
if os.path.exists(path):
    with open(path) as fh:
        data = json.load(fh)
    shutil.copy2(path, path + ".bak")
else:
    data = {}
data.setdefault("mcpServers", {})["sage"] = entry
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print("registered the %r entry in %s" % ("sage", path))
EOF

echo
echo "verifying the registration (a throwaway kernel, never the live one):"
SAGE_PYTHON="$SAGE_PY" "$SPY" "$SRC/sage_mcp.py" --check --config "$MCP_JSON"
echo
echo "done. Restart the client (in Pi: reload MCP servers) to pick it up."
