"""Safe-only mode validation.

``--safe-only`` restricts a translation unit to the Epitaxy language and the
compiler runtime.  User-written C++ interop of any kind is rejected before
ownership checking:

* no preprocessor directives (``#include``, ``#define``, …);
* no ``cpp1 { ... }`` blocks;
* no ``unsafe`` functions, blocks or expressions, no raw pointer types;
* no external C++ types or names (only builtin scalars, user-declared types,
  ``unique``/``shared``, borrows, and the trusted runtime string type);
* no ``unchecked_*`` operations;
* no thread API (``epi::spawn`` etc. are C++ runtime and not part of safe-only);
* no by-reference capture in function expressions.

The compiler runtime itself (``std::string``, interpolation, ``epi::print`` /
``epi::println``, contracts, bounds/null/overflow helpers) is trusted: it is
generated/owned by the Epitaxy implementation, not written by the user.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass

from . import ast
from .diag import CompileError

BUILTIN_TYPES = {
    "i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64",
    "f32", "f64", "bool", "char", "void",
    "longdouble", "longlong", "uint", "ulong", "ulonglong", "ushort",
    "usize", "isize", "auto",
}

# Types provided by the compiler runtime (not user-written C++ interop).
RUNTIME_TYPES = {"std::string"}

# Qualified names that are part of the checked runtime surface.
ALLOWED_QUALIFIED = {"epi::print", "epi::println"}

# Bare names allowed to appear without a user declaration.
ALLOWED_BARE = {"print", "println", "unique", "shared", "this", "new"}

TYPE_NODES = (
    ast.NamedType,
    ast.PointerType,
    ast.ConstType,
    ast.DeducedType,
    ast.BorrowType,
    ast.FunctionType,
    ast.TypeOfType,
    ast.ConstrainedType,
)


def validate(tu: ast.TranslationUnit, filename: str = "<input>") -> None:
    _Validator(filename).run(tu)


class _Validator:
    def __init__(self, filename: str):
        self.filename = filename
        self.user_types: set[str] = set()
        self.user_aliases: set[str] = set()
        self.user_namespaces: set[str] = set()
        self.user_functions: set[str] = set()
        self.globals: set[str] = set()
        self.members: dict[str, set[str]] = {}
        self.template_names: set[str] = set()

    # -- diagnostics -----------------------------------------------------

    def error(self, node, message: str):
        raise CompileError(
            message,
            getattr(node, "line", 0),
            getattr(node, "col", 0),
            self.filename,
        )

    # -- name collection -------------------------------------------------

    def collect(self, items: list, owner: str | None = None) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.user_namespaces.add(item.name)
                self.collect(item.members, owner)
            elif isinstance(item, ast.TypeDecl):
                for param in item.template_params:
                    self.template_names.add(param.name)
                if item.kind == "==":
                    self.user_aliases.add(item.name)
                else:
                    self.user_types.add(item.name)
                    members = set()
                    for member in item.members:
                        if isinstance(member, ast.VarDecl) and member.name != "this":
                            members.add(member.name)
                    self.members[item.name] = members
                    self.collect(item.members, item.name)
            elif isinstance(item, ast.FunctionDecl):
                for param in item.template_params:
                    self.template_names.add(param.name)
                if owner is None and item.name:
                    self.user_functions.add(item.name)
            elif isinstance(item, ast.VarDecl) and owner is None and item.name:
                self.globals.add(item.name)

    # -- entry point -----------------------------------------------------

    def run(self, tu: ast.TranslationUnit) -> None:
        self.collect(tu.items)
        self.visit_items(tu.items, None)

    # -- declarations ----------------------------------------------------

    def visit_items(self, items: list, owner: str | None) -> None:
        for item in items:
            if isinstance(item, (ast.ModuleDecl, ast.ImportDecl)):
                continue
            if isinstance(item, ast.Preproc):
                self.error(
                    item,
                    "preprocessor directives are not allowed in safe-only mode",
                )
            elif isinstance(item, ast.Cpp1Block):
                self.error(
                    item,
                    "'cpp1' blocks are not allowed in safe-only mode",
                )
            elif isinstance(item, ast.NamespaceDecl):
                self.visit_items(item.members, owner)
            elif isinstance(item, ast.TypeDecl):
                for param in item.template_params:
                    self.visit_template_param(param)
                if item.kind == "==":
                    self.visit_type(item.alias)
                else:
                    self.visit_items(item.members, item.name)
            elif isinstance(item, ast.FunctionDecl):
                self.visit_function(item, owner)
            elif isinstance(item, ast.VarDecl):
                locals_ = self.scope_locals(owner)
                if item.type is not None:
                    self.visit_type(item.type)
                self.visit_expr(item.init, locals_, owner)
            elif isinstance(item, ast.Enumerator):
                self.visit_expr(item.value, self.scope_locals(owner), owner)
            elif isinstance(item, ast.UsingDecl):
                self.check_using(item)

    def visit_template_param(self, param: ast.TemplateParam) -> None:
        if param.type is not None:
            self.visit_type(param.type)
        self.visit_expr(param.default, set(), None)

    def check_using(self, item: ast.UsingDecl) -> None:
        target = item.target.split("::")
        if len(target) == 1 and target[0] == "_":
            return
        head = target[0]
        if head in self.user_namespaces or head in self.user_types or head in self.user_aliases:
            return
        self.error(
            item,
            f"using external C++ name '{item.target}' is not allowed in safe-only mode",
        )

    def visit_function(self, decl: ast.FunctionDecl, owner: str | None) -> None:
        if decl.is_unsafe:
            self.error(
                decl,
                "'unsafe' functions are not allowed in safe-only mode",
            )
        for param in decl.template_params:
            self.visit_template_param(param)
        locals_ = self.scope_locals(owner)
        for param in decl.params:
            if param.type is not None:
                self.visit_type(param.type)
            self.visit_expr(param.default, locals_, owner)
            if param.name and not param.is_this and not param.is_wildcard:
                locals_.add(param.name)
        if decl.returns is not None:
            returns = decl.returns
            if returns.type is not None:
                self.visit_type(returns.type)
            for named in returns.named:
                if named.type is not None:
                    self.visit_type(named.type)
        if decl.requires is not None:
            self.visit_expr(decl.requires, locals_, owner)
        for contract in decl.contracts:
            self.visit_expr(contract.cond, locals_, owner)
            self.visit_expr(contract.message, locals_, owner)
            for group in contract.groups:
                self.visit_expr(group, locals_, owner)
            for predicate in contract.predicates:
                self.visit_expr(predicate, locals_, owner)
        if decl.body is not None:
            self.visit_stmt(decl.body.stmt, locals_, owner)

    # -- statements ------------------------------------------------------

    def scope_locals(self, owner: str | None) -> set[str]:
        locals_ = set(ALLOWED_BARE)
        locals_.add("this")
        if owner is not None:
            locals_ |= self.members.get(owner, set())
        return locals_

    def visit_stmt(self, stmt, locals_: set[str], owner: str | None) -> None:
        if stmt is None:
            return
        if isinstance(stmt, ast.Block):
            inner = set(locals_)
            for item in stmt.stmts:
                self.visit_stmt(item, inner, owner)
            return
        if isinstance(stmt, ast.UnsafeBlock):
            self.error(
                stmt,
                "'unsafe' blocks are not allowed in safe-only mode",
            )
        if isinstance(stmt, ast.Cpp1Block):
            self.error(stmt, "'cpp1' blocks are not allowed in safe-only mode")
        if isinstance(stmt, ast.Preproc):
            self.error(
                stmt,
                "preprocessor directives are not allowed in safe-only mode",
            )
        if isinstance(stmt, ast.UsingDecl):
            self.check_using(stmt)
            return
        if isinstance(stmt, ast.FunctionDecl):
            self.visit_function(stmt, owner)
            if stmt.name:
                locals_.add(stmt.name)
            return
        if isinstance(stmt, ast.VarDecl):
            if stmt.type is not None:
                self.visit_type(stmt.type)
            self.visit_expr(stmt.init, locals_, owner)
            if stmt.name:
                locals_.add(stmt.name)
            return
        if isinstance(stmt, ast.ExprStmt):
            self.visit_expr(stmt.expr, locals_, owner)
            return
        if isinstance(stmt, ast.Return):
            self.visit_expr(stmt.expr, locals_, owner)
            return
        if isinstance(stmt, ast.If):
            self.visit_expr(stmt.cond, locals_, owner)
            self.visit_stmt(stmt.then, set(locals_), owner)
            self.visit_stmt(stmt.else_, set(locals_), owner)
            return
        if isinstance(stmt, ast.While):
            self.visit_expr(stmt.cond, locals_, owner)
            self.visit_stmt(stmt.body, set(locals_), owner)
            self.visit_expr(getattr(stmt, "next_expr", None), locals_, owner)
            return
        if isinstance(stmt, ast.DoWhile):
            self.visit_stmt(stmt.body, set(locals_), owner)
            self.visit_expr(getattr(stmt, "next_expr", None), locals_, owner)
            self.visit_expr(stmt.cond, locals_, owner)
            return
        if isinstance(stmt, ast.For):
            self.visit_expr(stmt.range, locals_, owner)
            inner = set(locals_)
            param = stmt.param
            if param is not None:
                if param.type is not None:
                    self.visit_type(param.type)
                if param.name:
                    inner.add(param.name)
            self.visit_stmt(stmt.body, inner, owner)
            self.visit_expr(getattr(stmt, "next_expr", None), locals_, owner)
            return
        if isinstance(stmt, ast.TryCatch):
            self.visit_stmt(stmt.body, set(locals_), owner)
            for catch in stmt.catches:
                inner = set(locals_)
                if catch.param is not None:
                    if catch.param.type is not None:
                        self.visit_type(catch.param.type)
                    if catch.param.name:
                        inner.add(catch.param.name)
                self.visit_stmt(catch.body, inner, owner)
            return
        if isinstance(stmt, ast.Throw):
            self.visit_expr(stmt.expr, locals_, owner)
            return
        if isinstance(stmt, ast.Assert):
            self.visit_expr(stmt.cond, locals_, owner)
            self.visit_expr(stmt.message, locals_, owner)
            return
        if isinstance(stmt, ast.RaiiScope):
            inner = set(locals_)
            for param in stmt.params:
                if param.type is not None:
                    self.visit_type(param.type)
                self.visit_expr(param.default, inner, owner)
                if param.name:
                    inner.add(param.name)
            self.visit_stmt(stmt.body, inner, owner)
            return
        # break / continue / preproc handled above
        return

    # -- types -----------------------------------------------------------

    def visit_type(self, texpr) -> None:
        if texpr is None:
            return
        if isinstance(texpr, ast.PointerType):
            self.error(
                texpr,
                "raw pointer types are not allowed in safe-only mode",
            )
        if isinstance(texpr, ast.NamedType):
            name = texpr.name.text()
            if name not in BUILTIN_TYPES and name not in RUNTIME_TYPES:
                if name not in ("unique", "shared"):
                    if not (
                        name in self.user_types
                        or name in self.user_aliases
                        or name in self.template_names
                    ):
                        self.error(
                            texpr,
                            f"external C++ type '{name}' is not allowed in "
                            "safe-only mode",
                        )
            for arg in texpr.args or ():
                if isinstance(arg, ast.Node) and isinstance(arg, TYPE_NODES):
                    self.visit_type(arg)
                elif isinstance(arg, ast.Expr):
                    self.visit_expr(arg, set(ALLOWED_BARE) | {"this"}, None)
            return
        if isinstance(texpr, ast.ConstType):
            self.visit_type(texpr.inner)
            return
        if isinstance(texpr, ast.BorrowType):
            self.visit_type(texpr.inner)
            return
        if isinstance(texpr, ast.FunctionType):
            for param in texpr.params:
                self.visit_type(param)
            self.visit_type(texpr.returns)
            return
        if isinstance(texpr, ast.TypeOfType):
            self.visit_expr(texpr.expr, set(ALLOWED_BARE) | {"this"}, None)
            return
        if isinstance(texpr, ast.ConstrainedType):
            self.visit_type(texpr.base)
            self.visit_expr(texpr.constraint, set(ALLOWED_BARE) | {"this"}, None)
            return
        if isinstance(texpr, ast.DeducedType):
            return

    # -- expressions -----------------------------------------------------

    def check_name(self, node, locals_: set[str], owner: str | None) -> None:
        if isinstance(node, ast.NameExpr):
            name = node.name
            if name.leading_global:
                self.error(
                    node,
                    f"qualified external name '::{name.text()}' is not allowed "
                    "in safe-only mode",
                )
        else:
            name = ast.Name(parts=[node.name])
        if len(name.parts) == 1:
            bare = name.parts[0]
            if bare == "_":
                return
            if (
                bare in locals_
                or bare in self.globals
                or bare in self.user_types
                or bare in self.user_aliases
                or bare in self.user_functions
                or bare in self.template_names
                or bare in BUILTIN_TYPES
            ):
                return
            self.error(
                node,
                f"external C++ name '{bare}' is not allowed in safe-only mode",
            )
        text = "::".join(name.parts)
        if text in ALLOWED_QUALIFIED:
            return
        head = name.parts[0]
        if (
            head in self.user_types
            or head in self.user_aliases
            or head in self.user_namespaces
        ):
            return
        self.error(
            node,
            f"external C++ name '{text}' is not allowed in safe-only mode",
        )

    def check_reference_captures(self, lam: ast.Lambda) -> None:
        def unwrap(node):
            while isinstance(node, ast.Parenthesized):
                node = node.expr
            return node

        def walk(node):
            if node is None:
                return
            if isinstance(node, ast.Postfix) and node.op == "$":
                inner = unwrap(node.expr)
                if isinstance(inner, ast.Postfix) and inner.op == "&":
                    self.error(
                        lam,
                        "by-reference captures are not allowed in safe-only "
                        "mode; capture by value instead",
                    )
                walk(node.expr)
                return
            if isinstance(node, ast.Lambda) and node is not lam:
                return
            if isinstance(node, (list, tuple)):
                for item in node:
                    walk(item)
                return
            if is_dataclass(node) and not isinstance(node, type):
                for field in fields(node):
                    walk(getattr(node, field.name, None))

        walk(lam.body)

    def visit_expr(self, expr, locals_: set[str], owner: str | None) -> None:
        if expr is None:
            return
        if isinstance(expr, (ast.NameExpr, ast.Id)):
            self.check_name(expr, locals_, owner)
            return
        if isinstance(expr, ast.QualifiedNameExpr):
            head = expr.segments[0][0]
            if head not in self.user_types and head not in self.user_aliases:
                text = "::".join(seg[0] for seg in expr.segments)
                self.error(
                    expr,
                    f"external C++ name '{text}' is not allowed in safe-only mode",
                )
            for _name, args in expr.segments:
                for arg in args or ():
                    if isinstance(arg, TYPE_NODES):
                        self.visit_type(arg)
                    elif isinstance(arg, ast.Expr):
                        self.visit_expr(arg, locals_, owner)
            return
        if isinstance(expr, ast.Literal):
            return
        if isinstance(expr, ast.StringLiteral):
            for part in expr.parts:
                if isinstance(part, ast.StrInterp):
                    self.visit_expr(part.expr, locals_, owner)
            return
        if isinstance(expr, ast.Lambda):
            self.check_reference_captures(expr)
            for param in expr.template_params:
                self.visit_template_param(param)
            inner = set(locals_)
            for param in expr.params:
                if param.type is not None:
                    self.visit_type(param.type)
                self.visit_expr(param.default, inner, owner)
                if param.name and not param.is_wildcard:
                    inner.add(param.name)
            if expr.returns is not None and expr.returns.type is not None:
                self.visit_type(expr.returns.type)
            if expr.body is not None:
                self.visit_stmt(expr.body.stmt, inner, owner)
            return
        if isinstance(expr, ast.UnsafeExpr):
            self.error(
                expr,
                "'unsafe' expressions are not allowed in safe-only mode",
            )
        if isinstance(expr, ast.Call):
            self.visit_expr(expr.callee, locals_, owner)
            for arg in expr.args:
                self.visit_expr(arg, locals_, owner)
            for arg in expr.template_args or ():
                if isinstance(arg, TYPE_NODES):
                    self.visit_type(arg)
            return
        if isinstance(expr, ast.Member):
            if "::" in expr.name:
                self.error(
                    expr,
                    f"qualified external member '{expr.name}' is not allowed "
                    "in safe-only mode",
                )
            self.visit_expr(expr.obj, locals_, owner)
            return
        if isinstance(expr, ast.As):
            self.visit_expr(expr.expr, locals_, owner)
            self.visit_type(expr.type)
            return
        if isinstance(expr, ast.Is):
            self.visit_expr(expr.expr, locals_, owner)
            if isinstance(expr.constraint, TYPE_NODES):
                self.visit_type(expr.constraint)
            elif isinstance(expr.constraint, ast.Expr):
                self.visit_expr(expr.constraint, locals_, owner)
            return
        if isinstance(expr, ast.Inspect):
            self.visit_expr(expr.expr, locals_, owner)
            if expr.type is not None:
                self.visit_type(expr.type)
            for alt in expr.alts:
                if isinstance(alt.constraint, TYPE_NODES):
                    self.visit_type(alt.constraint)
                elif isinstance(alt.constraint, ast.Expr):
                    self.visit_expr(alt.constraint, locals_, owner)
                self.visit_expr(alt.value, locals_, owner)
            return
        if isinstance(expr, ast.New):
            self.visit_type(expr.type)
            for arg in expr.args:
                self.visit_expr(arg, locals_, owner)
            return
        if isinstance(expr, ast.Temporary):
            self.visit_type(expr.type)
            self.visit_expr(expr.init, locals_, owner)
            return
        if isinstance(expr, ast.Parenthesized):
            self.visit_expr(expr.expr, locals_, owner)
            return
        if isinstance(expr, ast.Postfix):
            self.visit_expr(expr.expr, locals_, owner)
            return
        if isinstance(expr, ast.Unary):
            self.visit_expr(expr.expr, locals_, owner)
            return
        if isinstance(expr, ast.Binary):
            self.visit_expr(expr.left, locals_, owner)
            self.visit_expr(expr.right, locals_, owner)
            return
        if isinstance(expr, ast.CompareChain):
            self.visit_expr(expr.first, locals_, owner)
            for _op, operand in expr.rest:
                self.visit_expr(operand, locals_, owner)
            return
        if isinstance(expr, ast.Assign):
            self.visit_expr(expr.lhs, locals_, owner)
            self.visit_expr(expr.rhs, locals_, owner)
            return
        if isinstance(expr, (ast.Move, ast.Forward, ast.OutArg)):
            self.visit_expr(expr.expr, locals_, owner)
            return
        if isinstance(expr, ast.Range):
            self.visit_expr(expr.first, locals_, owner)
            self.visit_expr(expr.last, locals_, owner)
            return
        if isinstance(expr, ast.Fold):
            self.visit_expr(expr.pack, locals_, owner)
            self.visit_expr(getattr(expr, "init", None), locals_, owner)
            return
        if isinstance(expr, ast.PackExpansion):
            self.visit_expr(expr.expr, locals_, owner)
            return
        if isinstance(expr, ast.SizeofPack):
            return
        if isinstance(expr, ast.CommaList):
            for item in expr.items:
                self.visit_expr(item, locals_, owner)
            return
        # Conservative fallback: walk dataclass fields.
        if is_dataclass(expr) and not isinstance(expr, type):
            for field in fields(expr):
                value = getattr(expr, field.name, None)
                if isinstance(value, TYPE_NODES):
                    self.visit_type(value)
                elif isinstance(value, ast.Expr):
                    self.visit_expr(value, locals_, owner)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        if isinstance(item, TYPE_NODES):
                            self.visit_type(item)
                        elif isinstance(item, ast.Expr):
                            self.visit_expr(item, locals_, owner)
