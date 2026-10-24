# Examples

Each file is a short transcript of one behaviour, with the real output. They are
about the *mechanics* -- what a timeout means, what survives an interrupt, where
sessions live -- not about mathematics.

| file | shows |
| --- | --- |
| [01-long-job.md](01-long-job.md) | `exec`'s timeout cancels nothing: job id, then incremental `poll` |
| [02-interrupt.md](02-interrupt.md) | `interrupt` fails *one job* with `KeyboardInterrupt`; the session keeps its variables |
| [03-run-file.md](03-run-file.md) | `run_file` runs an existing script in Sage's interpreter, in its own directory, output inline |
| [04-sessions.md](04-sessions.md) | sessions are separate namespaces in one kernel |
| [05-notebook.md](05-notebook.md) | `notebook` check / extract / run |

The two payloads those transcripts use are here too: `spanning_trees.py` (a Sage
script) and `curve.ipynb` (a two-cell notebook). To follow along, install the
server (see the top-level `README.md`) and give your client the `sage` entry it
prints; the transcripts then read the same in any MCP client.
