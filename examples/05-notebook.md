# 05 — notebooks, in Sage's interpreter

`notebook` validates, lists, strips and executes `.ipynb` files. All four actions
run in **Sage's** interpreter (the kernel's), and the tool is only advertised when
that interpreter has `nbformat`+`nbconvert`+`nbclient` -- SageMath ships them.
The server process itself never imports them, so a plain-Python server install
still has a working `notebook`.

The payload is `examples/curve.ipynb` (one markdown cell, two code cells):

```python
# notebook(action="check", path="/…/examples/curve.ipynb")
```

```
OK   /home/jgcoelho/.pi/agent/sage-mcp/examples/curve.ipynb v4.5 kernel=sagemath cells=3 (code=2, with-output=0)
```

```python
# notebook(action="extract", path="/…/examples/curve.ipynb")
```

```
# extracted from /home/jgcoelho/.pi/agent/sage-mcp/examples/curve.ipynb (2 code cells)

# --- cell 1 (code) ---
R.<x> = QQ[]
f = x^3 - 2*x + 1
print(f.factor())

# --- cell 2 (code) ---
E = EllipticCurve([0, -1, 1, -10, -20])
(E.rank(), E.torsion_order(), E.conductor())
```

- `action="run"` executes with nbconvert in Sage's kernel, writes
  `<name>.out.ipynb` (or `out=…`, or `in_place=true`) atomically, and reports how
  many cells ended in error. `cell_timeout_s` bounds each cell, `timeout_s` the
  whole run; a timeout kills the process group.
- `action="clear"` strips outputs in place -- also atomically, so an interrupted
  clear cannot leave a half-written notebook.
- A notebook that does not parse is reported as a tool error naming the problem,
  not as a protocol failure; a cell that raises is reported by `run` as a cell
  error with the traceback, which is the useful outcome for a `run`, not a reason
  to fail the call.
