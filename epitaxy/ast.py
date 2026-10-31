"""Abstract syntax tree for Epitaxy.

The AST is deliberately explicit: every syntactic construct of the language has
a node, because the parser is context-free and the code generator must never
perform name lookup to decide how to interpret a construct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Source positions
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class Node:
    line: int = 0
    col: int = 0


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class Name(Node):
    """A possibly qualified name such as ``std::vector`` or ``::foo``."""

    parts: list[str] = field(default_factory=list)
    leading_global: bool = False

    def text(self) -> str:
        prefix = "::" if self.leading_global else ""
        return prefix + "::".join(self.parts)


@dataclass(kw_only=True)
class NamedType(Node):
    name: Name
    args: Optional[list] = None  # list[TypeExpr | Expr]


@dataclass(kw_only=True)
class PointerType(Node):
    inner: object


@dataclass(kw_only=True)
class ConstType(Node):
    inner: object


@dataclass(kw_only=True)
class DeducedType(Node):
    """``_`` in a type position."""


@dataclass(kw_only=True)
class BorrowType(Node):
    """A borrow type: ``in T`` (shared) or ``inout T`` (exclusive)."""

    kind: str = "in"
    inner: object = None


@dataclass(kw_only=True)
class FunctionType(Node):
    params: list = field(default_factory=list)
    returns: object = None


@dataclass(kw_only=True)
class TypeOfType(Node):
    expr: object


@dataclass(kw_only=True)
class ConstrainedType(Node):
    base: object
    constraint: object  # Expr


@dataclass(kw_only=True)
class TemplateParam(Node):
    name: str = ""
    is_type: bool = False
    type: object = None  # TypeExpr for non-type parameters
    default: object = None  # Expr
    variadic: bool = False


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class Preproc(Node):
    text: str


@dataclass(kw_only=True)
class UsingDecl(Node):
    target: str
    is_namespace: bool = False


@dataclass(kw_only=True)
class Param(Node):
    name: Optional[str] = None
    type: object = None  # TypeExpr | None (deduced)
    qualifiers: list[str] = field(default_factory=list)  # in/inout/out/copy/move/forward...
    default: object = None  # Expr
    variadic: bool = False
    is_this: bool = False
    is_that: bool = False
    is_wildcard: bool = False


@dataclass(kw_only=True)
class Contract(Node):
    kind: str = "pre"  # pre | post
    cond: object = None  # Expr
    message: object = None  # StringLit/Expr | None
    groups: list = field(default_factory=list)  # list[Expr]
    predicates: list = field(default_factory=list)  # list[Expr]


@dataclass(kw_only=True)
class NamedReturn(Node):
    name: str = ""
    type: object = None
    kind: str = "out"  # out | forward
    line: int = 0
    col: int = 0


@dataclass(kw_only=True)
class Returns(Node):
    kind: str = "void"  # void | single | named
    type: object = None  # TypeExpr (single)
    named: list = field(default_factory=list)  # list[NamedReturn]
    forward: bool = False
    forward_ref: bool = False
    borrow_kind: Optional[str] = None  # in | inout | out for borrow returns
    borrow_source: Optional[str] = None  # explicit source parameter name


@dataclass(kw_only=True)
class Body(Node):
    stmt: object  # Block or ExprStmt


@dataclass(kw_only=True)
class Decl(Node):
    pass


@dataclass(kw_only=True)
class FunctionDecl(Decl):
    name: Optional[str] = None  # None for anonymous function expressions
    template_params: list = field(default_factory=list)
    params: list = field(default_factory=list)
    returns: Optional[Returns] = None
    contracts: list = field(default_factory=list)
    requires: object = None  # Expr
    body: Optional[Body] = None
    kind: str = "="  # "=" or "=="
    is_operator: bool = False  # operator=
    operator: str = ""  # "=" for operator=
    access: Optional[str] = None  # public/private/protected at type scope
    is_declaration_only: bool = False
    is_unsafe: bool = False


@dataclass(kw_only=True)
class TypeDecl(Decl):
    name: str = ""
    template_params: list = field(default_factory=list)
    metafunctions: list = field(default_factory=list)  # e.g. ["enum"]
    kind: str = "="  # "=" or "=="
    members: list = field(default_factory=list)  # list[Decl]
    alias: object = None  # TypeExpr when kind == "=="
    access: Optional[str] = None


@dataclass(kw_only=True)
class NamespaceDecl(Decl):
    name: str = ""
    kind: str = "="  # "=" or "=="
    members: list = field(default_factory=list)
    alias: Optional[Name] = None
    access: Optional[str] = None


@dataclass(kw_only=True)
class Enumerator(Decl):
    """An enumerator inside an @enum / @flag_enum type body."""

    name: str = ""
    value: object = None  # Expr | None (auto-incrementing)


@dataclass(kw_only=True)
class VarDecl(Decl):
    name: Optional[str] = None  # None for unnamed expression-local object
    template_params: list = field(default_factory=list)
    type: object = None  # TypeExpr | None
    init: object = None  # Expr | InitList | None
    alias: bool = False  # "=="
    constraint: object = None  # Expr from "is"
    variadic: bool = False
    access: Optional[str] = None
    deduced: bool = False  # from ":=" form


# ---------------------------------------------------------------------------
# Initializers
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class InitList(Node):
    items: list = field(default_factory=list)


@dataclass(kw_only=True)
class DesignatedInit(Node):
    name: str = ""
    value: object = None


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class Block(Node):
    stmts: list = field(default_factory=list)


@dataclass(kw_only=True)
class ExprStmt(Node):
    expr: object = None


@dataclass(kw_only=True)
class Return(Node):
    expr: object = None


@dataclass(kw_only=True)
class If(Node):
    cond: object = None
    then: object = None
    else_: object = None


@dataclass(kw_only=True)
class While(Node):
    cond: object = None
    body: object = None
    next_expr: object = None
    label: Optional[str] = None


@dataclass(kw_only=True)
class DoWhile(Node):
    body: object = None
    next_expr: object = None
    cond: object = None
    label: Optional[str] = None


@dataclass(kw_only=True)
class For(Node):
    range: object = None  # Expr (range expression)
    next_expr: object = None
    param: object = None  # Param
    body: object = None
    label: Optional[str] = None


@dataclass(kw_only=True)
class Break(Node):
    label: Optional[str] = None


@dataclass(kw_only=True)
class Continue(Node):
    label: Optional[str] = None


@dataclass(kw_only=True)
class Catch(Node):
    param: Optional[Param] = None
    body: object = None
    is_catchall: bool = False


@dataclass(kw_only=True)
class TryCatch(Node):
    body: object = None
    catches: list = field(default_factory=list)


@dataclass(kw_only=True)
class Throw(Node):
    expr: object = None


@dataclass(kw_only=True)
class Assert(Node):
    cond: object = None
    message: object = None
    groups: list = field(default_factory=list)
    predicates: list = field(default_factory=list)


@dataclass(kw_only=True)
class UnsafeBlock(Node):
    """``unsafe { ... }``: a scope in which raw-pointer operations are allowed."""

    stmt: object = None


@dataclass(kw_only=True)
class RaiiScope(Node):
    """``(x: T = init) statement`` -- a local parameterised block."""

    params: list = field(default_factory=list)
    body: object = None


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class Expr(Node):
    pass


@dataclass(kw_only=True)
class Literal(Expr):
    kind: str = "number"  # number | char | bool | null
    raw: str = ""


@dataclass(kw_only=True)
class StringLiteral(Expr):
    parts: list = field(default_factory=list)  # list[StrLit | StrInterp]
    prefix: str = ""
    raw_string: bool = False
    raw_text: str = ""


@dataclass(kw_only=True)
class StrLit:
    text: str = ""  # text as it should appear in generated C++ (escapes preserved)


@dataclass(kw_only=True)
class StrInterp:
    expr: object = None
    spec: Optional[str] = None
    line: int = 0
    col: int = 0


@dataclass(kw_only=True)
class Id(Expr):
    name: str = ""


@dataclass(kw_only=True)
class NameExpr(Expr):
    name: Name
    template_args: Optional[list] = None


@dataclass(kw_only=True)
class QualifiedNameExpr(Expr):
    """A qualified name with template arguments on inner segments.

    Example: ``std::numeric_limits<double>::quiet_NaN``.
    """

    segments: list = field(default_factory=list)  # list[(name, args | None)]


@dataclass(kw_only=True)
class Unary(Expr):
    op: str = ""
    expr: object = None


@dataclass(kw_only=True)
class Postfix(Expr):
    op: str = ""  # * & ~ ++ -- $
    expr: object = None


@dataclass(kw_only=True)
class Binary(Expr):
    op: str = ""
    left: object = None
    right: object = None


@dataclass(kw_only=True)
class CompareChain(Expr):
    first: object = None
    rest: list = field(default_factory=list)  # list[(op, Expr)]


@dataclass(kw_only=True)
class Assign(Expr):
    op: str = "="
    lhs: object = None
    rhs: object = None


@dataclass(kw_only=True)
class Call(Expr):
    callee: object = None
    args: list = field(default_factory=list)
    template_args: Optional[list] = None


@dataclass(kw_only=True)
class Member(Expr):
    obj: object = None
    name: str = ""
    member_only: bool = False
    template_args: Optional[list] = None


@dataclass(kw_only=True)
class Subscript(Expr):
    obj: object = None
    index: object = None


@dataclass(kw_only=True)
class Is(Expr):
    expr: object = None
    constraint: object = None  # TypeExpr | Expr | IsWildcard


@dataclass(kw_only=True)
class IsWildcard(Node):
    pass


@dataclass(kw_only=True)
class As(Expr):
    expr: object = None
    type: object = None


@dataclass(kw_only=True)
class Range(Expr):
    first: object = None
    last: object = None
    closed: bool = False


@dataclass(kw_only=True)
class InspectAlt(Node):
    constraint: object = None  # TypeExpr | Expr | IsWildcard
    value: object = None  # Expr


@dataclass(kw_only=True)
class Inspect(Expr):
    expr: object = None
    type: object = None  # TypeExpr
    alts: list = field(default_factory=list)


@dataclass(kw_only=True)
class Lambda(Expr):
    template_params: list = field(default_factory=list)
    params: list = field(default_factory=list)
    returns: Optional[Returns] = None
    body: object = None  # Body
    kind: str = "="  # "=" or "=="


@dataclass(kw_only=True)
class Temporary(Expr):
    type: object = None
    init: object = None


@dataclass(kw_only=True)
class New(Expr):
    type: object = None
    args: list = field(default_factory=list)
    ownership: str = "unique"  # unique | shared


@dataclass(kw_only=True)
class Move(Expr):
    expr: object = None


@dataclass(kw_only=True)
class Forward(Expr):
    expr: object = None


@dataclass(kw_only=True)
class Fold(Expr):
    op: str = ""
    pack: object = None
    init: object = None
    left: bool = False


@dataclass(kw_only=True)
class PackExpansion(Expr):
    expr: object = None


@dataclass(kw_only=True)
class Parenthesized(Expr):
    expr: object = None


@dataclass(kw_only=True)
class UnsafeExpr(Expr):
    """``unsafe(expr)``: an expression evaluated in an unsafe context."""

    expr: object = None


@dataclass(kw_only=True)
class OutArg(Expr):
    """``f(out x)``: mark an argument as written by the callee."""

    expr: object = None


@dataclass(kw_only=True)
class SizeofPack(Expr):
    expr: object = None


@dataclass(kw_only=True)
class CommaList(Expr):
    items: list = field(default_factory=list)


@dataclass(kw_only=True)
class TranslationUnit(Node):
    items: list = field(default_factory=list)
