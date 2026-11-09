"""Preparse Sage source and build the code the kernel runs for one call.

Both functions are pure. The only Sage import in the tree is the lazy one
inside `_prepare`: it must stay inside the function, so importing the server
never imports Sage.
"""

import ast

from .errors import SAGE_NOT_FOUND, SageNotFound, SourceError


def _prepare(source: str) -> str:
    """Preparse and compile Sage source; return the preparsed text.

    This is the static check that execution and notebook validation share, so a
    source that passes here fails at the same place when it runs. Imports Sage
    lazily (never at module import) and raises `SourceError` with the exception
    text on a syntax error, `SageNotFound` if Sage cannot be imported.
    """
    try:
        from sage.repl.preparse import preparse
    except ImportError as e:
        raise SageNotFound(SAGE_NOT_FOUND) from e
    prepared = preparse(source)
    try:
        ast.parse(prepared)
        compile(prepared, "<sage>", "exec")
    except SyntaxError as e:
        detail = e.msg or "invalid syntax"
        if e.lineno is not None:
            detail = f"{detail} (line {e.lineno})"
        raise SourceError(detail) from None
    except UnicodeError as e:
        # A lone surrogate can come out of a JSON \uD800 escape and is not valid
        # source. It is a property of the source, so `invalid_source` is right.
        raise SourceError(f"the source is not valid Unicode ({e})") from None
    return prepared


def _wrapper(prepared: str, filename: str) -> str:
    """Build the kernel-side code that runs one prepared source.

    Seeds the shared namespace once, then execs the prepared tree in it. A
    trailing expression is evaluated after the statements and published through
    IPython with the `_mcp_trailing` metadata, so the collector can turn it into
    an `execute_result`; a `None` value publishes nothing. Pure: it runs no code.
    """
    return f'''
if "__mcp_ns__" not in globals():
    _mcp_seed = {{"__name__": "__main__"}}
    exec("from sage.all import *", _mcp_seed)
    exec("from IPython.display import Image", _mcp_seed)
    globals()["__mcp_ns__"] = _mcp_seed
_mcp_ns = globals()["__mcp_ns__"]
import ast as _mcp_ast
_mcp_tree = _mcp_ast.parse({prepared!r})
if _mcp_tree.body and isinstance(_mcp_tree.body[-1], _mcp_ast.Expr):
    _mcp_last = _mcp_ast.Expression(_mcp_tree.body.pop().value)
    exec(compile(_mcp_tree, {filename!r}, "exec"), _mcp_ns, _mcp_ns)
    _mcp_value = eval(compile(_mcp_last, {filename!r}, "eval"), _mcp_ns, _mcp_ns)
    if _mcp_value is not None:
        from IPython.display import display as _mcp_display
        _mcp_data = {{"text/plain": repr(_mcp_value)}}
        # Sage objects can give LaTeX and HTML. Use them when a notebook shows the value.
        for _mcp_mime, _mcp_attr, _mcp_fmt in (("text/latex", "_latex_", "$%s$"),
                                               ("text/html", "_html_", "%s")):
            _mcp_method = getattr(_mcp_value, _mcp_attr, None)
            if _mcp_method is not None:
                try:
                    _mcp_data[_mcp_mime] = _mcp_fmt % _mcp_method()
                except Exception:
                    pass
        _mcp_display(_mcp_data, raw=True, metadata={{"_mcp_trailing": True}})
else:
    exec(compile(_mcp_tree, {filename!r}, "exec"), _mcp_ns, _mcp_ns)
'''
