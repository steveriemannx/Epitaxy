"""Recursive-descent parser for Epitaxy.

The grammar is deliberately context free: a declaration is always
``name : kind = value``, types are written left to right, and the parser never
needs to perform name lookup.  A small ``stop`` set is threaded through the
expression parser so that constructs such as template argument lists and
``inspect`` alternatives have unambiguous boundaries.
"""

from __future__ import annotations

from typing import Optional

from . import ast
from .diag import CompileError
from .lexer import (
    AT,
    CHAR,
    EOF,
    IDENT,
    NUMBER,
    OP,
    PREPROC,
    STRING,
    Interp,
    Lexer,
    Token,
)

ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|="}
EQUALITY_OPS = {"==", "!="}
RELATIONAL_OPS = {"<", ">", "<=", ">="}
SHIFT_OPS = {"<<", ">>"}
ADDITIVE_OPS = {"+", "-"}
MULTIPLICATIVE_OPS = {"*", "/", "%"}

PARAM_QUALIFIERS = {
    "in",
    "inout",
    "out",
    "copy",
    "move",
    "forward",
    "in_ref",
    "forward_ref",
    "virtual",
    "override",
    "final",
    "implicit",
}

ACCESS_KEYWORDS = {"public", "private", "protected"}

STATEMENT_KEYWORDS = {
    "if",
    "while",
    "do",
    "for",
    "return",
    "break",
    "continue",
    "try",
    "throw",
    "assert",
    "using",
    "inspect",
}

TOKEN_EOF_TEXT = ""


def _mk_eof(line: int, col: int) -> Token:
    return Token(kind=EOF, text="", line=line, col=col)


class Parser:
    def __init__(self, tokens: list[Token], filename: str = "<input>"):
        self.tokens = tokens
        self.pos = 0
        self.filename = filename
        self._stop_stack: list[frozenset[str]] = []

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    @property
    def tok(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, k: int = 1) -> Token:
        i = self.pos + k
        if i >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[i]

    def kind(self, k: int = 0) -> str:
        return self.peek(k).kind if k else self.tok.kind

    def text(self, k: int = 0) -> str:
        t = self.peek(k) if k else self.tok
        return t.text

    def advance(self) -> Token:
        t = self.tok
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return t

    def at(self, text: str) -> bool:
        return self.tok.text == text

    def at_ident(self, name: str) -> bool:
        return self.tok.kind == IDENT and self.tok.text == name

    def at_kw(self, name: str) -> bool:
        """True for an unescaped contextual keyword."""
        return self.tok.kind == IDENT and self.tok.text == name and not self.tok.escaped_ident

    def match(self, text: str) -> bool:
        if self.at(text):
            self.advance()
            return True
        return False

    def match_ident(self, name: str) -> bool:
        if self.at_ident(name):
            self.advance()
            return True
        return False

    def expect(self, text: str, what: Optional[str] = None) -> Token:
        if not self.at(text):
            want = what or repr(text)
            raise self.error(f"expected {want}, but found {self.tok.describe()}")
        return self.advance()

    def expect_ident(self, what: str = "an identifier") -> Token:
        if self.tok.kind != IDENT:
            raise self.error(f"expected {what}, but found {self.tok.describe()}")
        return self.advance()

    def error(self, message: str, tok: Optional[Token] = None) -> CompileError:
        t = tok or self.tok
        return CompileError(message, t.line, t.col, self.filename)

    @property
    def _stop(self) -> frozenset[str]:
        return self._stop_stack[-1] if self._stop_stack else frozenset()

    def _at_stop(self) -> bool:
        return self.tok.text in self._stop

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def parse_translation_unit(self) -> ast.TranslationUnit:
        items = []
        start = self.tok
        while self.kind() != EOF:
            if self.kind() == PREPROC:
                t = self.advance()
                items.append(ast.Preproc(text=t.text, line=t.line, col=t.col))
                continue
            items.append(self.parse_declaration(scope="namespace"))
        return ast.TranslationUnit(items=items, line=start.line, col=start.col)

    def expect_eof(self) -> None:
        if self.kind() != EOF:
            raise self.error(f"unexpected {self.tok.describe()} after expression")

    # ------------------------------------------------------------------
    # Declarations
    # ------------------------------------------------------------------

    def parse_declaration(self, scope: str, access: Optional[str] = None):
        if self.kind() == PREPROC:
            t = self.advance()
            return ast.Preproc(text=t.text, line=t.line, col=t.col)

        if self.at_ident("using"):
            return self.parse_using()

        if self.kind() == IDENT:
            # operator= and friends: "operator" <op> ":"
            if (
                self.tok.text == "operator"
                and not self.tok.escaped_ident
                and self.peek(1).text not in (":", ":=")
            ):
                opname = self.parse_operator_name()
                self.expect(":", "':' after the operator name")
                return self.parse_function_like(None, opname, scope, access, self.tok.line, self.tok.col)

            nxt = self.peek(1)
            if nxt.kind == OP and nxt.text == ":=":
                name_tok = self.advance()
                self.advance()  # :=
                return self.parse_deduced_var(name_tok, access)

            if nxt.kind == OP and nxt.text == ":":
                name_tok = self.advance()
                self.advance()  # :
                return self.parse_after_name(name_tok.text, name_tok, scope, access)

        raise self.error(
            f"expected a declaration of the form 'name: kind = value', but found {self.tok.describe()}"
        )

    def parse_operator_name(self) -> str:
        start = self.expect_ident("'operator'")
        op = ""
        if self.at("[") and self.peek(1).text == "]":
            self.advance()
            self.advance()
            op = "[]"
        elif self.at("(") and self.peek(1).text == ")":
            self.advance()
            self.advance()
            op = "()"
        elif self.tok.kind == OP:
            op = self.advance().text
        else:
            raise self.error("expected an operator symbol after 'operator'")
        return op

    def parse_using(self):
        start = self.expect_ident("'using'")
        parts: list[str] = []
        if self.at("::"):
            parts.append("")
            self.advance()
        parts.append(self.expect_ident("a name after 'using'").text)
        while self.at("::"):
            self.advance()
            if self.at("_"):
                self.advance()
                return ast.UsingDecl(target="::".join(parts), is_namespace=True, line=start.line, col=start.col)
            parts.append(self.expect_ident("a name after '::'").text)
        self.expect(";", "';' after a using declaration")
        target = "::".join(parts)
        if target.endswith("::_"):
            return ast.UsingDecl(target=target[:-3], is_namespace=True, line=start.line, col=start.col)
        return ast.UsingDecl(target=target, is_namespace=False, line=start.line, col=start.col)

    def parse_deduced_var(self, name_tok: Token, access: Optional[str]):
        init = self.parse_expression()
        self.expect(";", "';' after an object declaration")
        return ast.VarDecl(
            name=name_tok.text,
            type=None,
            init=init,
            deduced=True,
            access=access,
            line=name_tok.line,
            col=name_tok.col,
        )

    def parse_template_params(self) -> list:
        self.expect("<")
        params = []
        while not self.at(">") and not self.at(">>"):
            name_tok = self.expect_ident("a template parameter name")
            variadic = self.match("...")
            p = ast.TemplateParam(name=name_tok.text, variadic=variadic, line=name_tok.line, col=name_tok.col)
            if self.match(":"):
                if self.at_ident("type"):
                    self.advance()
                    p.is_type = True
                else:
                    p.type = self.parse_type_expr()
            else:
                p.is_type = True
            if self.match("="):
                p.default = self.parse_expression(stop={",", ">", ">>"})
            params.append(p)
            if not self.match(","):
                break
        self.expect_template_close()
        return params

    def parse_after_name(self, name: str, name_tok: Token, scope: str, access: Optional[str]):
        line, col = name_tok.line, name_tok.col
        template_params = self.parse_template_params() if self.at("<") else []

        metafunctions: list = []
        while self.kind() == AT:
            at_tok = self.advance()
            args = None
            if self.at("<"):
                args = self.parse_template_args()
            metafunctions.append((at_tok.text, args))

        is_unsafe = False
        if self.at_kw("unsafe") and self.peek(1).text in ("(", "<"):
            self.advance()
            is_unsafe = True
            if self.at("<"):
                template_params = self.parse_template_params()

        if self.at_kw("type"):
            self.advance()
            if self.match("=="):
                alias = self.parse_type_with_constraint()
                self.expect(";", "';' after a type alias")
                return ast.TypeDecl(
                    name=name,
                    template_params=template_params,
                    metafunctions=metafunctions,
                    kind="==",
                    alias=alias,
                    access=access,
                    line=line,
                    col=col,
                )
            self.expect("=", "'=' or '==' after 'type'")
            self.expect("{", "'{' to begin the type body")
            members = self.parse_type_body()
            self.expect("}", "'}' to close the type body")
            if self.match(";"):
                pass
            return ast.TypeDecl(
                name=name,
                template_params=template_params,
                metafunctions=metafunctions,
                kind="=",
                members=members,
                access=access,
                line=line,
                col=col,
            )

        if self.at_kw("namespace"):
            self.advance()
            if self.match("=="):
                alias = self.parse_name()
                self.expect(";", "';' after a namespace alias")
                return ast.NamespaceDecl(
                    name=name,
                    kind="==",
                    alias=alias,
                    access=access,
                    line=line,
                    col=col,
                )
            self.expect("=", "'=' or '==' after 'namespace'")
            self.expect("{", "'{' to begin the namespace body")
            members = self.parse_namespace_body()
            self.expect("}", "'}' to close the namespace body")
            if self.match(";"):
                pass
            return ast.NamespaceDecl(
                name=name,
                kind="=",
                members=members,
                access=access,
                line=line,
                col=col,
            )

        if self.at("("):
            return self.parse_function_like(
                name, None, scope, access, line, col, template_params, is_unsafe=is_unsafe
            )

        # variable, variable template, or alias
        type_ = self.parse_type_with_constraint()
        if self.match("=="):
            value = self.parse_expression()
            self.expect(";", "';' after an alias declaration")
            return ast.VarDecl(
                name=name,
                template_params=template_params,
                type=type_,
                init=value,
                alias=True,
                access=access,
                line=line,
                col=col,
            )
        init = None
        if self.match("="):
            init = self.parse_initializer()
        self.expect(";", "';' after an object declaration")
        return ast.VarDecl(
            name=name,
            template_params=template_params,
            type=type_,
            init=init,
            access=access,
            line=line,
            col=col,
        )

    def parse_function_like(
        self,
        name: Optional[str],
        operator: Optional[str],
        scope: str,
        access: Optional[str],
        line: int,
        col: int,
        template_params: Optional[list] = None,
        is_unsafe: bool = False,
    ):
        params = self.parse_params()
        returns = self.parse_returns() if self.at("->") else None

        requires = None
        contracts: list = []
        while True:
            if self.at_ident("requires"):
                self.advance()
                requires = self.parse_expression(stop={"=", "==", ";", "pre", "post"})
            elif (self.at_ident("pre") or self.at_ident("post")) and (
                self.peek(1).text in ("(", "<")
            ):
                contracts.append(self.parse_contract())
            else:
                break

        kind = "="
        body = None
        is_declaration_only = False
        if self.match("=="):
            kind = "=="
            body = self.parse_body()
        elif self.match("="):
            body = self.parse_body()
        elif self.match(";"):
            is_declaration_only = True
        else:
            raise self.error("expected '=', '==' or ';' after a function signature")

        return ast.FunctionDecl(
            name=name,
            operator=operator or "",
            is_operator=operator is not None,
            template_params=template_params or [],
            params=params,
            returns=returns,
            contracts=contracts,
            requires=requires,
            body=body,
            kind=kind,
            access=access,
            is_declaration_only=is_declaration_only,
            is_unsafe=is_unsafe,
            line=line,
            col=col,
        )

    def parse_type_body(self) -> list:
        members = []
        access: Optional[str] = None
        while not self.at("}"):
            if self.kind() == EOF:
                raise self.error("unexpected end of file inside a type body")
            if self.kind() == PREPROC:
                t = self.advance()
                members.append(ast.Preproc(text=t.text, line=t.line, col=t.col))
                continue
            if self.tok.kind == IDENT and self.peek(1).kind == OP and self.peek(1).text in (";", ":="):
                name_tok = self.advance()
                value = None
                if self.match(":="):
                    value = self.parse_expression(stop={";"})
                self.expect(";", "';' after an enumerator")
                members.append(
                    ast.Enumerator(name=name_tok.text, value=value, line=name_tok.line, col=name_tok.col)
                )
                continue
            if self.tok.kind == IDENT and self.tok.text in ACCESS_KEYWORDS:
                nxt = self.peek(1)
                if not (nxt.kind == OP and nxt.text in (":", ":=")):
                    access_tok = self.advance()
                    access = access_tok.text
                    continue
            members.append(self.parse_declaration(scope="type", access=access))
            access = None
        return members

    def parse_namespace_body(self) -> list:
        members = []
        while not self.at("}"):
            if self.kind() == EOF:
                raise self.error("unexpected end of file inside a namespace body")
            if self.kind() == PREPROC:
                t = self.advance()
                members.append(ast.Preproc(text=t.text, line=t.line, col=t.col))
                continue
            members.append(self.parse_declaration(scope="namespace"))
        return members

    # ------------------------------------------------------------------
    # Parameters and returns
    # ------------------------------------------------------------------

    def parse_params(self) -> list:
        self.expect("(")
        params = []
        while not self.at(")"):
            params.append(self.parse_param())
            if not self.match(","):
                break
            if self.at(")"):  # trailing comma
                break
        self.expect(")", "')' to close the parameter list")
        return params

    def _param_lookahead_is_name(self) -> bool:
        nxt = self.peek(1)
        return nxt.kind == OP and nxt.text in (":", ",", ")", "=", "...")

    def parse_param(self) -> ast.Param:
        line, col = self.tok.line, self.tok.col
        qualifiers = []
        while (
            self.tok.kind == IDENT
            and self.tok.text in PARAM_QUALIFIERS
            and not self.tok.escaped_ident
            and not self._param_lookahead_is_name()
        ):
            qualifiers.append(self.advance().text)

        name: Optional[str] = None
        is_this = False
        is_that = False
        is_wildcard = False
        variadic = False

        if self.tok.kind == IDENT:
            if self.tok.text == "this" and not self.tok.escaped_ident:
                is_this = True
                self.advance()
            elif self.tok.text == "that" and not self.tok.escaped_ident:
                is_that = True
                self.advance()
            elif self.tok.text == "_":
                is_wildcard = True
                self.advance()
            else:
                name = self.advance().text
                if self.at("..."):
                    self.advance()
                    variadic = True

        type_ = None
        if self.match(":"):
            type_ = self.parse_type_with_constraint()

        default = None
        if self.match("="):
            default = self.parse_expression()

        return ast.Param(
            name=name,
            type=type_,
            qualifiers=qualifiers,
            default=default,
            variadic=variadic,
            is_this=is_this,
            is_that=is_that,
            is_wildcard=is_wildcard,
            line=line,
            col=col,
        )

    def parse_returns(self) -> ast.Returns:
        self.expect("->")
        if self.at("("):
            self.advance()
            named = []
            while not self.at(")"):
                p = self.parse_param()
                kind = "forward" if "forward" in p.qualifiers else "out"
                named.append(
                    ast.NamedReturn(
                        name=p.name or "_",
                        type=p.type,
                        kind=kind,
                        line=p.line,
                        col=p.col,
                    )
                )
                if not self.match(","):
                    break
                if self.at(")"):
                    break
            self.expect(")", "')' to close the return parameter list")
            return ast.Returns(kind="named", named=named, line=self.tok.line, col=self.tok.col)

        if (
            self.tok.kind == IDENT
            and self.tok.text in ("in", "inout", "out")
            and not self.tok.escaped_ident
        ):
            kw = self.advance()
            source = None
            if self.at("("):
                self.advance()
                source = self.expect_ident("the name of the borrowed parameter").text
                self.expect(")", "')' after the borrow source")
            type_ = self.parse_type_with_constraint()
            return ast.Returns(
                kind="single",
                type=type_,
                borrow_kind=kw.text,
                borrow_source=source,
                line=kw.line,
                col=kw.col,
            )

        forward = False
        forward_ref = False
        if self.at_ident("forward_ref"):
            forward_ref = True
            forward = True
            self.advance()
        elif self.at_ident("forward"):
            forward = True
            self.advance()
        elif self.at_ident("move"):
            self.advance()

        type_ = self.parse_type_with_constraint()
        is_void = isinstance(type_, ast.NamedType) and type_.name.text() == "void" and not type_.args
        return ast.Returns(
            kind="void" if is_void else "single",
            type=type_,
            forward=forward and not is_void,
            forward_ref=forward_ref,
            line=self.tok.line,
            col=self.tok.col,
        )

    # ------------------------------------------------------------------
    # Contracts
    # ------------------------------------------------------------------

    def parse_contract(self) -> ast.Contract:
        kw_tok = self.advance()  # pre / post
        groups: list = []
        predicates: list = []
        if self.at("<"):
            items = self.parse_angle_expression_list()
            if items:
                groups = [items[0]]
                predicates = items[1:]
        self.expect("(", "'(' after a contract name")
        cond = self.parse_expression(stop={",", ")"})
        message = None
        if self.match(","):
            message = self.parse_expression(stop={")"})
        self.expect(")", "')' to close the contract")
        return ast.Contract(
            kind=kw_tok.text,
            cond=cond,
            message=message,
            groups=groups,
            predicates=predicates,
            line=kw_tok.line,
            col=kw_tok.col,
        )

    def parse_angle_expression_list(self) -> list:
        self.expect("<")
        items = []
        if not self.at(">"):
            while True:
                items.append(self.parse_expression(stop={",", ">", ">>"}))
                if not self.match(","):
                    break
                if self.at(">"):
                    break
        self.expect_template_close()
        return items

    # ------------------------------------------------------------------
    # Types
    # ------------------------------------------------------------------

    def parse_type_with_constraint(self):
        type_ = self.parse_type_expr()
        if self.at_ident("is"):
            self.advance()
            constraint = self.parse_is_constraint()
            return ast.ConstrainedType(
                base=type_, constraint=constraint, line=type_.line, col=type_.col
            )
        return type_

    def parse_type_expr(self):
        if (
            self.tok.kind == IDENT
            and self.tok.text in ("in", "inout", "out")
            and not self.tok.escaped_ident
        ):
            nxt = self.peek(1)
            starts_type = nxt.kind == IDENT or (
                nxt.kind == OP and nxt.text in ("*", "(", "const")
            )
            if starts_type:
                kw = self.advance()
                inner = self.parse_type_expr()
                return ast.BorrowType(
                    kind=kw.text, inner=inner, line=kw.line, col=kw.col
                )
        ops: list[str] = []
        while True:
            if self.tok.kind == IDENT and self.tok.text == "const":
                ops.append("const")
                self.advance()
            elif self.tok.kind == OP and self.tok.text == "*":
                ops.append("*")
                self.advance()
            else:
                break
        atom = self.parse_type_atom()
        result = atom
        for op in reversed(ops):
            if op == "const":
                result = ast.ConstType(inner=result, line=atom.line, col=atom.col)
            else:
                result = ast.PointerType(inner=result, line=atom.line, col=atom.col)
        return result

    def parse_type_atom(self):
        t = self.tok
        if t.kind == OP and t.text == "_":
            self.advance()
            return ast.DeducedType(line=t.line, col=t.col)
        if t.kind == IDENT and t.text == "_":
            self.advance()
            return ast.DeducedType(line=t.line, col=t.col)
        if t.kind == OP and t.text == "(":
            self.advance()
            params = []
            while not self.at(")"):
                params.append(self.parse_param())
                if not self.match(","):
                    break
                if self.at(")"):
                    break
            self.expect(")")
            returns = self.parse_returns() if self.at("->") else None
            return ast.FunctionType(params=params, returns=returns, line=t.line, col=t.col)
        if t.kind == IDENT:
            if t.text == "type_of" and self.peek(1).text == "(":
                self.advance()
                self.advance()
                expr = self.parse_expression(stop={")"})
                self.expect(")")
                return ast.TypeOfType(expr=expr, line=t.line, col=t.col)
            if t.text == "decltype" and self.peek(1).text == "(":
                self.advance()
                self.advance()
                expr = self.parse_expression(stop={")"})
                self.expect(")")
                name = ast.Name(parts=["decltype"], line=t.line, col=t.col)
                return ast.NamedType(name=name, args=[expr], line=t.line, col=t.col)
            name = self.parse_name()
            args = None
            if self.at("<"):
                args = self.parse_template_args()
            return ast.NamedType(name=name, args=args, line=t.line, col=t.col)
        raise self.error(f"expected a type, but found {t.describe()}")

    def parse_template_args(self) -> list:
        self.expect("<")
        args = []
        while not self.at(">") and not self.at(">>"):
            save = self.pos
            is_type = False
            try:
                type_ = self.parse_type_expr()
                if self.tok.text in (",", ">", ">>", ">=", ">>="):
                    args.append(type_)
                    is_type = True
            except CompileError:
                is_type = False
            if not is_type:
                self.pos = save
                args.append(self.parse_expression(stop={",", ">", ">>", ">=", ">>="}))
            if not self.match(","):
                break
            if self.at(">") or self.at(">>"):
                break
        self.expect_template_close()
        return args

    def expect_template_close(self) -> None:
        t = self.tok
        if t.kind == OP and t.text == ">":
            self.advance()
            return
        # Split compound tokens that end a template argument list.
        splits = {">>": (">", ">"), ">=": (">", "="), ">>=": (">", ">=")}
        if t.kind == OP and t.text in splits:
            first, rest = splits[t.text]
            self.tokens[self.pos] = Token(kind=OP, text=first, line=t.line, col=t.col)
            self.tokens.insert(self.pos + 1, Token(kind=OP, text=rest, line=t.line, col=t.col + len(first)))
            self.advance()
            return
        raise self.error(f"expected '>' to close a template argument list, but found {t.describe()}")

    def parse_name(self) -> ast.Name:
        line, col = self.tok.line, self.tok.col
        leading = self.match("::")
        parts = [self.expect_ident("a name").text]
        while self.at("::"):
            self.advance()
            parts.append(self.expect_ident("a name after '::'").text)
        return ast.Name(parts=parts, leading_global=leading, line=line, col=col)

    # ------------------------------------------------------------------
    # Initializers
    # ------------------------------------------------------------------

    def parse_initializer(self):
        if self.at("("):
            return self.parse_paren_initializer()
        return self.parse_expression()

    def parse_paren_initializer(self):
        t = self.expect("(")
        items = []
        while not self.at(")"):
            items.append(self.parse_initializer_item())
            if not self.match(","):
                break
            if self.at(")"):
                break
        self.expect(")", "')' to close the initializer")
        return ast.InitList(items=items, line=t.line, col=t.col)

    def parse_initializer_item(self):
        # Support `name = value` designated initializers inside ( ).
        if self.tok.kind == IDENT and self.peek(1).text == "=" and self.peek(2).text != "=":
            name_tok = self.advance()
            self.advance()
            value = self.parse_initializer_item()
            return ast.DesignatedInit(name=name_tok.text, value=value, line=name_tok.line, col=name_tok.col)
        return self.parse_expression(stop={",", ")"})

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    def parse_body(self) -> ast.Body:
        t = self.tok
        if self.at("{"):
            return ast.Body(stmt=self.parse_block(), line=t.line, col=t.col)
        expr = self.parse_expression(stop={";"})
        self.expect(";", "';' after a single-expression function body")
        return ast.Body(
            stmt=ast.ExprStmt(expr=expr, line=expr.line, col=expr.col), line=t.line, col=t.col
        )

    def parse_block(self) -> ast.Block:
        start = self.expect("{")
        stmts = []
        while not self.at("}"):
            if self.kind() == EOF:
                raise self.error("unexpected end of file inside a block")
            stmts.append(self.parse_statement())
        self.expect("}", "'}' to close the block")
        return ast.Block(stmts=stmts, line=start.line, col=start.col)

    def parse_statement(self):
        t = self.tok

        if t.kind == PREPROC:
            self.advance()
            return ast.Preproc(text=t.text, line=t.line, col=t.col)

        if self.at("{"):
            return self.parse_block()

        if self.at_kw("unsafe") and self.peek(1).text == "{":
            self.advance()
            body = self.parse_block()
            return ast.UnsafeBlock(stmt=body, line=t.line, col=t.col)

        if self.at(";") and self.tok.kind == OP:
            self.advance()
            return ast.Block(stmts=[], line=t.line, col=t.col)

        if t.kind == IDENT:
            text = t.text
            nxt = self.peek(1)

            # named loop:  outer: while ...
            if (
                nxt.kind == OP
                and nxt.text == ":"
                and self.peek(2).kind == IDENT
                and self.peek(2).text in ("while", "for", "do")
            ):
                label = self.advance().text
                self.advance()  # :
                loop = self.parse_statement()
                if not isinstance(loop, (ast.While, ast.For, ast.DoWhile)):
                    raise self.error("a label must precede a loop")
                loop.label = label
                return loop

            if text == "if":
                return self.parse_if()
            if text == "while":
                return self.parse_while()
            if text == "do":
                return self.parse_do()
            if text == "for":
                return self.parse_for()
            if text == "return":
                self.advance()
                expr = None
                if not self.at(";"):
                    expr = self.parse_expression(stop={";"})
                self.expect(";", "';' after 'return'")
                return ast.Return(expr=expr, line=t.line, col=t.col)
            if text in ("break", "continue"):
                self.advance()
                label = None
                if self.tok.kind == IDENT:
                    label = self.advance().text
                self.expect(";", "';' after a loop control statement")
                cls = ast.Break if text == "break" else ast.Continue
                return cls(label=label, line=t.line, col=t.col)
            if text == "try":
                return self.parse_try()
            if text == "throw":
                self.advance()
                if self.match("("):
                    expr = self.parse_expression(stop={")"})
                    self.expect(")")
                else:
                    expr = self.parse_expression(stop={";"})
                self.expect(";", "';' after 'throw'")
                return ast.Throw(expr=expr, line=t.line, col=t.col)
            if text == "assert" and self.peek(1).text in ("(", "<"):
                return self.parse_assert()
            if text == "using":
                return self.parse_using()

            if nxt.kind == OP and nxt.text in (":", ":="):
                decl = self.parse_declaration(scope="function")
                if isinstance(decl, (ast.TypeDecl, ast.NamespaceDecl)):
                    raise self.error("types and namespaces cannot be declared at function scope")
                return decl

            if text == "operator" and nxt.text not in (":", ":="):
                raise self.error("operator functions cannot be declared at function scope")

        if self.at("("):
            save = self.pos
            try:
                params = self.parse_params()
                if self.at("{") or (self.tok.kind == IDENT and self.tok.text in STATEMENT_KEYWORDS):
                    body = self.parse_statement()
                    return ast.RaiiScope(params=params, body=body, line=t.line, col=t.col)
            except CompileError:
                pass
            self.pos = save

        expr = self.parse_expression(stop={";"})
        self.expect(";", "';' after an expression statement")
        return ast.ExprStmt(expr=expr, line=t.line, col=t.col)

    def parse_if(self) -> ast.If:
        t = self.advance()  # if
        cond = self.parse_expression(stop={"{"})
        then = self.parse_block()
        else_ = None
        if self.at_ident("else"):
            self.advance()
            if self.at_ident("if"):
                else_ = self.parse_if()
            else:
                else_ = self.parse_block()
        return ast.If(cond=cond, then=then, else_=else_, line=t.line, col=t.col)

    def parse_while(self) -> ast.While:
        t = self.advance()
        cond = self.parse_expression(stop={"next", "{"})
        next_expr = None
        if self.at_ident("next"):
            self.advance()
            next_expr = self.parse_expression(stop={"{"})
        body = self.parse_block()
        return ast.While(cond=cond, body=body, next_expr=next_expr, line=t.line, col=t.col)

    def parse_do(self) -> ast.DoWhile:
        t = self.advance()
        body = self.parse_block()
        next_expr = None
        if self.at_ident("next"):
            self.advance()
            next_expr = self.parse_expression(stop={"while"})
        if not self.at_ident("while"):
            raise self.error("expected 'while' after a 'do' loop body")
        self.advance()
        cond = self.parse_expression(stop={";"})
        self.expect(";", "';' after a do-while loop")
        return ast.DoWhile(body=body, next_expr=next_expr, cond=cond, line=t.line, col=t.col)

    def parse_for(self) -> ast.For:
        t = self.advance()
        if self.at("("):
            rng = self.parse_paren_range()
        else:
            rng = self.parse_expression(stop={"next", "do"})
        next_expr = None
        if self.at_ident("next"):
            self.advance()
            next_expr = self.parse_expression(stop={"do"})
        if not self.at_ident("do"):
            raise self.error("expected 'do' in a range-for loop")
        self.advance()
        self.expect("(", "'(' to declare the loop variable")
        param = self.parse_param()
        self.expect(")", "')' after the loop variable")
        body = self.parse_statement()
        return ast.For(range=rng, next_expr=next_expr, param=param, body=body, line=t.line, col=t.col)

    def parse_paren_range(self):
        t = self.expect("(")
        items = [self.parse_expression(stop={",", ")"})]
        while self.match(","):
            if self.at(")"):
                break
            items.append(self.parse_expression(stop={",", ")"}))
        self.expect(")")
        if len(items) == 1:
            return items[0]
        return ast.CommaList(items=items, line=t.line, col=t.col)

    def parse_try(self) -> ast.TryCatch:
        t = self.advance()
        body = self.parse_block()
        catches = []
        while self.at_ident("catch"):
            self.advance()
            self.expect("(", "'(' after 'catch'")
            is_catchall = False
            param = None
            if self.at("_"):
                self.advance()
                is_catchall = True
            else:
                param = self.parse_param()
            self.expect(")", "')' after a catch parameter")
            cbody = self.parse_block()
            catches.append(ast.Catch(param=param, body=cbody, is_catchall=is_catchall, line=t.line, col=t.col))
        if not catches:
            raise self.error("expected at least one 'catch' after 'try'")
        return ast.TryCatch(body=body, catches=catches, line=t.line, col=t.col)

    def parse_assert(self) -> ast.Assert:
        t = self.advance()
        groups: list = []
        predicates: list = []
        if self.at("<"):
            items = self.parse_angle_expression_list()
            if items:
                groups = [items[0]]
                predicates = items[1:]
        self.expect("(", "'(' after 'assert'")
        cond = self.parse_expression(stop={",", ")"})
        message = None
        if self.match(","):
            message = self.parse_expression(stop={")"})
        self.expect(")", "')' after an assertion")
        self.expect(";", "';' after an assertion")
        return ast.Assert(cond=cond, message=message, groups=groups, predicates=predicates, line=t.line, col=t.col)

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    def parse_expression(self, stop: frozenset[str] | set[str] = frozenset()):
        self._stop_stack.append(frozenset(stop))
        try:
            return self.parse_assignment()
        finally:
            self._stop_stack.pop()

    def parse_assignment(self):
        left = self.parse_logical_or()
        if self.tok.kind == OP and self.tok.text in ASSIGN_OPS and not self._at_stop():
            op = self.advance().text
            right = self.parse_assignment()
            return ast.Assign(op=op, lhs=left, rhs=right, line=left.line, col=left.col)
        return left

    def _binary(self, sub, ops):
        left = sub()
        while self.tok.kind == OP and self.tok.text in ops and not self._at_stop():
            op_tok = self.advance()
            right = sub()
            left = ast.Binary(op=op_tok.text, left=left, right=right, line=op_tok.line, col=op_tok.col)
        return left

    def parse_logical_or(self):
        return self._binary(self.parse_logical_and, {"||"})

    def parse_logical_and(self):
        return self._binary(self.parse_bit_or, {"&&"})

    def parse_bit_or(self):
        return self._binary(self.parse_bit_xor, {"|"})

    def parse_bit_xor(self):
        return self._binary(self.parse_bit_and, {"^"})

    def parse_bit_and(self):
        return self._binary(self.parse_equality, {"&"})

    def parse_equality(self):
        return self._binary(self.parse_relational, EQUALITY_OPS)

    def parse_relational(self):
        left = self.parse_is_as()
        rest = []
        while self.tok.kind == OP and self.tok.text in RELATIONAL_OPS and not self._at_stop():
            op_tok = self.advance()
            right = self.parse_is_as()
            rest.append((op_tok.text, right))
        if rest:
            return ast.CompareChain(first=left, rest=rest, line=left.line, col=left.col)
        return left

    def parse_is_as(self):
        left = self.parse_shift()
        while True:
            if self.at_kw("is"):
                self.advance()
                constraint = self.parse_is_constraint()
                left = ast.Is(expr=left, constraint=constraint, line=left.line, col=left.col)
            elif self.at_kw("as"):
                self.advance()
                type_ = self.parse_type_expr()
                left = ast.As(expr=left, type=type_, line=left.line, col=left.col)
            else:
                break
        return left

    def parse_is_constraint(self):
        t = self.tok
        if self.tok.kind == IDENT and self.tok.text == "_":
            self.advance()
            return ast.IsWildcard(line=t.line, col=t.col)
        if self.at("("):
            self.advance()
            expr = self.parse_expression(stop={")"})
            self.expect(")")
            return expr
        if t.kind == OP and t.text == "*":
            return self.parse_type_expr()
        if t.kind in (NUMBER, STRING, CHAR):
            return self.parse_unary()
        if t.kind == OP and t.text in ("+", "-"):
            return self.parse_unary()
        return self.parse_type_expr()

    def parse_shift(self):
        return self._binary(self.parse_additive, SHIFT_OPS)

    def parse_additive(self):
        return self._binary(self.parse_multiplicative, ADDITIVE_OPS)

    def parse_multiplicative(self):
        return self._binary(self.parse_unary, MULTIPLICATIVE_OPS)

    def parse_unary(self):
        t = self.tok
        if t.kind == OP and t.text in ("!", "+", "-"):
            self.advance()
            expr = self.parse_unary()
            return ast.Unary(op=t.text, expr=expr, line=t.line, col=t.col)
        if t.kind == IDENT and t.text in ("move", "forward", "out") and not t.escaped_ident:
            nxt = self.peek(1)
            if nxt.kind == IDENT or nxt.text == "(" or nxt.text == "*":
                self.advance()
                expr = self.parse_unary()
                if t.text == "move":
                    return ast.Move(expr=expr, line=t.line, col=t.col)
                if t.text == "forward":
                    return ast.Forward(expr=expr, line=t.line, col=t.col)
                return ast.OutArg(expr=expr, line=t.line, col=t.col)
        return self.parse_postfix()

    def _next_token_starts_operand(self) -> bool:
        nxt = self.peek(1)
        if nxt.kind in (IDENT, NUMBER, STRING, CHAR, AT):
            return True
        return nxt.kind == OP and nxt.text in ("(", "!", "+", "-")

    def _next_starts_operand(self) -> bool:
        return self._next_token_starts_operand()

    def parse_postfix(self):
        expr = self.parse_primary()
        while True:
            t = self.tok
            if t.kind != OP:
                break
            if t.text == ".":
                self.advance()
                name, qualified = self.parse_member_name()
                template_args = None
                if self.at("<"):
                    save = self.pos
                    try:
                        template_args = self.parse_template_args()
                    except CompileError:
                        self.pos = save
                        template_args = None
                if name == "new" and template_args is not None:
                    self.expect("(", "'(' after 'new<T>'")
                    args = self.parse_call_args_body()
                    ownership = "shared" if _is_shared_owner(expr) else "unique"
                    type_ = template_args[0] if template_args else None
                    expr = ast.New(type=type_, args=args, ownership=ownership, line=t.line, col=t.col)
                    continue
                member = ast.Member(
                    obj=expr,
                    name=name,
                    member_only=False,
                    template_args=template_args,
                    line=t.line,
                    col=t.col,
                )
                if self.at("("):
                    args = self.parse_call_args()
                    expr = ast.Call(callee=member, args=args, line=t.line, col=t.col)
                else:
                    expr = member
                continue
            if t.text == "..":
                self.advance()
                name, _ = self.parse_member_name()
                member = ast.Member(obj=expr, name=name, member_only=True, line=t.line, col=t.col)
                if self.at("("):
                    args = self.parse_call_args()
                    expr = ast.Call(callee=member, args=args, line=t.line, col=t.col)
                else:
                    expr = member
                continue
            if t.text == "[":
                self.advance()
                index = self.parse_expression(stop={"]"})
                self.expect("]", "']' to close a subscript")
                expr = ast.Subscript(obj=expr, index=index, line=t.line, col=t.col)
                continue
            if t.text == "(":
                args = self.parse_call_args()
                expr = ast.Call(callee=expr, args=args, line=t.line, col=t.col)
                continue
            if t.text in ("*", "&"):
                if self._next_token_starts_operand():
                    break
                self.advance()
                expr = ast.Postfix(op=t.text, expr=expr, line=t.line, col=t.col)
                continue
            if t.text in ("~", "++", "--", "$"):
                self.advance()
                expr = ast.Postfix(op=t.text, expr=expr, line=t.line, col=t.col)
                continue
            if t.text in ("..<", "..="):
                self.advance()
                last = self.parse_additive()
                expr = ast.Range(first=expr, last=last, closed=t.text == "..=", line=t.line, col=t.col)
                continue
            if t.text == "...":
                self.advance()
                expr = ast.PackExpansion(expr=expr, line=t.line, col=t.col)
                continue
            if t.text == "<":
                save = self.pos
                try:
                    template_args = self.parse_template_args()
                except CompileError:
                    self.pos = save
                    break
                if self.at("("):
                    args = self.parse_call_args()
                    expr = ast.Call(callee=expr, args=args, template_args=template_args, line=t.line, col=t.col)
                    continue
                if isinstance(expr, ast.NameExpr) and not self._next_starts_operand():
                    expr = ast.NameExpr(
                        name=expr.name, template_args=template_args, line=t.line, col=t.col
                    )
                    continue
                self.pos = save
                break
            break
        return expr

    def parse_member_name(self) -> tuple[str, bool]:
        if self.tok.kind != IDENT:
            raise self.error(f"expected a member name after '.', but found {self.tok.describe()}")
        parts = [self.advance().text]
        while self.at("::"):
            self.advance()
            parts.append(self.expect_ident("a name after '::'").text)
        return "::".join(parts), len(parts) > 1

    def parse_call_args(self) -> list:
        self.expect("(")
        return self.parse_call_args_body()

    def parse_call_args_body(self) -> list:
        args = []
        while not self.at(")"):
            args.append(self.parse_expression(stop={",", ")"}))
            if not self.match(","):
                break
            if self.at(")"):
                break
        self.expect(")", "')' to close the argument list")
        return args

    def parse_primary(self):
        t = self.tok

        if t.kind == NUMBER:
            self.advance()
            return ast.Literal(kind="number", raw=t.text, line=t.line, col=t.col)
        if t.kind == CHAR:
            self.advance()
            return ast.Literal(kind="char", raw=t.prefix + t.text, line=t.line, col=t.col)
        if t.kind == STRING:
            return self.parse_string_literal()
        if t.kind == OP and t.text == "(":
            return self.parse_paren_or_fold()
        if t.kind == OP and t.text == ":":
            return self.parse_unnamed_expression()
        if t.kind == AT:
            raise self.error("metafunction references are only allowed in type declarations")
        if t.kind == IDENT:
            text = t.text
            escaped = t.escaped_ident
            if text in ("true", "false") and not escaped:
                self.advance()
                return ast.Literal(kind="bool", raw=text, line=t.line, col=t.col)
            if text == "nullptr" and not escaped:
                self.advance()
                return ast.Literal(kind="null", raw="nullptr", line=t.line, col=t.col)
            if text == "inspect" and not escaped:
                return self.parse_inspect()
            if text == "new" and not escaped and self.peek(1).text == "<":
                self.advance()
                template_args = self.parse_template_args()
                self.expect("(", "'(' after 'new<T>'")
                args = self.parse_call_args_body()
                type_ = template_args[0] if template_args else None
                return ast.New(type=type_, args=args, ownership="unique", line=t.line, col=t.col)
            if text == "sizeof" and not escaped and self.peek(1).text == "...":
                self.advance()
                self.advance()
                self.expect("(")
                expr = self.parse_expression(stop={")"})
                self.expect(")")
                return ast.SizeofPack(expr=expr, line=t.line, col=t.col)
            if text == "unsafe" and not escaped and self.peek(1).text == "(":
                self.advance()
                self.expect("(")
                inner = self.parse_expression(stop={")"})
                self.expect(")", "')' after an unsafe expression")
                return ast.UnsafeExpr(expr=inner, line=t.line, col=t.col)
            if text == "_" and not escaped:
                self.advance()
                return ast.Id(name="_", line=t.line, col=t.col)
            if text == "this" and not escaped:
                self.advance()
                return ast.Id(name="this", line=t.line, col=t.col)
            if text == "that" and not escaped:
                self.advance()
                return ast.Id(name="that", line=t.line, col=t.col)
            name = self.parse_name()
            return ast.NameExpr(name=name, line=t.line, col=t.col)

        raise self.error(f"expected an expression, but found {t.describe()}")

    def parse_string_literal(self) -> ast.StringLiteral:
        t = self.advance()
        parts: list = []
        if t.raw_string:
            return ast.StringLiteral(
                parts=[], prefix=t.prefix, raw_string=True, raw_text=t.text,
                line=t.line, col=t.col,
            )
        if not t.parts:
            parts.append(ast.StrLit(text=""))
        else:
            for p in t.parts:
                if isinstance(p, str):
                    parts.append(ast.StrLit(text=_unescape_interp_escapes(p)))
                else:
                    assert isinstance(p, Interp)
                    sub_tokens = Lexer(
                        p.src, filename=self.filename, start_line=p.line, start_col=p.col
                    ).tokenize()
                    sub = Parser(sub_tokens, self.filename)
                    expr = sub.parse_expression()
                    sub.expect_eof()
                    parts.append(ast.StrInterp(expr=expr, spec=p.spec, line=p.line, col=p.col))
        return ast.StringLiteral(parts=parts, prefix=t.prefix, line=t.line, col=t.col)

    def parse_paren_or_fold(self):
        if self._paren_has_ellipsis():
            return self.parse_fold()
        t = self.expect("(")
        first = self.parse_expression(stop={",", ")"})
        if self.match(","):
            items = [first]
            while not self.at(")"):
                items.append(self.parse_expression(stop={",", ")"}))
                if not self.match(","):
                    break
            self.expect(")", "')' to close a comma list")
            return ast.CommaList(items=items, line=t.line, col=t.col)
        self.expect(")", "')' to close a parenthesised expression")
        return ast.Parenthesized(expr=first, line=t.line, col=t.col)

    def _paren_has_ellipsis(self) -> bool:
        depth = 0
        i = self.pos
        while i < len(self.tokens):
            t = self.tokens[i]
            if t.kind == OP:
                if t.text in ("(", "[", "{"):
                    depth += 1
                elif t.text in (")", "]", "}"):
                    depth -= 1
                    if depth == 0:
                        return False
                elif t.text == "..." and depth == 1:
                    return True
            elif t.kind in (EOF,):
                return False
            i += 1
        return False

    def parse_fold(self) -> ast.Fold:
        start = self.pos
        assert self.tokens[start].text == "("
        depth = 0
        i = start
        end = None
        while i < len(self.tokens):
            t = self.tokens[i]
            if t.kind == OP:
                if t.text in ("(", "[", "{"):
                    depth += 1
                elif t.text in (")", "]", "}"):
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            i += 1
        if end is None:
            raise self.error("unterminated fold expression")
        inner = self.tokens[start + 1 : end]
        # find the top-level ellipsis
        ell = None
        depth = 0
        for j, t in enumerate(inner):
            if t.kind == OP:
                if t.text in ("(", "[", "{"):
                    depth += 1
                elif t.text in (")", "]", "}"):
                    depth -= 1
                elif t.text == "..." and depth == 0:
                    ell = j
                    break
        if ell is None:
            raise self.error("malformed fold expression")
        t_line, t_col = self.tokens[start].line, self.tokens[start].col

        if ell == 0:
            # (... op pack)
            if len(inner) < 3 or inner[1].kind != OP:
                raise self.error("malformed unary right fold")
            op = inner[1].text
            pack = self._sub_expression(inner[2:])
            self.pos = end + 1
            return ast.Fold(op=op, pack=pack, init=None, left=False, line=t_line, col=t_col)

        op_tok = inner[ell - 1]
        if op_tok.kind != OP:
            raise self.error("expected an operator before '...' in a fold expression")
        op = op_tok.text
        if ell + 2 < len(inner) and inner[ell + 1].kind == OP:
            # (init op ... op pack)
            init = self._sub_expression(inner[: ell - 1])
            pack = self._sub_expression(inner[ell + 2 :])
            self.pos = end + 1
            return ast.Fold(op=op, pack=pack, init=init, left=True, line=t_line, col=t_col)
        if ell + 1 == len(inner):
            # (pack op ...)
            pack = self._sub_expression(inner[: ell - 1])
            self.pos = end + 1
            return ast.Fold(op=op, pack=pack, init=None, left=True, line=t_line, col=t_col)
        raise self.error("malformed fold expression")

    def _sub_expression(self, tokens: list[Token]):
        if not tokens:
            raise self.error("expected an expression in a fold expression")
        eof = _mk_eof(tokens[-1].line, tokens[-1].col)
        sub = Parser(list(tokens) + [eof], self.filename)
        expr = sub.parse_expression()
        sub.expect_eof()
        return expr

    def parse_unnamed_expression(self):
        t = self.expect(":")
        template_params = self.parse_template_params() if self.at("<") else []
        if self.at("("):
            params = self.parse_params()
            returns = self.parse_returns() if self.at("->") else None
            kind = "="
            if self.match("=="):
                kind = "=="
            else:
                self.expect("=", "'=' before a function expression body")
            body = self.parse_body()
            return ast.Lambda(
                template_params=template_params,
                params=params,
                returns=returns,
                body=body,
                kind=kind,
                line=t.line,
                col=t.col,
            )
        type_ = self.parse_type_with_constraint()
        self.expect("=", "'=' to define an unnamed object")
        init = self.parse_expression(stop={",", ")", "]", "}"})
        return ast.Temporary(type=type_, init=init, line=t.line, col=t.col)

    def parse_inspect(self) -> ast.Inspect:
        t = self.advance()  # inspect
        value = self.parse_expression(stop={"{", "->"})
        type_ = None
        if self.match("->"):
            type_ = self.parse_type_with_constraint()
        self.expect("{", "'{' to begin inspect alternatives")
        alts = []
        while not self.at("}"):
            if not self.at_ident("is"):
                raise self.error("expected 'is' to introduce an inspect alternative")
            is_tok = self.advance()
            constraint = self.parse_is_constraint()
            self.expect("=", "'=' before an inspect alternative body")
            alt_value = self.parse_expression(stop={";"})
            self.expect(";", "';' after an inspect alternative")
            alts.append(
                ast.InspectAlt(constraint=constraint, value=alt_value, line=is_tok.line, col=is_tok.col)
            )
        self.expect("}", "'}' to close inspect alternatives")
        return ast.Inspect(expr=value, type=type_, alts=alts, line=t.line, col=t.col)


def _is_shared_owner(expr) -> bool:
    if isinstance(expr, ast.NameExpr):
        return expr.name.parts[-1] == "shared"
    return False


def _unescape_interp_escapes(text: str) -> str:
    """Turn Epitaxy-only escapes ``\\$`` and ``\\(`` into literal characters."""
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in "$(":
            out.append(text[i + 1])
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def parse(source: str, filename: str = "<input>") -> ast.TranslationUnit:
    tokens = Lexer(source, filename).tokenize()
    return Parser(tokens, filename).parse_translation_unit()
