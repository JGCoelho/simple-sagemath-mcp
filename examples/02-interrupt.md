# 02 — `interrupt` stops one job, not the session

`interrupt` sends `SIGINT`: the running job ends as a `KeyboardInterrupt`, the
result says so, and **the session keeps its variables**. That is the difference
between a cancelled computation and a kernel you have to rebuild by hand.

```python
# exec(session="examples", timeout_s=0, code=...)
import time
marker = "kept across the interrupt"
i = 0
while True:
    i += 1
    print("round %d" % i)
    time.sleep(2)
```

```
[job_id j3 — call poll with job_id='j3', since=0]
```

```python
# poll(job_id="j3", since=0, wait_s=3)
```

```
round 1
round 2
round 3
[still running 5.8s; poll again with since=24]
```

```python
# interrupt()
```

```
SIGINT sent to kernel pid 211705
if a job was running it became that job's KeyboardInterrupt and the session kept its
variables (poll to collect the error); if the kernel was idle the signal was ignored,
because SIGINT cannot kill the kernel.
```

```python
# poll(job_id="j3", since=24, wait_s=10)
```

```
round 4
KeyboardInterrupt: interrupted by SIGINT (the session and its variables are preserved)
[job finished in 7.7s]
```

The namespace is intact:

```python
# exec(session="examples", code="marker, i")
```

```
('kept across the interrupt', 4)
```

Notes:

- `interrupt` only reaches **your** job. Another client's running job is refused
  (`not yours`), and an idle kernel ignores it -- `SIGINT` can never kill the
  kernel.
- When a `SIGINT` genuinely cannot stop the computation (a wedge at C level),
  the escape hatch is `interrupt(force=true)`: it stops the *kernel*, every
  session loses its variables, and the next `exec` tells you
  `[kernel restarted: …]`.
- The GAP crash-loop guard uses exactly this signal, one trip per job, so a cell
  stuck in GAP's crash-restart loop also ends as a collectable `KeyboardInterrupt`
  instead of hanging forever.
