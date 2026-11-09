"""Shape a call's outputs and tracebacks into the text the tools return. Pure."""

import re

from . import constants


# --- output shaping ----------------------------------------------------------

def _summarise(outs: list[dict]) -> tuple[str, str, str | None, str | None]:
    """Pure: make the short text view of outputs.

    Return `(stdout, stderr, last trailing value or None, "Ename: evalue" or
    None)`. The trailing value is the `text/plain` of an `execute_result`, which
    only the wrapper's trailing-expression display produces; a `display(...)` in
    user code is a `display_data` and is never reported as the result. So a
    statement-only source has no result, even when it displays something. Pure.
    """
    def stream(name):
        return "".join(o["text"] for o in outs
                       if o["output_type"] == "stream" and o.get("name") == name)

    result = next((o["data"]["text/plain"] for o in reversed(outs)
                   if o.get("output_type") == "execute_result"
                   and "text/plain" in o.get("data", {})), None)
    error = next((f"{o['ename']}: {o['evalue']}"
                  for o in outs if o["output_type"] == "error"), None)
    return stream("stdout"), stream("stderr"), result, error


def _truncate(text: str, budget: int) -> tuple[str, bool]:
    """Cut `text` to `budget` characters and mark the cut. Pure.

    Returns `(text, cut)`; a cut text keeps the first `budget` characters and
    appends a marker naming how many were dropped. The marker is not counted in
    the budget. Never touches where the text came from.
    """
    if len(text) <= budget:
        return text, False
    if budget < 0:
        budget = 0
    cut = len(text) - budget
    return text[:budget] + f"\n… [truncated {cut} characters]", True


def _exec_view(outs: list[dict]) -> dict:
    """Shape a call's outputs into `{result, stdout, stderr, truncated}`. Pure.

    One MAX_INLINE_CHARS budget shared by `result`, then `stdout`, then
    `stderr`, in that order; a later field is cut when the earlier ones already
    used the budget. `truncated` is true iff anything was cut. The untruncated
    text is not kept anywhere.
    """
    stdout, stderr, result, _ = _summarise(outs)
    result = result or ""
    budget = constants.MAX_INLINE_CHARS
    result, cut_result = _truncate(result, budget)
    # The cut marker is not content: charge only the characters that were kept.
    budget = max(0, budget - (budget if cut_result else len(result)))
    stdout, cut_stdout = _truncate(stdout, budget)
    budget = max(0, budget - (budget if cut_stdout else len(stdout)))
    stderr, cut_stderr = _truncate(stderr, budget)
    return {"result": result, "stdout": stdout, "stderr": stderr,
            "truncated": cut_result or cut_stdout or cut_stderr}


# --- tracebacks --------------------------------------------------------------

# IPython colours its tracebacks, and the frames it shows for the generated
# wrapper (`Cell In[n], line k`) contain the server's source, not the user's.
# Both are noise in `error.details`, which an agent has to read as plain text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_WRAPPER_FRAME_MARKERS = ("_mcp_last", "_mcp_value", "_mcp_tree", "_mcp_ns",
                          "_mcp_seed", "_mcp_display", "__mcp_ns__")


def _clean_traceback(lines: list) -> str:
    """Return a traceback as plain text, without the wrapper's own frames.

    Drops the ANSI escapes and every traceback entry that mentions a name from
    the generated wrapper, keeping the exception header, the frames in the
    user's file and the library frames, and the final `Ename: evalue` line.
    Pure, and never raises.
    """
    kept: list[str] = []
    for line in lines:
        text = _ANSI_RE.sub("", str(line))
        if any(marker in text for marker in _WRAPPER_FRAME_MARKERS):
            continue
        kept.append(text.rstrip())
    # The header separator is decoration once the colours are gone.
    while kept and (not kept[0].strip() or set(kept[0].strip()) == {"-"}):
        kept.pop(0)
    return "\n".join(kept).strip()
