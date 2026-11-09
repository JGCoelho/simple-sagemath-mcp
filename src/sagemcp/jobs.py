"""Job records for `exec` and `run_file`: one call that runs in the kernel.

A job is started by `kernel._start_job`, which holds the kernel lock while the
job runs. This module only keeps the record: the id, the timing, the outputs as
they arrive, and the final envelope. It takes no lock and touches no kernel.

The registry lives in the lifespan state: `state["jobs"]` maps job id to `Job`
(insertion order, oldest first) and `state["active"]` is the job that holds the
kernel, or None. Records are kept in memory only; a server restart drops them.
"""

import time
import uuid
from dataclasses import dataclass, field

from . import constants


# --- job records -------------------------------------------------------------

@dataclass
class Job:
    """One call that runs in the kernel. The fields after `job_id` are mutable.

    `outputs` is the list `kernel._run_source` appends to as iopub messages
    arrive, so `job_status` can read the output of a running job. `result` is
    the envelope `exec` returns, set once the job finishes. `status` is
    `running` until then; a restart sets `interrupted` first.
    """

    job_id: str
    started: float = field(default_factory=time.monotonic)
    outputs: list = field(default_factory=list)
    status: str = "running"          # running | done | error | interrupted
    result: dict | None = None       # the exec envelope, once finished
    finished: float | None = None    # monotonic end time; None while running
    task: object = None              # the asyncio task that runs the job

    def elapsed(self) -> float:
        """Seconds from start to finish, or to now while the job runs."""
        end = time.monotonic() if self.finished is None else self.finished
        return round(end - self.started, 3)


def new_job() -> Job:
    """A fresh running job with a short random id. Not registered yet."""
    return Job(job_id=uuid.uuid4().hex[:12])


def remember(state: dict, job: Job) -> None:
    """Register a job, then drop the oldest finished records over the bound."""
    state["jobs"][job.job_id] = job
    trim(state)


def trim(state: dict) -> None:
    """Drop the oldest finished records until the bound holds.

    The running job has no `finished` time and is never dropped, so the bound
    can be exceeded by one while a job runs. Never raises.
    """
    jobs = state["jobs"]
    for job_id in list(jobs):
        if len(jobs) <= constants.JOB_RETENTION:
            break
        if jobs[job_id].finished is not None:
            del jobs[job_id]


def find(state: dict, job_id: str) -> Job | None:
    """The record for `job_id`, or None if it is unknown or was dropped."""
    return state["jobs"].get(job_id)
