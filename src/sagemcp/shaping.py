"""Shape a call's outputs into the short text view the tools return. Pure."""

from . import constants


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
    the budget. Never touches the source of the text (a file, a notebook).
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

