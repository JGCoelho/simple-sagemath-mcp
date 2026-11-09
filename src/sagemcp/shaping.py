"""Shape a call's outputs into the short text view the tools return. Pure."""


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
