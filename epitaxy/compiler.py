"""The Epitaxy compilation pipeline.

Source -> tokens -> AST -> ownership/borrow checking -> C++20.
"""

from __future__ import annotations

from . import ast
from .codegen import CodeGen, Options
from .ownership import analyze
from .parser import parse
from .safeonly import validate as validate_safe_only


def transpile(source: str, filename: str = "<input>", options: Options | None = None) -> str:
    """Parse, ownership-check and lower an Epitaxy translation unit."""

    options = options or Options()
    if options.safe_only:
        # Safe-only implies the strict checked subset.
        options.strict_safety = True
    if options.strict_safety:
        # Strict mode is not negotiable: all checks stay on.
        options.subscript_checks = True
        options.null_checks = True
        options.div_checks = True
        options.sign_checks = True
        options.ownership_checks = True
    tu = parse(source, filename)
    has_module_decl = any(
        isinstance(item, (ast.ModuleDecl, ast.ImportDecl)) for item in tu.items
    )
    if has_module_decl and options.cpp_module is None and not options.cpp_module_entry:
        from .diag import CompileError

        line = col = 0
        for item in tu.items:
            if isinstance(item, (ast.ModuleDecl, ast.ImportDecl)):
                line, col = item.line, item.col
                break
        raise CompileError(
            "'module'/'import' declarations require the --cpp-modules build mode",
            line,
            col,
            filename,
        )
    if options.safe_only:
        validate_safe_only(tu, filename)
    info = analyze(
        tu,
        filename,
        enabled=options.ownership_checks,
        strict=options.strict_safety,
    )
    return CodeGen(options, filename).generate(tu, info)
