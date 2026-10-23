r"""
tests/interrupt_ownership.sage -- "you may only interrupt your own job".

Two MCP servers (a = owner, b = stranger) share one kernel.  b must not be able
to SIGINT a's job, and a must still be able to stop its own runaway job.

    cd <this directory> && sage tests/interrupt_ownership.sage

It uses a throw-away state directory, so it never touches the kernel of a live
Pi session, and it exits 0 only if every check passes.  The layer above this --
the MCP tool turning a refusal into `isError` -- is covered by
tests/acceptance.py.
"""
import os
import shutil
import sys
import time

def package_dir():
    """Directory holding sage_client.py.

    Sage runs a .sage file inside the ``sage.all`` namespace, where ``__file__``
    is *its own* all.py -- so the script's own path has to come from argv.
    """
    env = os.environ.get("SAGE_MCP_HOME")
    if env and os.path.exists(os.path.join(env, "sage_client.py")):
        return env
    for base in (sys.argv[0], os.getcwd()):
        path = os.path.abspath(base)
        for candidate in (os.path.dirname(path), os.path.dirname(os.path.dirname(path))):
            if os.path.exists(os.path.join(candidate, "sage_client.py")):
                return candidate
    return os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0])))


sys.path.insert(0, package_dir())
import sage_client as sc  # noqa: E402  (needs package_dir() on the path)

STATE = "/tmp/sage-mcp-interrupt-ownership-%d" % os.getpid()
DEADLINE = int(2 ^ 6)  # int(64) seconds: the preparser rewrites 2^6 for us
SHORT_JOB = "import time\ntime.sleep(6)\nsurvived = 'yes'\n"
RUNAWAY = "import time\ntime.sleep(120)\nnever_reached = True\n"
checks = []


def pause(seconds):
    """The preparser turns ``0.05`` into a RealLiteral; ``time.sleep`` wants a float."""
    time.sleep(float(seconds))


def check(name, ok, detail=""):
    checks.append(bool(ok))
    print(("  PASS  " if ok else "  FAIL  ") + name)
    if detail:
        print("        " + str(detail).strip().replace("\n", "\n        "))


def submit(client, code, session="own"):
    reply = client.op(
        "submit", code=code, session=session, preparse=False, _timeout=DEADLINE
    )
    return reply["job_id"]


def wait_job(client, job_id, timeout=DEADLINE):
    reply = client.op(
        "wait", job_id=job_id, timeout=timeout, _timeout=timeout + 15
    )
    return reply["job"]


def wait_running(client, timeout=DEADLINE):
    """Wait until the kernel publishes a running job (or give up)."""
    end = time.time() + timeout
    while time.time() < end:
        info = client.running_job(wait=0)
        if info is not None:
            return info
        pause(0.05)
    return None


def main():
    owner = sc.Client(state_dir=STATE)  # "a": submits the jobs
    stranger = sc.Client(state_dir=STATE)  # "b": a different MCP server
    try:
        owner.ensure()
        state = owner.read_state()
        hello = owner.op("hello")
        print(
            "kernel pid %d, sage %s, python %s"
            % (state["pid"], hello["sage_version"], hello["python"])
        )
        print(
            "owner client %s..., stranger client %s...\n"
            % (owner.client_id[:8], stranger.client_id[:8])
        )

        # -- 1. a job is attributed to the client that submitted it ---------
        job = submit(owner, SHORT_JOB)
        running = wait_running(owner)
        check(
            "the kernel publishes the running job and its owner",
            running is not None
            and running.get("job") == job
            and running.get("owner") == owner.client_id,
            running,
        )

        # -- 2. a stranger's interrupt is refused ---------------------------
        pid, message, refused = stranger.interrupt()
        check(
            "another client's interrupt is refused (no signal sent)",
            refused and pid is None and "not yours" in message,
            message,
        )

        # -- 3. and the job lived through it --------------------------------
        info = wait_job(owner, job)
        check(
            "the refused interrupt left the job running to completion",
            info["state"] == "done"
            and not info["error"]
            and info["duration_s"] >= 5.5,
            "state=%s duration=%.1fs error=%r"
            % (info["state"], info["duration_s"], info["error"]),
        )
        check(
            "the job's session kept its result",
            "'yes'" in (wait_job(owner, submit(owner, "survived"))["repr"] or ""),
            "",
        )

        # -- 4. the owner may still stop its own runaway job ----------------
        job = submit(owner, RUNAWAY)
        check(
            "the runaway job is running",
            (wait_running(owner) or {}).get("job") == job,
            "",
        )
        pid, message, refused = owner.interrupt()
        check(
            "the owner can interrupt its own job",
            (not refused) and pid == state["pid"] and message is None,
            "pid=%r message=%r" % (pid, message),
        )
        info = wait_job(owner, job)
        check(
            "the interrupted job ends in KeyboardInterrupt",
            "KeyboardInterrupt" in (info["error"] or ""),
            info["error"],
        )

        # -- 5. housekeeping ------------------------------------------------
        check(
            "the running-job file is removed when the job ends",
            owner.running_job(wait=0) is None,
            "",
        )
        snapshot = owner.op("status")
        check(
            "the kernel is alive and idle afterwards",
            snapshot["jobs"]["running"] == [] and snapshot["jobs"]["queued"] == [],
            snapshot["jobs"],
        )
        pid, message, refused = owner.interrupt()
        check(
            "an interrupt while idle sends nothing",
            pid is None and "nothing to interrupt" in message,
            message,
        )
    finally:
        for client in (owner, stranger):
            try:
                client.kill()
            except Exception:
                pass
        shutil.rmtree(STATE, ignore_errors=True)

    failed = checks.count(False)
    print("\n%d checks, %d failed" % (len(checks), failed))
    print("ALL PASS" if not failed else "FAILED")
    return failed


# A .sage script is exec'd inside the sage.all namespace (__name__ is
# 'sage.all', not '__main__'), so there is no __main__ guard: run it here.
# Sage's runner turns any SystemExit into exit status 1, so that is the failure
# signal; a clean return leaves the status at 0.
if main():
    raise SystemExit(1)
