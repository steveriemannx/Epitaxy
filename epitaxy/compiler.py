"""The Epitaxy compilation pipeline.

Source -> tokens -> AST -> ownership/borrow checking -> C++20.
"""

from __future__ import annotations

from .codegen import CodeGen, Options
from .ownership import analyze
from .parser import parse


def transpile(source: str, filename: str = "<input>", options: Options | None = None) -> str:
    """Parse, ownership-check and lower an Epitaxy translation unit."""

    options = options or Options()
    if options.strict_safety:
        # Strict mode is not negotiable: all checks stay on.
        options.subscript_checks = True
        options.null_checks = True
        options.div_checks = True
        options.sign_checks = True
        options.ownership_checks = True
    tu = parse(source, filename)
    info = analyze(
        tu,
        filename,
        enabled=options.ownership_checks,
        strict=options.strict_safety,
    )
    return CodeGen(options, filename).generate(tu, info)
