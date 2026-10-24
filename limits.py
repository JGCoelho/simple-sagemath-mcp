"""limits.py -- SAGE_MCP_LIMITS: the kernel's resource limits (PLAN §7).

    SAGE_MCP_LIMITS="cpu=3600,as=8G,fsize=1G,nproc=off"

Unset keys keep their default (cpu off, as 8 GiB, fsize 1 GiB, nproc off);
`0`/`off` disables a limit and leaves the inherited one alone.  Only soft limits
are lowered, `hard = min(soft + slack, inherited hard)`.  `as`/`fsize`/core are
applied before `import sage.all` (that import peaks near 4 GiB); `cpu` after it
(RLIMIT_CPU counts from exec) and `nproc` before each job (the per-UID count
drifts).  README "Limits" says what each one means and how it fails.
"""

import os
import resource

ENV = "SAGE_MCP_LIMITS"
KINDS = ("cpu", "as", "fsize", "nproc")
DEFAULTS = {"cpu": 0, "as": 8 * 1024**3, "fsize": 1024**3, "nproc": 0}
RESOURCES = {
    "cpu": resource.RLIMIT_CPU,
    "as": resource.RLIMIT_AS,
    "fsize": resource.RLIMIT_FSIZE,
    "nproc": resource.RLIMIT_NPROC,
}
UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
OFF_WORDS = ("", "0", "off", "none", "unlimited")
SLACK = {"cpu": 5, "nproc": 200}


class LimitError(ValueError):
    """SAGE_MCP_LIMITS could not be parsed or applied."""


def _number(raw, key):
    text = str(raw).strip().lower()
    if text in OFF_WORDS:
        return 0
    digits = text[:-1] if text.endswith("b") else text  # "8gb" == "8g"
    unit = digits[-1:].upper() if digits[-1:].upper() in UNITS else ""
    digits = digits[: -len(unit)] if unit else digits
    if not digits.isdigit():
        raise LimitError(
            "%s=%r is not a number: use an integer, optionally with a K/M/G "
            "suffix (e.g. as=8G, fsize=1G, cpu=3600, nproc=off)" % (key, raw)
        )
    return int(digits) * UNITS[unit]


def parse(spec=None):
    """The limits a process should apply, from `spec` or $SAGE_MCP_LIMITS."""
    values = dict(DEFAULTS)
    raw = os.environ.get(ENV) if spec is None else spec
    if raw is None or not str(raw).strip():
        return values
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        key = key.strip().lower()
        if not sep:
            raise LimitError(
                "%r is not key=value: expected e.g. cpu=3600,as=8G,fsize=1G,nproc=off"
                % (item,)
            )
        if key not in KINDS:
            raise LimitError("unknown limit %r (known: %s)" % (key, ", ".join(KINDS)))
        values[key] = _number(value, key)
    return values


def _cpu_seconds():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def apply(values, kinds=KINDS):
    """Lower the soft limits; return {name: applied soft or None} (None = off).

    A hard limit below the configured soft one is honoured by clamping, not by
    failing the boot: we never raise a limit, only lower it.
    """
    applied = {}
    for name in kinds:
        soft = int(values.get(name) or 0)
        if soft <= 0:
            applied[name] = None
            continue
        if name == "cpu":
            soft += int(_cpu_seconds()) + 1  # the budget starts now
        _old_soft, old_hard = resource.getrlimit(RESOURCES[name])
        hard = soft + SLACK.get(name, max(1, soft // 4))
        if old_hard != resource.RLIM_INFINITY:
            hard = min(hard, old_hard)
        soft = min(soft, hard)
        try:
            resource.setrlimit(RESOURCES[name], (soft, hard))
        except (ValueError, OSError) as exc:
            raise LimitError(
                "cannot apply %s=%s (soft %s, hard %s): %s"
                % (name, values.get(name), soft, hard, exc)
            )
        applied[name] = soft
    return applied


def uid_tasks():
    """How many tasks RLIMIT_NPROC counts for our real UID right now.

    Linux counts processes **and threads**, per UID and machine-wide -- not the
    kernel's children -- which is why `nproc=N` is a margin over this number.
    """
    uid = os.getuid()
    total = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            for line in open("/proc/%s/status" % pid):
                if line.startswith("Uid:") and int(line.split()[1]) == uid:
                    total += len(os.listdir("/proc/%s/task" % pid))
                    break
        except (OSError, ValueError):
            continue
    return total


_NPROC_HARD = []  # the hard limit we may raise up to (captured on first arm)


def arm_nproc(values):
    """RLIMIT_NPROC as `nproc=<margin>` tasks more than the UID runs right now.

    Re-armed before each job and each child spawn: the per-UID count drifts, so
    a number captured at boot would be stale (and Sage's import leaves threads
    behind for a moment).  Returns the absolute soft limit, or None when off.
    """
    margin = int(values.get("nproc") or 0)
    if margin <= 0:
        return None
    if not _NPROC_HARD:
        _NPROC_HARD.append(resource.getrlimit(resource.RLIMIT_NPROC)[1])
    cap = _NPROC_HARD[0]
    soft = uid_tasks() + margin
    hard = soft + max(1, soft // 4)
    if cap != resource.RLIM_INFINITY:
        hard = min(hard, cap)
    soft = min(soft, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (soft, hard))
    except (ValueError, OSError) as exc:
        raise LimitError(
            "cannot arm nproc=%d (soft %d, hard %d): %s" % (margin, soft, hard, exc)
        )
    return soft


def no_core():
    """RLIMIT_CORE=0, always: a Sage crash must not fill /tmp with a core."""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass


def human(name, values):
    """One limit as text (`2s`, `8G`, `off`), for messages and the status line."""
    value = int(values.get(name) or 0)
    if value <= 0:
        return "off"
    if name == "cpu":
        return "%ds" % value
    if name == "nproc":
        return str(value)
    for unit, factor in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if value % factor == 0:
            return "%d%s" % (value // factor, unit)
    return str(value)


def describe(values):
    """`cpu=off, as=8G, fsize=1G, nproc=off` for the status text and the log."""
    return ", ".join("%s=%s" % (name, human(name, values)) for name in KINDS)


def as_json(values, source="configured"):
    """The `limits` object of sage://kernel/status (bytes/seconds, None = off)."""
    out = {name: (int(values.get(name) or 0) or None) for name in KINDS}
    out["source"] = source  # "kernel": read from the kernel; "configured": ours
    return out
