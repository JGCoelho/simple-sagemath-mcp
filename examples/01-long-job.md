# 01 — a job outlives its `exec`

`exec`'s `timeout_s` bounds **how long the call waits**, not how long the work
runs. When the wait ends you get the output so far and a job id; `poll` collects
what came after, from an offset, so nothing is re-run and nothing is repeated.

```python
# exec(session="examples", timeout_s=3, code=...)
import time
t0 = time.time()
for i in range(12):
    print("tick %2d  %4.1fs" % (i, time.time() - t0))
    time.sleep(1)
```

```
tick  0   0.0s
tick  1   1.0s
tick  2   2.0s
tick  3   3.0s
[still running: 3.0s]
[job_id j2 — call poll with job_id='j2', since=60]
```

`since=60` is where the text above ends: the next call asks only for what is new.

```python
# poll(job_id="j2", since=60, wait_s=20)
```

```
tick  4   4.0s
...
tick 11  11.0s
[job finished in 12.0s]
```

Two things worth noticing:

- `wait_s=20` blocked until the job ended, instead of polling in a loop. With no
  `wait_s` the same call would have answered `[still running 9s; …]` immediately.
- That wait is safe even though it outlives a client's own request timeout: the
  server reports progress every 10 s while it waits, and a progress notification
  resets the client's timeout. Nothing needs to be fire-and-forget unless the job
  is longer than the timeout you configured.
- The agent's turn can end here and the job keeps going; a later `poll`, from this
  server or the next one that adopts the kernel, still collects it from the
  offset it was given. A `poll` with `since=0` after that returns the whole
  output again -- a job is a buffer, not a one-shot reply, and the kernel keeps
  its last 200 finished jobs.
