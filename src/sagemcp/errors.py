"""The server's exception types and the one shared "no Sage" message.

Nothing here imports anything: every other module may import this one.
"""

SAGE_NOT_FOUND = ("SageMath not found. Install it, or set SAGE_BIN to the sage "
                  "executable.")


class SageNotFound(Exception):
    """No Sage installation, or no python that can run its kernel, was found."""


class SourceError(Exception):
    """The source is not valid Sage code (preparse, parse, or compile failed)."""


class ExecTimeout(Exception):
    """The per-call budget expired. The kernel may still be computing."""

    def __init__(self, seconds: float):
        super().__init__(f"the call exceeded {seconds:g} s")


class KernelDied(Exception):
    """The kernel died, so the namespace is gone."""


class KernelBusy(Exception):
    """Another call holds the kernel, so this one is refused, not queued.

    `job_id` names the running job, or is None when the holder is not a job
    (a restart).
    """

    def __init__(self, job_id: str | None):
        super().__init__("the kernel is busy with another call")
        self.job_id = job_id


class GapCrashLoop(Exception):
    """GAP crashed `GAP_CRASH_LIMIT` times in one call; the call was interrupted."""

    def __init__(self, outputs: list[dict]):
        super().__init__("GAP crashed repeatedly")
        self.outputs = outputs
        self.stderr = "".join(
            o["text"] for o in outputs
            if o["output_type"] == "stream" and o.get("name") == "stderr")

