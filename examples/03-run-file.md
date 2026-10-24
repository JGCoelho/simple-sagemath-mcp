# 03 — `run_file`: a file you wrote, run where it lives

`run_file` runs a file that already exists on disk. Nothing is copied, the
working directory is the script's own, and the exit code, the duration and both
streams come back in the result. It runs in **Sage's interpreter**, so
`from sage.all import *` works, but it is a *separate process* from the kernel --
it does not see your sessions, and it cannot block them.

`examples/spanning_trees.py` is the payload (a Sage script with a `--n` flag):

```python
# run_file(path="/…/examples/spanning_trees.py", args=["--n", "6"])
```

```
exit=0  spanning_trees.py  2.03s  python3.14 spanning_trees.py --n 6
cwd=/home/jgcoelho/.pi/agent/sage-mcp/examples  lang=python
--- stdout ---
connected graphs on 6 vertices: 112
spanning trees in total:          10183

--- stderr ---
arguments seen by the script:     ['--n', '6']
```

- `lang` defaults to `auto`: the `.py` extension plus the `#!/usr/bin/env python3`
  shebang, executed with Sage's interpreter. `lang="sage"` shells out to `sage`
  instead; `lang="bash"`/`"exec"` are there for the other things a file might be.
- `cwd` defaults to the script's directory, so `open("data.csv")` next to the
  script works. Pass `cwd=…` to override, `stdin=…` to feed input.
- A non-zero exit **is an error result** carrying the output: run the same file
  with `args=["--n", "6", "--fail"]` and the tool reports exit 3 plus
  `failing on request` on stderr. That is a tool-level error, not a protocol
  fault, so the model sees the file's own output.
- `timeout_s` (default 120) kills the whole process group -- a script that spawns
  children does not leave them behind.
- Output is capped (20 kB per stream) so a runaway `print` loop cannot flood the
  context; the result says `[... N more bytes ...]` when it was truncated.
