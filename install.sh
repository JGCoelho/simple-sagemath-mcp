#!/usr/bin/env bash
# install.sh -- point an MCP client at this server.  No Pi required: it finds a
# Python that can `import sage.all`, writes a "sage" entry into an mcp.json next
# to the target directory when one exists, and otherwise prints the JSON to paste
# into whatever configuration the client uses.
#
#   ./install.sh                             register this folder in place
#   ./install.sh /some/other/dir             copy the tree there and register that
#   PI_AGENT_DIR=/tmp/fake ./install.sh      register into another agent dir
#   SAGE_PYTHON=/path/to/python ./install.sh use a specific interpreter
#
# Requirements: Linux, bash, and SageMath (a `sage` on PATH, or SAGE_PYTHON).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PI_AGENT_DIR="${PI_AGENT_DIR:-$HOME/.pi/agent}"
DST="${1:-$PI_AGENT_DIR/sage-mcp}"
MCP_JSON="$(dirname "$DST")/mcp.json"

# --------------------------------------------------------------------------
# the interpreter: the first candidate that can actually import sage.all
# --------------------------------------------------------------------------
candidates=()
[ -n "${SAGE_PYTHON:-}" ] && candidates+=("$SAGE_PYTHON")
[ -n "${PYTHON:-}" ] && candidates+=("$PYTHON")
if command -v sage >/dev/null 2>&1; then
  candidates+=("$(dirname "$(command -v sage)")/python")
fi
command -v python3 >/dev/null 2>&1 && candidates+=("$(command -v python3)")
command -v python  >/dev/null 2>&1 && candidates+=("$(command -v python)")

PY=""
for candidate in ${candidates+"${candidates[@]}"}; do
  [ -x "$candidate" ] || continue
  if PATH="$(dirname "$candidate"):$PATH" "$candidate" -c 'import sage.all' \
       >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "error: no interpreter here can 'import sage.all'." >&2
  echo "       Install SageMath, or point SAGE_PYTHON at Sage's python, e.g." >&2
  echo "       SAGE_PYTHON=\$(sage -c 'print(__import__(\"sys\").executable)') ./install.sh" >&2
  exit 1
fi
PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"

# sage needs its own bin directory on PATH, or `import sage.all` dies with
# FeatureNotPresentError: singular is not available.
export PATH="$(dirname "$PY"):$PATH"

echo "source : $SRC"
echo "target : $DST"
echo "python : $PY"
echo

mkdir -p "$DST/tests"
if [ "$SRC" != "$DST" ]; then
  install -m 0644 "$SRC/sage_mcp.py" "$SRC/sage_client.py" "$SRC/sage_kernel.py" \
      "$SRC/runfile.py" "$SRC/nbrun.py" "$DST/"
  install -m 0644 "$SRC/tests/acceptance.py" "$SRC/tests/interrupt_ownership.sage" "$DST/tests/"
  install -m 0644 "$SRC/README.md" "$SRC/agents-section.md" "$DST/" 2>/dev/null || true
  [ -f "$SRC/example.mcp.json" ] && install -m 0644 "$SRC/example.mcp.json" "$DST/"
fi
chmod 0755 "$DST/sage_mcp.py" "$DST/sage_kernel.py"

# leftovers from older layouts: `pdf` is its own server now, and the first
# `run_file` was an artifact store
for stale in artifacts.py pdftext.py server.py probe.txt; do
  if [ -e "$DST/$stale" ]; then
    rm -f "$DST/$stale"
    echo "removed stale $stale"
  fi
done
rm -rf "$DST/__pycache__" "$DST/tests/__pycache__"

# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------
CFG="$(
  "$PY" - "$DST/sage_mcp.py" "$PY" <<'EOF'
import json
import sys

script, python = sys.argv[1], sys.argv[2]
print(json.dumps(
    {
        "command": python,
        "args": [script],
        "env": {"SAGE_CWD": "/tmp"},
    },
    indent=2,
))
EOF
)"

if [ -f "$MCP_JSON" ]; then
  "$PY" - "$MCP_JSON" "$DST/sage_mcp.py" "$PY" <<'EOF'
import json
import shutil
import sys

path, script, python = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    data = json.load(fh)
shutil.copy2(path, path + ".bak")
data.setdefault("mcpServers", {})["sage"] = {
    "command": python,
    "args": [script],
    "env": {"SAGE_CWD": "/tmp"},
    "exposure": "direct",
}
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print("patched %s" % path)
EOF
else
  echo "No $MCP_JSON here.  Add this to your MCP client's config as \"sage\":"
  echo "$CFG"
fi

if [ -f "$DST/agents-section.md" ] && [ -f "$PI_AGENT_DIR/AGENTS.md" ]; then
  echo
  echo "Replace the '## Sage (MCP: \`sage\`)' section of $PI_AGENT_DIR/AGENTS.md with:"
  echo "------------------------------------------------------------"
  cat "$DST/agents-section.md"
  echo "------------------------------------------------------------"
fi

echo
echo "done. Restart the client (in Pi: restart or reload MCP servers):"
echo "  exec poll reset drop status interrupt kill run_file notebook"
echo
echo "Verify with:"
echo "  cd /tmp && $PY $DST/tests/acceptance.py"
echo "  cd /tmp && $(dirname "$PY")/sage $DST/tests/interrupt_ownership.sage"
