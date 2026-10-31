"""Definite-last-use analysis for automatic move/forward.

Cpp2 automatically moves from the last use of a local variable, and forwards
from the last use of a ``forward`` parameter.  Doing that safely requires
knowing that a use really is the last one on every execution path.

Epitaxy uses a conservative approximation that is sound:

* only names declared in the current function (parameters and locals) are
  considered,
* the use must be the lexically last use of the name anywhere in the function,
* the use must not be inside any loop (a later iteration could use it again),
* the name must never be captured by a lambda (deferred evaluation), and its
  address must never be taken,
* the use must be in a position where a value is consumed (call argument,
  assignment right-hand side, initialiser, returned value, or an operand).

Uses that do not meet these conditions are simply left as ordinary lvalues,
which is always correct.
"""

from __future__ import annotations

import dataclasses

from . import ast

SAFE_PARENTS = {"arg", "rhs", "init", "return", "operand", "list"}


class _Use:
    __slots__ = ("order", "node", "depth", "in_lambda", "parent")

    def __init__(self, order, node, depth, in_lambda, parent):
        self.order = order
        self.node = node
        self.depth = depth
        self.in_lambda = in_lambda
        self.parent = parent


class _Analysis:
    def __init__(self):
        self.counter = 0
        self.uses: dict[str, list[_Use]] = {}
        self.locals: set[str] = set()
        self.address_taken: set[str] = set()

    # -- collection -----------------------------------------------------

    def add_use(self, name, node, depth, in_lambda, parent):
        self.counter += 1
        self.uses.setdefault(name, []).append(
            _Use(self.counter, node, depth, in_lambda, parent)
        )

    def walk_stmt(self, stmt, depth=0, in_lambda=False):
        if stmt is None:
            return
        if isinstance(stmt, ast.Block):
            for s in stmt.stmts:
                self.walk_stmt(s, depth, in_lambda)
        elif isinstance(stmt, ast.ExprStmt):
            self.walk_expr(stmt.expr, "operand", depth, in_lambda)
        elif isinstance(stmt, ast.VarDecl):
            if stmt.name:
                self.locals.add(stmt.name)
            self.walk_expr(stmt.init, "init", depth, in_lambda)
            self.walk_type(stmt.type, depth, in_lambda)
        elif isinstance(stmt, ast.Return):
            self.walk_expr(stmt.expr, "return", depth, in_lambda)
        elif isinstance(stmt, ast.If):
            self.walk_expr(stmt.cond, "operand", depth, in_lambda)
            self.walk_stmt(stmt.then, depth, in_lambda)
            self.walk_stmt(stmt.else_, depth, in_lambda)
        elif isinstance(stmt, ast.While):
            self.walk_expr(stmt.cond, "operand", depth, in_lambda)
            self.walk_stmt(stmt.body, depth + 1, in_lambda)
            self.walk_expr(stmt.next_expr, "operand", depth, in_lambda)
        elif isinstance(stmt, ast.DoWhile):
            self.walk_stmt(stmt.body, depth + 1, in_lambda)
            self.walk_expr(stmt.next_expr, "operand", depth, in_lambda)
            self.walk_expr(stmt.cond, "operand", depth, in_lambda)
        elif isinstance(stmt, ast.For):
            self.walk_expr(stmt.range, "operand", depth, in_lambda)
            if stmt.param is not None and stmt.param.name:
                self.locals.add(stmt.param.name)
            self.walk_stmt(stmt.body, depth + 1, in_lambda)
            self.walk_expr(stmt.next_expr, "operand", depth, in_lambda)
        elif isinstance(stmt, ast.TryCatch):
            self.walk_stmt(stmt.body, depth, in_lambda)
            for catch in stmt.catches:
                if catch.param is not None and catch.param.name:
                    self.locals.add(catch.param.name)
                self.walk_stmt(catch.body, depth, in_lambda)
        elif isinstance(stmt, ast.Throw):
            self.walk_expr(stmt.expr, "operand", depth, in_lambda)
        elif isinstance(stmt, ast.Assert):
            self.walk_expr(stmt.cond, "operand", depth, in_lambda)
            self.walk_expr(stmt.message, "operand", depth, in_lambda)
        elif isinstance(stmt, ast.RaiiScope):
            for p in stmt.params:
                if p.name:
                    self.locals.add(p.name)
                self.walk_expr(p.default, "init", depth, in_lambda)
            self.walk_stmt(stmt.body, depth + 1, in_lambda)
        elif isinstance(stmt, ast.UsingDecl):
            pass
        elif isinstance(stmt, ast.Preproc):
            pass

    def walk_type(self, t, depth, in_lambda):
        if isinstance(t, ast.NamedType) and t.args:
            for arg in t.args:
                if isinstance(arg, ast.Expr):
                    self.walk_expr(arg, "operand", depth, in_lambda)
        elif isinstance(t, ast.ConstrainedType):
            self.walk_expr(t.constraint, "operand", depth, in_lambda)
            self.walk_type(t.base, depth, in_lambda)

    def walk_expr(self, node, parent, depth, in_lambda):
        if node is None:
            return
        if isinstance(node, ast.NameExpr) and len(node.name.parts) == 1 and not node.name.leading_global:
            self.add_use(node.name.parts[0], node, depth, in_lambda, parent)
            return
        if isinstance(node, ast.Id):
            self.add_use(node.name, node, depth, in_lambda, parent)
            return
        if isinstance(node, (ast.Literal, ast.Parenthesized)) and not isinstance(node, ast.Literal):
            self.walk_expr(node.expr, parent, depth, in_lambda)
            return
        if isinstance(node, ast.Literal):
            return
        if isinstance(node, ast.Call):
            self.walk_expr(node.callee, "callee", depth, in_lambda)
            for a in node.args:
                self.walk_expr(a, "arg", depth, in_lambda)
            return
        if isinstance(node, ast.Member):
            self.walk_expr(node.obj, "obj", depth, in_lambda)
            return
        if isinstance(node, ast.Binary):
            self.walk_expr(node.left, "operand", depth, in_lambda)
            self.walk_expr(node.right, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.CompareChain):
            self.walk_expr(node.first, "operand", depth, in_lambda)
            for _op, e in node.rest:
                self.walk_expr(e, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.Assign):
            self.walk_expr(node.lhs, "target", depth, in_lambda)
            self.walk_expr(node.rhs, "rhs", depth, in_lambda)
            return
        if isinstance(node, ast.Subscript):
            self.walk_expr(node.obj, "obj", depth, in_lambda)
            self.walk_expr(node.index, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.Postfix):
            if node.op == "&":
                self._mark_address(node.expr)
                self.walk_expr(node.expr, "address", depth, in_lambda)
            elif node.op in ("++", "--"):
                self.walk_expr(node.expr, "incdec", depth, in_lambda)
            else:
                self.walk_expr(node.expr, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.Unary):
            self.walk_expr(node.expr, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.Is):
            self.walk_expr(node.expr, "operand", depth, in_lambda)
            self.walk_constraint(node.constraint, depth, in_lambda)
            return
        if isinstance(node, ast.As):
            self.walk_expr(node.expr, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.Range):
            self.walk_expr(node.first, "operand", depth, in_lambda)
            self.walk_expr(node.last, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.Inspect):
            self.walk_expr(node.expr, "operand", depth, in_lambda)
            for alt in node.alts:
                self.walk_constraint(alt.constraint, depth + 1, in_lambda)
                self.walk_expr(alt.value, "operand", depth + 1, in_lambda)
            return
        if isinstance(node, ast.Lambda):
            self.walk_stmt(node.body.stmt, depth, True)
            return
        if isinstance(node, ast.Temporary):
            self.walk_expr(node.init, "init", depth, in_lambda)
            return
        if isinstance(node, ast.New):
            for a in node.args:
                self.walk_expr(a, "arg", depth, in_lambda)
            return
        if isinstance(node, (ast.Move, ast.Forward)):
            self.walk_expr(node.expr, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.Fold):
            self.walk_expr(node.pack, "operand", depth, in_lambda)
            self.walk_expr(node.init, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.PackExpansion):
            self.walk_expr(node.expr, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.SizeofPack):
            self.walk_expr(node.expr, "operand", depth, in_lambda)
            return
        if isinstance(node, ast.CommaList):
            for item in node.items:
                self.walk_expr(item, "list", depth, in_lambda)
            return
        if isinstance(node, ast.StringLiteral):
            for part in node.parts:
                if isinstance(part, ast.StrInterp):
                    self.walk_expr(part.expr, "operand", depth, in_lambda)
            return

    def walk_constraint(self, constraint, depth, in_lambda):
        if isinstance(constraint, ast.Expr):
            self.walk_expr(constraint, "operand", depth, in_lambda)
        elif isinstance(constraint, ast.PointerType):
            pass

    def _mark_address(self, expr):
        if isinstance(expr, ast.NameExpr) and len(expr.name.parts) == 1:
            self.address_taken.add(expr.name.parts[0])
        elif isinstance(expr, ast.Id):
            self.address_taken.add(expr.name)


def analyze_moves(decl: ast.FunctionDecl) -> dict[int, str]:
    analysis = _Analysis()

    param_kinds: dict[str, str] = {}
    for p in decl.params:
        if p.name and not p.is_this and not p.is_that:
            for kind in ("move", "forward"):
                if kind in p.qualifiers:
                    param_kinds[p.name] = kind
                    break
            else:
                param_kinds[p.name] = "in"

    if decl.body is None:
        return {}
    analysis.walk_stmt(decl.body.stmt)

    result: dict[int, str] = {}
    for name in analysis.locals | set(param_kinds):
        uses = analysis.uses.get(name)
        if not uses:
            continue
        if name in param_kinds and param_kinds[name] not in ("move", "forward"):
            continue
        if name in analysis.address_taken:
            continue
        kind = "forward" if param_kinds.get(name) == "forward" else "move"
        # A lambda capture or a use inside a loop would make a later use
        # possible; both are conservatively rejected.
        if any(u.in_lambda for u in uses):
            continue
        last = max(uses, key=lambda u: u.order)
        if last.depth > 0:
            continue
        if last.parent not in SAFE_PARENTS:
            continue
        # A returned local variable is already implicitly moved; adding an
        # explicit std::move would pessimise copy elision.  Forwarding a
        # forwarding reference still needs std::forward, though.
        if last.parent == "return" and kind == "move":
            continue
        result[id(last.node)] = kind
    return result
