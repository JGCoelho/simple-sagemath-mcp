# 04 — sessions are namespaces, not processes

One kernel, many sessions. A session is a name for a dict of globals seeded from
`from sage.all import *`, so two sessions cannot see each other's variables, and
both persist between calls without a second Sage process or a second kernel.

```python
# exec(session="work", code="x = 41; print('x is', x)")
```

```
x is 41
```

```python
# exec(session="default", code='print("x" in globals())')
```

```
False
```

```python
# exec(session="work", code="x + 1")      # a later call, same namespace
```

```
42
```

`status` shows the namespace sizes, which is how you check what you left behind:

```
session default: 1873 names
session examples: 1878 names
session work: 1874 names
```

- A new session is created by naming it: there is no `start` call, and the first
  `exec` on a fresh kernel starts the kernel itself (`status` says
  `not running` until then).
- `reset(session="work")` empties that namespace back to the `sage.all` base and
  keeps the name; it does **not** wait for an idle session, so do not reset one
  whose job you still want. `drop(sessions=["work"])` frees the name and refuses
  a session with work in flight; `drop(all=true)` takes every idle one. Neither
  restarts the kernel or touches another session.
- Use distinct session names when parallel subagents must not collide -- two
  agents on `default` share one namespace by design.
