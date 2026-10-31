"""Ownership and borrow checking for Epitaxy.

This pass implements the "option 1" ownership model:

* values are owned; ``unique<T>`` is a move-only owning handle, ``shared<T>`` is
  a shared owning handle;
* ``in T`` / ``inout T`` declare shared / exclusive borrows (also available as
  parameter kinds ``in`` / ``inout`` / ``out``);
* a borrow cannot outlive its owner, a borrow of a local cannot be returned, and
  moving or mutating an owner while it is borrowed is an error;
* moving a value invalidates it (use-after-move is an error);
* raw pointers (``*T``) may be dereferenced or created from a place only inside
  an ``unsafe`` block or an ``unsafe`` function.

The analysis is deliberately conservative and operates on Epitaxy declarations.
It does not model C++ library internals, threads/data races, or full lifetime
polymorphism; see docs/COMPATIBILITY.md for the exact rule set.

Public entry point: :func:`analyze`, which raises :class:`CompileError` on a
violation and returns code-generation annotations.
"""

from __future__ import annotations

from typing import Optional

from . import ast
from .diag import CompileError

BORROW_KINDS = ("in", "inout", "out")
VALUE_KINDS = ("in", "in_ref", "inout", "out", "copy", "move", "forward", "forward_ref")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class Ty:
    __slots__ = ("kind", "name", "borrow", "inner")

    def __init__(self, kind: str, name: str = "", borrow: str = "", inner: "Ty" = None):
        self.kind = kind  # value | unique | shared | borrow | raw | unknown
        self.name = name
        self.borrow = borrow
        self.inner = inner

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        if self.kind in ("unique", "shared", "raw") and self.inner is not None:
            return f"Ty({self.kind}<{self.inner!r}>)"
        if self.kind == "borrow":
            return f"Ty({self.borrow} {self.inner!r})"
        return f"Ty({self.kind} {self.name})"

    @property
    def move_only(self) -> bool:
        return self.kind == "unique"

    @property
    def copyable(self) -> bool:
        return self.kind != "unique"

    def type_name(self) -> str:
        if self.kind == "value":
            return self.name
        if self.kind in ("unique", "shared", "raw") and self.inner is not None:
            return self.inner.type_name()
        if self.kind == "borrow" and self.inner is not None:
            return self.inner.type_name()
        return ""


UNKNOWN = Ty("unknown")


class ParamSig:
    __slots__ = ("name", "kind", "ty", "has_default", "variadic")

    def __init__(self, name, kind, ty, has_default=False, variadic=False):
        self.name = name
        self.kind = kind
        self.ty = ty
        self.has_default = has_default
        self.variadic = variadic


class RetSig:
    __slots__ = ("kind", "ty", "borrow", "source")

    def __init__(self, kind="value", ty=UNKNOWN, borrow=None, source=None):
        self.kind = kind  # value | void | borrow
        self.ty = ty
        self.borrow = borrow
        self.source = source


class Signature:
    __slots__ = ("name", "params", "ret", "this_kind", "is_unsafe", "owner")

    def __init__(self, name, params, ret, this_kind, is_unsafe, owner):
        self.name = name
        self.params = params
        self.ret = ret
        self.this_kind = this_kind
        self.is_unsafe = is_unsafe
        self.owner = owner

    def accepts(self, argc: int) -> bool:
        required = 0
        for p in self.params:
            if not p.has_default and not p.variadic:
                required += 1
        if any(p.variadic for p in self.params):
            return argc >= required
        return required <= argc <= len(self.params)


class OwnershipInfo:
    """Code-generation annotations produced by the analysis."""

    def __init__(self):
        self.deduced_borrow: dict[int, str] = {}
        self.borrow_names: dict[int, set[str]] = {}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Tables:
    def __init__(self):
        self.aliases: dict[str, ast.TypeExpr] = {}
        self.types: dict[str, ast.TypeDecl] = {}
        self.data_members: dict[str, list[tuple[str, ast.TypeExpr]]] = {}
        self.free_functions: dict[str, list[Signature]] = {}
        self.methods: dict[tuple[str, str], list[Signature]] = {}
        self.constructors: dict[str, list[Signature]] = {}


def _param_kind(p: ast.Param) -> str:
    for q in ("in_ref", "forward_ref", "in", "inout", "out", "copy", "move", "forward"):
        if q in p.qualifiers:
            return q
    return "in"


def _this_kind(decl: ast.FunctionDecl) -> Optional[str]:
    if decl.params and decl.params[0].is_this:
        for q in ("out", "inout", "move", "in"):
            if q in decl.params[0].qualifiers:
                return q
        return "in"
    return None


class TypeResolver:
    def __init__(self, tables: Tables):
        self.tables = tables

    def ty_of(self, texpr, depth: int = 0) -> Ty:
        if texpr is None or isinstance(texpr, ast.DeducedType):
            return UNKNOWN
        if isinstance(texpr, ast.ConstrainedType):
            return self.ty_of(texpr.base, depth)
        if isinstance(texpr, ast.ConstType):
            return self.ty_of(texpr.inner, depth)
        if isinstance(texpr, ast.BorrowType):
            return Ty("borrow", borrow=texpr.kind, inner=self.ty_of(texpr.inner, depth))
        if isinstance(texpr, ast.PointerType):
            return Ty("raw", inner=self.ty_of(texpr.inner, depth))
        if isinstance(texpr, ast.FunctionType):
            return Ty("value", name="function")
        if isinstance(texpr, ast.TypeOfType):
            return UNKNOWN
        if isinstance(texpr, ast.NamedType):
            name = texpr.name.text()
            if (
                len(texpr.name.parts) == 1
                and depth < 12
                and name in self.tables.aliases
                and name not in self.tables.types
            ):
                return self.ty_of(self.tables.aliases[name], depth + 1)
            if len(texpr.name.parts) == 1 and name in ("unique", "shared") and texpr.args:
                return Ty(name, inner=self.ty_of(texpr.args[0], depth))
            return Ty("value", name=name)
        return UNKNOWN


# ---------------------------------------------------------------------------
# Places and environment
# ---------------------------------------------------------------------------


class Place:
    __slots__ = ("root", "path", "via")

    def __init__(self, root: str, path: tuple = (), via=None):
        self.root = root
        self.path = path
        self.via = via  # LocalState of a borrow variable used as the access path

    def child(self, segment: str) -> "Place":
        return Place(self.root, self.path + (segment,), self.via)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Place({self.via.name + '->' if self.via else ''}{self.root}{''.join('.' + p for p in self.path)})"


class LocalState:
    __slots__ = (
        "name",
        "ty",
        "kind",  # local | param | this
        "param_kind",
        "mutable",
        "scope_depth",
        "loop_depth",
        "is_borrow",
        "borrow_kind",
        "borrow_root",
        "borrow_path",
        "moved",
        "out_param",
        "initialized",
    )

    def __init__(self, name, ty, kind="local", param_kind=None, mutable=True,
                 scope_depth=0, loop_depth=0):
        self.name = name
        self.ty = ty
        self.kind = kind
        self.param_kind = param_kind
        self.mutable = mutable
        self.scope_depth = scope_depth
        self.loop_depth = loop_depth
        self.is_borrow = False
        self.borrow_kind = None
        self.borrow_root = None
        self.borrow_path = ()
        self.moved = False
        self.out_param = param_kind == "out"
        self.initialized = param_kind != "out"


class Borrow:
    __slots__ = ("root", "path", "kind", "holder", "depth", "node")

    def __init__(self, root, path, kind, holder, depth, node):
        self.root = root
        self.path = path
        self.kind = kind
        self.holder = holder
        self.depth = depth
        self.node = node


def _overlap(a: tuple, b: tuple) -> bool:
    n = min(len(a), len(b))
    return a[:n] == b[:n]


class Env:
    def __init__(self, func: ast.FunctionDecl, owner, this_kind, tables: Tables,
                 resolver: TypeResolver, info: OwnershipInfo):
        self.func = func
        self.owner = owner
        self.this_kind = this_kind
        self.tables = tables
        self.resolver = resolver
        self.info = info
        self.locals: dict[str, LocalState] = {}
        self.borrows: list[Borrow] = []
        self.depth = 0
        self.loop_depth = 0
        self.unsafe = 1 if func.is_unsafe else 0
        self.scope_stack: list[list[str]] = [[]]

    def push_scope(self):
        self.depth += 1
        self.scope_stack.append([])

    def pop_scope(self):
        for name in self.scope_stack.pop():
            self.locals.pop(name, None)
        self.borrows = [b for b in self.borrows if b.depth < self.depth]
        self.depth -= 1

    def declare(self, name, ty, **kwargs) -> LocalState:
        local = LocalState(name, ty, scope_depth=self.depth, loop_depth=self.loop_depth, **kwargs)
        self.locals[name] = local
        self.scope_stack[-1].append(name)
        return local


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class Checker:
    def __init__(self, tables: Tables, resolver: TypeResolver, info: OwnershipInfo,
                 filename: str, enabled: bool = True):
        self.tables = tables
        self.resolver = resolver
        self.info = info
        self.filename = filename
        self.enabled = enabled

    # -- diagnostics ----------------------------------------------------

    def error(self, message: str, node) -> None:
        if not self.enabled:
            return
        line = getattr(node, "line", 0)
        col = getattr(node, "col", 0)
        raise CompileError(message, line, col, self.filename)

    # -- signature construction -----------------------------------------

    def signature_of(self, decl: ast.FunctionDecl, owner: Optional[str]) -> Signature:
        params = []
        for p in decl.params:
            if p.is_this:
                continue
            if p.is_that:
                params.append(
                    ParamSig("that", "move" if "move" in p.qualifiers else "in",
                             Ty("value", name=owner or ""), False, False)
                )
                continue
            params.append(
                ParamSig(
                    p.name or "_",
                    _param_kind(p),
                    self.resolver.ty_of(p.type),
                    p.default is not None,
                    p.variadic,
                )
            )
        ret = RetSig("void")
        r = decl.returns
        if r is not None:
            if r.kind == "void":
                ret = RetSig("void")
            elif getattr(r, "borrow_kind", None):
                ret = RetSig("borrow", self.resolver.ty_of(r.type), r.borrow_kind,
                             r.borrow_source)
            elif r.kind == "single":
                ret = RetSig("value", self.resolver.ty_of(r.type), source=r.forward)
            else:
                ret = RetSig("value", UNKNOWN)
        elif decl.body is not None and isinstance(decl.body.stmt, ast.ExprStmt):
            ret = RetSig("value", UNKNOWN)
        this_kind = _this_kind(decl)
        return Signature(decl.name or "", params, ret, this_kind, decl.is_unsafe, owner)

    # -- expression type inference --------------------------------------

    def infer_ty(self, expr, env: Env) -> Ty:
        if expr is None:
            return UNKNOWN
        if isinstance(expr, ast.Parenthesized):
            return self.infer_ty(expr.expr, env)
        if isinstance(expr, ast.UnsafeExpr):
            return self.infer_ty(expr.expr, env)
        if isinstance(expr, ast.NameExpr) and len(expr.name.parts) == 1:
            name = expr.name.parts[0]
            local = env.locals.get(name)
            if local is not None:
                return local.ty
            if env.owner:
                for member_name, member_type in self.tables.data_members.get(env.owner, ()):
                    if member_name == name:
                        return self.resolver.ty_of(member_type)
            return UNKNOWN
        if isinstance(expr, ast.Id):
            if expr.name == "this":
                return Ty("value", name=env.owner or "")
            local = env.locals.get(expr.name)
            return local.ty if local is not None else UNKNOWN
        if isinstance(expr, ast.Postfix) and expr.op == "*":
            inner = self.infer_ty(expr.expr, env)
            if inner.kind in ("unique", "shared", "raw", "borrow") and inner.inner is not None:
                return inner.inner
            return UNKNOWN
        if isinstance(expr, ast.Member):
            obj_ty = self.infer_ty(expr.obj, env)
            name = expr.name.split("::")[-1]
            owner = obj_ty.type_name()
            for member_name, member_type in self.tables.data_members.get(owner, ()):
                if member_name == name:
                    return self.resolver.ty_of(member_type)
            return UNKNOWN
        if isinstance(expr, ast.Call):
            sig = self.resolve_call(expr, env)
            if sig is not None:
                return sig.ret.ty if sig.ret.kind == "value" else sig.ret.ty
            return UNKNOWN
        if isinstance(expr, ast.New):
            inner = self.resolver.ty_of(expr.type)
            return Ty("unique", inner=inner)
        if isinstance(expr, ast.Temporary):
            return self.resolver.ty_of(expr.type)
        if isinstance(expr, ast.StringLiteral):
            return Ty("value", name="std::string")
        if isinstance(expr, ast.Binary) and expr.op in (
            "==", "!=", "<", ">", "<=", ">=", "&&", "||"
        ):
            return Ty("value", name="bool")
        return UNKNOWN

    # -- call resolution -------------------------------------------------

    def resolve_call(self, node: ast.Call, env: Env) -> Optional[Signature]:
        callee = node.callee
        argc = len(node.args)
        if isinstance(callee, ast.NameExpr) and len(callee.name.parts) == 1:
            name = callee.name.parts[0]
            candidates = self.tables.free_functions.get(name, []) + self.tables.constructors.get(name, [])
            for sig in candidates:
                if sig.accepts(argc):
                    return sig
            return None
        if isinstance(callee, ast.Member) and not callee.template_args:
            name = callee.name.split("::")[-1]
            owner = self.infer_ty(callee.obj, env).type_name()
            for sig in self.tables.methods.get((owner, name), []):
                if sig.accepts(argc):
                    return sig
            # UFCS fallback to a free function with the receiver as first argument
            for sig in self.tables.free_functions.get(name, []):
                if sig.accepts(argc + 1):
                    return sig
            return None
        return None

    def is_member_call(self, node: ast.Call, env: Env) -> bool:
        callee = node.callee
        if not isinstance(callee, ast.Member) or callee.template_args:
            return False
        owner = self.infer_ty(callee.obj, env).type_name()
        name = callee.name.split("::")[-1]
        return bool(self.tables.methods.get((owner, name)))

    # -- place resolution ------------------------------------------------

    def as_place(self, expr, env: Env) -> Optional[Place]:
        if expr is None:
            return None
        if isinstance(expr, ast.Parenthesized):
            return self.as_place(expr.expr, env)
        if isinstance(expr, ast.UnsafeExpr):
            return self.as_place(expr.expr, env)
        if isinstance(expr, ast.OutArg):
            return self.as_place(expr.expr, env)
        if isinstance(expr, ast.NameExpr) and len(expr.name.parts) == 1:
            name = expr.name.parts[0]
            local = env.locals.get(name)
            if local is None:
                if env.owner and any(m == name for m, _t in self.tables.data_members.get(env.owner, ())):
                    return Place("this", (name,))
                return Place(name) if not expr.name.leading_global else None
            if local.is_borrow:
                return Place(local.borrow_root, local.borrow_path, via=local)
            return Place(name)
        if isinstance(expr, ast.Id):
            if expr.name == "this":
                return Place("this")
            local = env.locals.get(expr.name)
            if local is None:
                return Place(expr.name)
            if local.is_borrow:
                return Place(local.borrow_root, local.borrow_path, via=local)
            return Place(expr.name)
        if isinstance(expr, ast.Member):
            base = self.as_place(expr.obj, env)
            if base is None:
                return None
            return base.child(expr.name.split("::")[-1])
        if isinstance(expr, ast.Subscript):
            base = self.as_place(expr.obj, env)
            if base is None:
                return None
            return base.child("[]")
        if isinstance(expr, ast.Postfix) and expr.op == "*":
            base = self.as_place(expr.expr, env)
            if base is None:
                return None
            return base.child("*")
        return None

    # -- root mutability -------------------------------------------------

    def root_info(self, place: Place, env: Env):
        if place.root == "this":
            mutable = env.this_kind in ("inout", "out")
        else:
            local = env.locals.get(place.root)
            if local is None:
                return (True, None)
            mutable = local.mutable
        return (mutable, place.root)

    # -- access control --------------------------------------------------

    def active_borrows(self, root: str, path: tuple, env: Env) -> list[Borrow]:
        return [b for b in env.borrows if b.root == root and _overlap(b.path, path)]

    def check_access(self, place: Place, need: str, env: Env, node) -> None:
        if place.via is not None:
            via = place.via
            if need == "move":
                self.error(f"cannot move out of the borrow '{via.name}'", node)
            if need == "write" and via.borrow_kind != "inout":
                self.error(
                    f"cannot modify through the shared borrow '{via.name}'; "
                    "declare it with 'inout' to allow mutation",
                    node,
                )
            return

        borrows = self.active_borrows(place.root, place.path, env)
        if need == "read":
            if any(b.kind in ("inout", "out") for b in borrows):
                self.error(
                    f"cannot read '{place.root}' while it is exclusively borrowed", node
                )
            return
        if borrows:
            action = "move" if need == "move" else "modify"
            self.error(
                f"cannot {action} '{place.root}' while it is borrowed", node
            )
        # Writing *through* a pointer writes the pointee, not the pointer
        # variable, so a read-only pointer parameter does not make the pointee
        # read-only.
        if need == "write" and place.path and place.path[0] == "*":
            local = env.locals.get(place.root)
            if local is not None and local.ty.kind in ("raw", "unique", "shared"):
                return
        mutable, _ = self.root_info(place, env)
        if not mutable:
            if need == "write":
                self.error(f"cannot modify read-only '{place.root}'", node)
            if need == "move":
                self.error(f"cannot move from read-only '{place.root}'", node)

    # -- expression checking --------------------------------------------

    def check_expr(self, expr, env: Env, mode: str = "value") -> None:
        if expr is None:
            return
        if isinstance(expr, (ast.Literal,)):
            return
        if isinstance(expr, ast.StringLiteral):
            for part in expr.parts:
                if isinstance(part, ast.StrInterp):
                    self.check_expr(part.expr, env)
            return
        if isinstance(expr, ast.Parenthesized):
            self.check_expr(expr.expr, env, mode)
            return
        if isinstance(expr, ast.UnsafeExpr):
            env.unsafe += 1
            try:
                self.check_expr(expr.expr, env, mode)
            finally:
                env.unsafe -= 1
            return
        if isinstance(expr, ast.OutArg):
            place = self.as_place(expr.expr, env)
            if place is None:
                self.error("an 'out' argument must be an lvalue", expr)
            self.check_access(place, "write", env, expr)
            return
        if isinstance(expr, ast.Move):
            place = self.as_place(expr.expr, env)
            if place is None:
                return
            self.check_access(place, "move", env, expr)
            self.mark_moved(place, env, expr)
            return
        if isinstance(expr, ast.Forward):
            place = self.as_place(expr.expr, env)
            if place is not None:
                self.check_access(place, "read", env, expr)
            return
        if isinstance(expr, ast.NameExpr):
            if len(expr.name.parts) == 1 and not expr.name.leading_global:
                name = expr.name.parts[0]
                if name in env.locals:
                    self.use_name(name, expr, env, mode)
                elif env.owner and any(m == name for m, _t in self.tables.data_members.get(env.owner, ())):
                    self.check_access(Place("this", (name,)), "read", env, expr)
            return
        if isinstance(expr, ast.Id):
            if expr.name == "_":
                return
            if expr.name == "this":
                place = Place("this")
                self.check_access(place, "read", env, expr)
                return
            self.use_name(expr.name, expr, env, mode)
            return
        if isinstance(expr, ast.Unary):
            self.check_expr(expr.expr, env)
            return
        if isinstance(expr, ast.Postfix):
            self.check_postfix(expr, env)
            return
        if isinstance(expr, ast.Binary):
            self.check_expr(expr.left, env)
            self.check_expr(expr.right, env)
            return
        if isinstance(expr, ast.CompareChain):
            self.check_expr(expr.first, env)
            for _op, operand in expr.rest:
                self.check_expr(operand, env)
            return
        if isinstance(expr, ast.Assign):
            self.check_assignment(expr, env)
            return
        if isinstance(expr, ast.Call):
            self.check_call(expr, env)
            return
        if isinstance(expr, ast.Member):
            place = self.as_place(expr, env)
            if place is not None:
                self.check_access(place, "read", env, expr)
            else:
                self.check_expr(expr.obj, env)
            return
        if isinstance(expr, ast.Subscript):
            place = self.as_place(expr, env)
            if place is not None:
                self.check_access(place, "read", env, expr)
            else:
                self.check_expr(expr.obj, env)
            self.check_expr(expr.index, env)
            return
        if isinstance(expr, ast.Is):
            self.check_expr(expr.expr, env)
            constraint = expr.constraint
            if isinstance(constraint, ast.Expr):
                self.check_expr(constraint, env)
            return
        if isinstance(expr, ast.As):
            self.check_expr(expr.expr, env)
            return
        if isinstance(expr, ast.Range):
            self.check_expr(expr.first, env)
            self.check_expr(expr.last, env)
            return
        if isinstance(expr, ast.Inspect):
            self.check_expr(expr.expr, env)
            for alt in expr.alts:
                self.check_expr(alt.value, env)
            return
        if isinstance(expr, ast.Temporary):
            self.check_expr(expr.init, env)
            return
        if isinstance(expr, ast.New):
            for arg in expr.args:
                self.check_expr(arg, env)
            return
        if isinstance(expr, ast.Fold):
            self.check_expr(expr.pack, env)
            self.check_expr(expr.init, env)
            return
        if isinstance(expr, ast.PackExpansion):
            self.check_expr(expr.expr, env)
            return
        if isinstance(expr, ast.SizeofPack):
            return
        if isinstance(expr, ast.CommaList):
            for item in expr.items:
                self.check_expr(item, env)
            return
        if isinstance(expr, ast.Lambda):
            self.check_lambda(expr, env)
            return
        # Unknown node: walk dataclass fields defensively.
        return

    # -- names -----------------------------------------------------------

    def use_name(self, name: str, node, env: Env, mode: str) -> None:
        local = env.locals.get(name)
        if local is None:
            return  # global / function name: not modelled
        if local.is_borrow:
            place = Place(local.borrow_root, local.borrow_path, via=local)
            self.check_access(place, "read" if mode != "move" else "move", env, node)
            return
        if local.moved:
            self.error(f"use of moved value '{name}'", node)
        if local.out_param and not local.initialized:
            self.error(
                f"'out' parameter '{name}' is read before it is assigned", node
            )
        if mode == "move":
            self.check_access(Place(name), "move", env, node)
            self.mark_moved(Place(name), env, node)
        elif mode == "write":
            self.check_access(Place(name), "write", env, node)
        else:
            self.check_access(Place(name), "read", env, node)

    # -- moves -----------------------------------------------------------

    def mark_moved(self, place: Place, env: Env, node) -> None:
        if place.path:
            self.error(
                f"cannot move part of '{place.root}'; only whole objects can be moved",
                node,
            )
        local = env.locals.get(place.root)
        if local is None:
            return
        if local.is_borrow:
            self.error(f"cannot move the borrow '{local.name}'", node)
        if local.kind == "param" and local.param_kind in ("in", "in_ref", "out"):
            self.error(f"cannot move from the '{local.param_kind}' parameter '{local.name}'", node)
        if local.loop_depth < env.loop_depth:
            self.error(
                f"cannot move '{local.name}' inside a loop; it may be used in a later iteration",
                node,
            )
        local.moved = True

    # -- postfix operators ----------------------------------------------

    def check_postfix(self, expr: ast.Postfix, env: Env) -> None:
        op = expr.op
        if op == "&":
            self.check_address_of(expr, env)
            return
        if op == "*":
            operand_ty = self.infer_ty(expr.expr, env)
            if operand_ty.kind == "raw" and env.unsafe == 0:
                self.error(
                    "dereferencing a raw pointer requires an 'unsafe' block or "
                    "an 'unsafe' function",
                    expr,
                )
            self.check_expr(expr.expr, env)
            place = self.as_place(expr, env)
            if place is not None:
                self.check_access(place, "read", env, expr)
            return
        if op in ("++", "--"):
            place = self.as_place(expr.expr, env)
            if place is not None:
                self.check_access(place, "write", env, expr)
            else:
                self.check_expr(expr.expr, env)
            return
        self.check_expr(expr.expr, env)

    def check_address_of(self, expr: ast.Postfix, env: Env) -> None:
        operand = expr.expr
        place = self.as_place(operand, env)
        if place is None:
            # Address of a function or a global: allowed (C++ interop, function
            # pointers are safe).
            if isinstance(operand, (ast.NameExpr, ast.Id)):
                return
            self.error("cannot take the address of a temporary value", expr)
            return
        if env.unsafe > 0:
            # Raw pointer creation: trust the programmer.
            self.check_access(place, "read", env, expr)
            return
        if place.root not in env.locals and place.root != "this":
            return  # global/function address
        self.error(
            "taking the address of a place requires a borrow type "
            "('in T' / 'inout T') or an 'unsafe' block",
            expr,
        )

    # -- assignments -----------------------------------------------------

    def check_assignment(self, expr: ast.Assign, env: Env) -> None:
        lhs = expr.lhs
        if isinstance(lhs, ast.Id) and lhs.name == "_":
            self.check_expr(expr.rhs, env)
            self.forbid_copy_of_move_only(expr.rhs, env, "discard")
            return
        place = self.as_place(lhs, env)
        if place is None:
            self.error("the left-hand side of an assignment must be an lvalue", lhs)
            self.check_expr(expr.rhs, env)
            return
        direct_borrow = None
        if isinstance(lhs, (ast.NameExpr, ast.Id)):
            name = lhs.name.parts[0] if isinstance(lhs, ast.NameExpr) else lhs.name
            local = env.locals.get(name)
            if local is not None and local.is_borrow:
                direct_borrow = local
        if direct_borrow is not None:
            if direct_borrow.borrow_kind != "inout":
                self.error(
                    f"cannot assign through the shared borrow '{direct_borrow.name}'; "
                    "declare it with 'inout' to allow mutation",
                    lhs,
                )
                return
            if expr.op != "=":
                self.check_access(place, "read", env, lhs)
            self.check_expr(expr.rhs, env)
            self.forbid_copy_of_move_only(expr.rhs, env, "assignment")
            return
        compound = expr.op != "="
        if compound:
            self.check_access(place, "read", env, lhs)
        self.check_access(place, "write", env, lhs)
        self.check_expr(expr.rhs, env)
        if expr.op == "=":
            self.forbid_copy_of_move_only(expr.rhs, env, "assignment")
            local = env.locals.get(place.root)
            if local is not None and not local.is_borrow:
                local.moved = False
                if not place.path:
                    local.initialized = True

    # -- calls -----------------------------------------------------------

    def check_call(self, node: ast.Call, env: Env) -> None:
        sig = self.resolve_call(node, env)
        member_call = self.is_member_call(node, env)
        if sig is None:
            self.check_unknown_call(node, env)
            return
        if sig.is_unsafe and env.unsafe == 0:
            self.error(
                f"calling the unsafe function '{sig.name}' requires an 'unsafe' "
                "block or an 'unsafe' function",
                node,
            )
        receiver_place = None
        receiver_expr = None
        if isinstance(node.callee, ast.Member):
            receiver_expr = node.callee.obj
            receiver_place = self.as_place(receiver_expr, env)
            if receiver_place is not None:
                self.check_access(receiver_place, "read", env, node)
            else:
                self.check_expr(receiver_expr, env)

        arg_exprs = list(node.args)
        if not member_call and isinstance(node.callee, ast.Member):
            # UFCS free function: the receiver becomes the first argument.
            receiver_ty = self.infer_ty(receiver_expr, env)
            params = [ParamSig("self", "in", receiver_ty)] + list(sig.params)
            arg_exprs = [receiver_expr] + arg_exprs
            arg_places = [receiver_place]
        else:
            params = list(sig.params)
            arg_places = []

        plan_borrows: list[tuple[Place, str]] = []
        plan_moves: list[Place] = []

        for arg in arg_exprs[len(arg_places):]:
            if isinstance(arg, ast.OutArg):
                place = self.as_place(arg.expr, env)
                arg_places.append(place)
                if place is not None:
                    self.check_access(place, "write", env, arg)
            elif isinstance(arg, ast.Move):
                place = self.as_place(arg.expr, env)
                arg_places.append(place)
                if place is not None:
                    self.check_access(place, "move", env, arg)
                    self.mark_moved(place, env, arg)
                    plan_moves.append(place)
            else:
                arg_places.append(self.as_place(arg, env))
                self.check_expr(arg, env)

        if member_call:
            if sig.this_kind in ("inout", "out"):
                if receiver_place is None:
                    self.error("this method call needs an lvalue receiver", node)
                else:
                    plan_borrows.append((receiver_place, "inout"))
            elif sig.this_kind == "in" and receiver_place is not None:
                plan_borrows.append((receiver_place, "in"))

        for i, param in enumerate(params):
            actual = arg_exprs[i] if i < len(arg_exprs) else None
            place = arg_places[i] if i < len(arg_places) else None
            if param.kind == "in" or param.kind == "in_ref":
                if isinstance(actual, ast.OutArg):
                    self.error("this parameter is read-only; remove 'out'", actual)
                if isinstance(actual, ast.Move):
                    self.error("an 'in' parameter does not take ownership; remove 'move'", actual)
                if place is not None:
                    plan_borrows.append((place, "in"))
            elif param.kind in ("inout", "out"):
                if isinstance(actual, ast.OutArg) and param.kind == "out":
                    pass  # explicit out marker, already checked
                if place is None:
                    self.error(
                        f"argument {i + 1} of '{sig.name}' is '{param.kind}' and "
                        "must be an lvalue",
                        node,
                    )
                else:
                    plan_borrows.append((place, param.kind))
                if isinstance(actual, ast.Move):
                    self.error("this parameter does not take ownership; remove 'move'", actual)
            elif param.kind == "move":
                if isinstance(actual, ast.Move) and place is not None:
                    pass  # already moved above
                elif place is not None:
                    self.check_access(place, "move", env, node)
                    self.mark_moved(place, env, node)
                    plan_moves.append(place)
            elif param.kind == "forward":
                pass
            elif param.kind == "copy":
                self.forbid_copy_of_move_only(actual, env, f"argument of '{sig.name}'")

        self.check_borrow_plans(plan_borrows, plan_moves, env, node)

    def check_borrow_plans(self, plans, moves, env: Env, node) -> None:
        for place, kind in plans:
            if place.via is not None:
                if kind in ("inout", "out") and place.via.borrow_kind != "inout":
                    self.error(
                        "cannot create an exclusive borrow through a shared borrow",
                        node,
                    )
                # Reborrows are allowed; do not conflict with their own borrow.
            existing = [
                b
                for b in env.borrows
                if b.root == place.root
                and _overlap(b.path, place.path)
                and b.holder != (place.via.name if place.via is not None else None)
            ]
            for b in existing:
                if b.kind in ("inout", "out") or kind in ("inout", "out"):
                    self.error(
                        f"'{place.root}' is already borrowed; exclusive and shared "
                        "borrows cannot overlap",
                        node,
                    )
            for other_place, other_kind in plans:
                if other_place is place:
                    continue
                if other_place.root == place.root and _overlap(other_place.path, place.path):
                    if other_kind in ("inout", "out") or kind in ("inout", "out"):
                        if other_place.via is place.via and place.via is not None:
                            continue
                        self.error(
                            f"'{place.root}' is aliased by two arguments with "
                            "conflicting borrows",
                            node,
                        )
            for moved in moves:
                if moved.root == place.root and _overlap(moved.path, place.path):
                    self.error(
                        f"'{place.root}' is both moved and borrowed by this call", node
                    )
            if kind in ("inout", "out"):
                mutable, _ = self.root_info(place, env)
                if not mutable:
                    self.error(f"cannot borrow read-only '{place.root}' exclusively", node)

    def check_unknown_call(self, node: ast.Call, env: Env) -> None:
        callee = node.callee
        if isinstance(callee, ast.Member):
            receiver = callee.obj
            receiver_place = self.as_place(receiver, env)
            if receiver_place is not None:
                self.check_access(receiver_place, "read", env, node)
            else:
                self.check_expr(receiver, env)
        else:
            self.check_expr(callee, env)
        for arg in node.args:
            if isinstance(arg, ast.OutArg):
                place = self.as_place(arg.expr, env)
                if place is not None:
                    self.check_access(place, "write", env, arg)
                continue
            self.check_expr(arg, env)
            self.forbid_copy_of_move_only(arg, env, "argument of an external function")

    # -- lambdas ---------------------------------------------------------

    def check_lambda(self, node: ast.Lambda, env: Env) -> None:
        captured = _captured_names(node.body)
        for name in captured:
            local = env.locals.get(name)
            if local is None:
                continue
            if local.is_borrow:
                self.error(
                    f"a function expression cannot capture the borrow '{name}'", node
                )
            if local.moved:
                self.error(f"a function expression cannot capture the moved value '{name}'", node)
            if local.ty.move_only:
                self.error(
                    f"a function expression cannot capture the move-only value "
                    f"'{name}'; use an explicit 'move' at the call site",
                    node,
                )

    # -- copy / move-only checks ----------------------------------------

    def forbid_copy_of_move_only(self, expr, env: Env, context: str) -> None:
        if expr is None:
            return
        if isinstance(expr, (ast.Move, ast.Forward)):
            return
        if isinstance(expr, ast.Parenthesized):
            self.forbid_copy_of_move_only(expr.expr, env, context)
            return
        ty = self.infer_ty(expr, env)
        if not ty.move_only:
            return
        place = self.as_place(expr, env)
        if place is None:
            return  # a temporary is fine
        self.error(
            f"'{place.root}' has type 'unique<...>' and is move-only; use "
            f"'(move {place.root})' to transfer ownership in this {context}",
            expr,
        )

    # -- statements ------------------------------------------------------

    def check_statements(self, stmts: list, env: Env) -> None:
        for stmt in stmts:
            self.check_stmt(stmt, env)

    def check_stmt(self, stmt, env: Env) -> None:
        if isinstance(stmt, ast.Block):
            env.push_scope()
            try:
                self.check_statements(stmt.stmts, env)
            finally:
                env.pop_scope()
            return
        if isinstance(stmt, ast.UnsafeBlock):
            env.unsafe += 1
            try:
                if isinstance(stmt.stmt, ast.Block):
                    env.push_scope()
                    try:
                        self.check_statements(stmt.stmt.stmts, env)
                    finally:
                        env.pop_scope()
                else:
                    self.check_stmt(stmt.stmt, env)
            finally:
                env.unsafe -= 1
            return
        if isinstance(stmt, ast.ExprStmt):
            self.check_expr(stmt.expr, env)
            return
        if isinstance(stmt, ast.VarDecl):
            self.check_var_decl(stmt, env)
            return
        if isinstance(stmt, ast.Return):
            self.check_return(stmt, env)
            return
        if isinstance(stmt, ast.If):
            self.check_expr(stmt.cond, env)
            self._branch_check(stmt.then, stmt.else_, env)
            return
        if isinstance(stmt, ast.While):
            self.check_expr(stmt.cond, env)
            env.push_scope()
            env.loop_depth += 1
            try:
                self.check_statements(stmt.body.stmts, env)
            finally:
                env.loop_depth -= 1
                env.pop_scope()
            if stmt.next_expr is not None:
                self.check_expr(stmt.next_expr, env)
            return
        if isinstance(stmt, ast.DoWhile):
            env.push_scope()
            env.loop_depth += 1
            try:
                self.check_statements(stmt.body.stmts, env)
            finally:
                env.loop_depth -= 1
                env.pop_scope()
            if stmt.next_expr is not None:
                self.check_expr(stmt.next_expr, env)
            self.check_expr(stmt.cond, env)
            return
        if isinstance(stmt, ast.For):
            self.check_expr(stmt.range, env)
            env.push_scope()
            env.loop_depth += 1
            try:
                param = stmt.param
                if param is not None and param.name:
                    self._declare_param_local(param, env)
                if isinstance(stmt.body, ast.Block):
                    self.check_statements(stmt.body.stmts, env)
                else:
                    self.check_stmt(stmt.body, env)
            finally:
                env.loop_depth -= 1
                env.pop_scope()
            if stmt.next_expr is not None:
                self.check_expr(stmt.next_expr, env)
            return
        if isinstance(stmt, ast.TryCatch):
            self.check_statements(stmt.body.stmts, env)
            for catch in stmt.catches:
                env.push_scope()
                try:
                    if catch.param is not None and catch.param.name:
                        self._declare_param_local(catch.param, env)
                    self.check_statements(catch.body.stmts, env)
                finally:
                    env.pop_scope()
            return
        if isinstance(stmt, ast.Throw):
            self.check_expr(stmt.expr, env)
            return
        if isinstance(stmt, ast.Assert):
            self.check_expr(stmt.cond, env)
            self.check_expr(stmt.message, env)
            return
        if isinstance(stmt, ast.RaiiScope):
            env.push_scope()
            try:
                for p in stmt.params:
                    if p.name:
                        self.check_expr(p.init, env)
                        env.declare(p.name, self.resolver.ty_of(p.type) if p.type is not None else UNKNOWN)
                self.check_stmt(stmt.body, env)
            finally:
                env.pop_scope()
            return
        # break / continue / using / preproc: nothing to check
        return

    def _declare_param_local(self, param: ast.Param, env: Env) -> None:
        ty = self.resolver.ty_of(param.type) if param.type is not None else UNKNOWN
        kind = _param_kind(param)
        env.declare(
            param.name,
            ty,
            kind="local",
            mutable=True,
        )

    def _branch_check(self, then_branch, else_branch, env: Env) -> None:
        snapshot = {name: (l.moved, l.initialized) for name, l in env.locals.items()}
        env.push_scope()
        try:
            self.check_statements(then_branch.stmts, env)
        finally:
            env.pop_scope()
        then_state = {name: (l.moved, l.initialized) for name, l in env.locals.items()}
        for name, state in snapshot.items():
            if name in env.locals:
                env.locals[name].moved, env.locals[name].initialized = state
        if else_branch is not None:
            env.push_scope()
            try:
                if isinstance(else_branch, ast.If):
                    self.check_stmt(else_branch, env)
                else:
                    self.check_statements(else_branch.stmts, env)
            finally:
                env.pop_scope()
            else_state = {name: (l.moved, l.initialized) for name, l in env.locals.items()}
            for name, state in snapshot.items():
                local = env.locals.get(name)
                if local is None:
                    continue
                then_value = then_state.get(name, state)
                else_value = else_state.get(name, state)
                local.moved = then_value[0] and else_value[0]
                local.initialized = then_value[1] and else_value[1]
        else:
            for name, state in snapshot.items():
                local = env.locals.get(name)
                if local is None:
                    continue
                then_value = then_state.get(name, state)
                local.moved = state[0] and then_value[0]
                local.initialized = state[1] and then_value[1]

    def check_var_decl(self, decl: ast.VarDecl, env: Env) -> None:
        if decl.name is None:
            return
        ty = self.resolver.ty_of(decl.type) if decl.type is not None else None
        deduced_amp = (
            decl.type is None
            and isinstance(decl.init, ast.Postfix)
            and decl.init.op == "&"
        )
        if isinstance(decl.type, ast.BorrowType) or deduced_amp or (
            decl.type is None and decl.init is not None and self._borrow_init_kind(decl.init, env)
        ):
            kind = None
            if isinstance(decl.type, ast.BorrowType):
                kind = decl.type.kind
                if kind == "out":
                    self.error("'out' cannot be used as a local borrow type", decl)
            else:
                kind = self._borrow_init_kind(decl.init, env)
            self.check_borrow_decl(decl, kind, env)
            return
        if decl.init is not None:
            self.check_expr(decl.init, env)
            self.forbid_copy_of_move_only(decl.init, env, "initialiser")
        if ty is None and decl.init is not None:
            ty = self.infer_ty(decl.init, env)
        env.declare(
            decl.name,
            ty if ty is not None else UNKNOWN,
            mutable=True,
        )

    def _borrow_init_kind(self, init, env: Env):
        if isinstance(init, ast.Postfix) and init.op == "&":
            return None  # kind comes from the type; deduced borrow gets default 'in'
        if isinstance(init, ast.Call):
            sig = self.resolve_call(init, env)
            if sig is not None and sig.ret.kind == "borrow":
                return sig.ret.borrow
        return None

    def check_borrow_decl(self, decl: ast.VarDecl, kind: str, env: Env) -> None:
        init = decl.init
        if init is None:
            self.error(f"borrow '{decl.name}' needs an initial value", decl)
            return
        root = None
        path = ()
        if isinstance(init, ast.Postfix) and init.op == "&":
            place = self.as_place(init.expr, env)
            if place is None:
                self.error(
                    "a borrow must be created from a place (a local variable, "
                    "parameter or member), not a temporary",
                    init,
                )
                return
            root, path = place.root, place.path
            if isinstance(decl.type, ast.BorrowType):
                kind = decl.type.kind
                self.info.deduced_borrow[id(decl)] = kind
            else:
                self.info.deduced_borrow[id(decl)] = kind or "in"
        elif isinstance(init, ast.Call):
            sig = self.resolve_call(init, env)
            if sig is not None and sig.ret.kind == "borrow":
                if isinstance(decl.type, ast.BorrowType):
                    kind = decl.type.kind
                else:
                    kind = sig.ret.borrow or "in"
                self.info.deduced_borrow[id(decl)] = kind
                root = self._borrow_return_root(sig, init, env)
            else:
                self.error(
                    "a borrow must be initialised from a place or a function "
                    "that returns a borrow",
                    init,
                )
                return
        elif isinstance(init, (ast.NameExpr, ast.Id, ast.Member, ast.Subscript)):
            place = self.as_place(init, env)
            if place is None:
                self.error("a borrow must be initialised from a borrow or a place", init)
                return
            root, path = place.root, place.path
            if isinstance(decl.type, ast.BorrowType):
                kind = decl.type.kind
            else:
                kind = place.via.borrow_kind if place.via is not None else "in"
            self.info.deduced_borrow[id(decl)] = kind
        else:
            self.error(
                "a borrow must be initialised from a place or a function that "
                "returns a borrow",
                init,
            )
            return

        kind = kind or "in"
        if kind == "out":
            self.error("'out' cannot be used as a local borrow kind", decl)
        if root is None:
            local = env.declare(decl.name, Ty("borrow", borrow=kind), mutable=(kind == "inout"))
            local.is_borrow = True
            local.borrow_kind = kind
            self.info.borrow_names.setdefault(id(env.func), set()).add(decl.name)
            return
        self.validate_borrow_root(root, path, kind, env, decl)
        self.check_new_borrow(root, path, kind, env, decl, holder=decl.name)
        local = env.declare(
            decl.name,
            Ty("borrow", borrow=kind),
            mutable=False,
        )
        local.is_borrow = True
        local.borrow_kind = kind
        local.borrow_root = root
        local.borrow_path = path
        env.borrows.append(Borrow(root, path, kind, decl.name, env.depth, decl))
        self.info.borrow_names.setdefault(id(env.func), set()).add(decl.name)

    def validate_borrow_root(self, root: str, path: tuple, kind: str, env: Env, node) -> None:
        if root == "this":
            if kind == "inout" and env.this_kind not in ("inout", "out"):
                self.error("cannot create an exclusive borrow of a read-only 'this'", node)
            return
        local = env.locals.get(root)
        if local is None:
            return  # global: not modelled
        if local.kind == "local" and local.scope_depth > env.depth:
            self.error(f"borrow of '{root}' outlives its owner", node)
        if kind == "inout" and not local.mutable:
            self.error(f"cannot create an exclusive borrow of read-only '{root}'", node)
        if local.moved:
            self.error(f"cannot borrow the moved value '{root}'", node)
        if local.kind == "local" and local.scope_depth > env.depth:
            self.error(f"borrow of '{root}' outlives its owner", node)

    def check_new_borrow(self, root, path, kind, env, node, holder=None) -> None:
        for b in env.borrows:
            if b.root == root and _overlap(b.path, path) and b.holder != holder:
                if b.kind in ("inout", "out") or kind in ("inout", "out"):
                    self.error(
                        f"cannot borrow '{root}' here: it is already borrowed "
                        f"({'exclusively' if b.kind in ('inout', 'out') else 'shared'})",
                        node,
                    )

    def _borrow_return_root(self, sig: Signature, call: ast.Call, env: Env):
        source = sig.ret.source
        if source is not None:
            for i, param in enumerate(sig.params):
                if param.name == source and i < len(call.args):
                    place = self.as_place(call.args[i], env)
                    return place.root if place is not None else None
            return None
        borrow_params = [i for i, p in enumerate(sig.params) if p.kind in ("in", "inout")]
        if len(borrow_params) == 1:
            place = self.as_place(call.args[borrow_params[0]], env)
            return place.root if place is not None else None
        return None

    # -- returns ---------------------------------------------------------

    def check_return(self, stmt: ast.Return, env: Env) -> None:
        func = env.func
        r = func.returns
        is_borrow = r is not None and getattr(r, "borrow_kind", None)
        if is_borrow:
            self.check_borrow_return(stmt, env, r)
        else:
            if stmt.expr is not None:
                self.check_expr(stmt.expr, env)
                # Returning a value by value implicitly transfers ownership.
                if not isinstance(stmt.expr, (ast.Move, ast.Forward)):
                    ty = self.infer_ty(stmt.expr, env)
                    place = self.as_place(stmt.expr, env)
                    if ty.move_only and place is not None:
                        self.mark_moved(place, env, stmt.expr)
        self.check_out_params_initialized(env, stmt)

    def check_borrow_return(self, stmt: ast.Return, env: Env, r) -> None:
        if stmt.expr is None:
            self.error("a function returning a borrow must return a value", stmt)
            return
        expr = stmt.expr
        if isinstance(expr, ast.Postfix) and expr.op == "&":
            expr = expr.expr
        place = self.as_place(expr, env)
        if place is None:
            if isinstance(expr, ast.Call):
                return  # reborrow from a function whose contract we trust
            self.error("a borrowed return value must come from a place", stmt)
            return
        root = place.root
        kind = r.borrow_kind
        source = r.borrow_source
        if root == "this":
            if env.this_kind not in ("in", "inout"):
                self.error("'this' is not borrowable here", stmt)
            if kind == "inout" and env.this_kind != "inout":
                self.error("cannot return an exclusive borrow of read-only 'this'", stmt)
            if source is not None and source != "this":
                self.error(f"the borrow source is 'this', not '{source}'", stmt)
            return
        local = env.locals.get(root)
        if local is None:
            return
        if local.kind == "local":
            self.error(
                f"cannot return a borrow of the local variable '{root}'; it is "
                "destroyed when the function returns",
                stmt,
            )
            return
        if local.is_borrow:
            # A borrow local: find its owner; the owner must itself be borrowable.
            self.error(
                "cannot return a borrow variable directly; return the borrowed "
                "place instead",
                stmt,
            )
            return
        if local.kind == "param":
            if local.param_kind not in ("in", "inout", "in_ref"):
                self.error(
                    f"cannot return a borrow of the owned parameter '{root}'",
                    stmt,
                )
                return
            if kind == "inout" and local.param_kind != "inout":
                self.error(
                    f"cannot return an exclusive borrow of the shared parameter '{root}'",
                    stmt,
                )
        if source is not None and root != source:
            self.error(
                f"the returned borrow comes from '{root}', but the return type "
                f"names '{source}'",
                stmt,
            )
        if source is None and local.kind == "param":
            borrow_params = [
                p.name
                for p in env.func.params
                if p.name and _param_kind(p) in ("in", "inout", "in_ref")
            ]
            if env.this_kind in ("in", "inout"):
                borrow_params.append("this")
            if len(borrow_params) > 1:
                self.error(
                    "the returned borrow is ambiguous; annotate the return type "
                    "with its source, e.g. '-> in(param) T'",
                    stmt,
                )

    def check_out_params_initialized(self, env: Env, node) -> None:
        for name, local in env.locals.items():
            if local.out_param and not local.initialized:
                self.error(
                    f"'out' parameter '{name}' is not assigned on this path", node
                )

    # -- functions -------------------------------------------------------

    def check_function(self, decl: ast.FunctionDecl, owner: Optional[str]) -> None:
        if decl.body is None:
            return
        this_kind = _this_kind(decl)
        env = Env(decl, owner, this_kind, self.tables, self.resolver, self.info)
        if decl.params and decl.params[0].is_this:
            env.locals["this"] = LocalState(
                "this",
                Ty("value", name=owner or ""),
                kind="this",
                mutable=this_kind in ("inout", "out"),
            )
        for p in decl.params:
            if p.is_this:
                continue
            if not p.name or p.is_wildcard:
                continue
            kind = _param_kind(p)
            ty = self.resolver.ty_of(p.type)
            mutable = kind not in ("in", "in_ref")
            env.locals[p.name] = LocalState(
                p.name, ty, kind="param", param_kind=kind, mutable=mutable
            )
        body = decl.body.stmt
        if isinstance(body, ast.Block):
            self.check_statements(body.stmts, env)
        elif isinstance(body, ast.ExprStmt):
            r = decl.returns
            if r is not None and getattr(r, "borrow_kind", None):
                synthetic = ast.Return(
                    expr=body.expr,
                    line=getattr(body.expr, "line", 0),
                    col=getattr(body.expr, "col", 0),
                )
                self.check_borrow_return(synthetic, env, r)
            else:
                self.check_expr(body.expr, env)
        self.check_out_params_initialized(env, decl)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _captured_names(node) -> set[str]:
    import dataclasses

    names: set[str] = set()

    def walk(value):
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
            return
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            if isinstance(value, ast.NameExpr) and len(value.name.parts) == 1:
                names.add(value.name.parts[0])
            if isinstance(value, ast.Id):
                names.add(value.name)
            for field in dataclasses.fields(value):
                walk(getattr(value, field.name))

    walk(node)
    return names


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------


def build_tables(tu: ast.TranslationUnit) -> Tables:
    tables = Tables()
    resolver_holder = TypeResolver(tables)

    def collect(items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                collect(item.members)
            elif isinstance(item, ast.TypeDecl):
                if item.kind == "==":
                    if not item.template_params:
                        tables.aliases[item.name] = item.alias
                else:
                    tables.types[item.name] = item
                    members = []
                    for member in item.members:
                        if isinstance(member, ast.VarDecl) and member.name != "this":
                            members.append((member.name, member.type))
                    tables.data_members[item.name] = members

    collect(tu.items)

    # Second pass: signatures (aliases are known now).
    def collect_functions(items: list, owner: Optional[str] = None) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                collect_functions(item.members, owner)
            elif isinstance(item, ast.FunctionDecl):
                checker = _SigBuilder(tables, resolver_holder)
                sig = checker.signature_of(item, owner)
                if owner is not None:
                    tables.methods.setdefault((owner, item.name or ""), []).append(sig)
                elif item.is_operator and item.operator == "=" and _this_kind(item) == "out":
                    tables.constructors.setdefault(owner or "", []).append(sig)
                elif item.name:
                    tables.free_functions.setdefault(item.name, []).append(sig)
            elif isinstance(item, ast.TypeDecl) and item.kind == "=":
                for member in item.members:
                    if isinstance(member, ast.FunctionDecl):
                        checker = _SigBuilder(tables, resolver_holder)
                        sig = checker.signature_of(member, item.name)
                        if member.is_operator and member.operator == "=" and _this_kind(member) == "out":
                            tables.constructors.setdefault(item.name, []).append(sig)
                        elif not member.is_operator:
                            tables.methods.setdefault((item.name, member.name or ""), []).append(sig)

    collect_functions(tu.items)
    return tables


class _SigBuilder(Checker):
    def __init__(self, tables, resolver):
        super().__init__(tables, resolver, OwnershipInfo(), "<input>", enabled=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def analyze(tu: ast.TranslationUnit, filename: str = "<input>",
            enabled: bool = True) -> OwnershipInfo:
    """Check ownership/borrowing and return code-generation annotations."""

    tables = build_tables(tu)
    resolver = TypeResolver(tables)
    info = OwnershipInfo()
    checker = Checker(tables, resolver, info, filename, enabled)

    # Data members may not be borrows.
    for type_decl in tables.types.values():
        for member in type_decl.members:
            if isinstance(member, ast.VarDecl) and isinstance(member.type, ast.BorrowType):
                checker.error(
                    f"data member '{member.name}' cannot be a borrow; borrows are "
                    "only valid as parameters, returns and local variables",
                    member,
                )

    def walk(items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                walk(item.members)
            elif isinstance(item, ast.FunctionDecl):
                checker.check_function(item, None)
            elif isinstance(item, ast.TypeDecl) and item.kind == "=":
                for member in item.members:
                    if isinstance(member, ast.FunctionDecl):
                        checker.check_function(member, item.name)

    walk(tu.items)
    return info
