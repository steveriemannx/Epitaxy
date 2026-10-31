"""Lexer for Epitaxy.

The lexer is intentionally small and context free.  Everything that depends on
the syntactic context (keywords, declaration kinds, type vs. expression) is
resolved by the parser; the lexer only knows about Cpp2-style tokens:

* identifiers (including the ``__identifier__`` escape),
* numbers, character literals, string literals (with interpolation),
* operators and punctuation,
* preprocessor lines (passed through verbatim),
* ``@name`` metafunction references.

String interpolation ``(expr)$`` is recognised here and returned as a structured
list of literal/interpolation parts, because the boundaries of the interpolation
must be found before the embedded expression can be lexed and parsed.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Optional

from .diag import CompileError

IDENT = "ident"
NUMBER = "number"
STRING = "string"
CHAR = "char"
OP = "op"
AT = "at"
PREPROC = "preproc"
CPP1 = "cpp1"
EOF = "eof"

# Longest operators first so that greedy matching works.
_OPERATORS = [
    "..<",
    "..=",
    "<=>",
    "<<=",
    ">>=",
    "...",
    "::",
    ":=",
    "==",
    "!=",
    "<=",
    ">=",
    "->",
    "&&",
    "||",
    "++",
    "--",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "&=",
    "|=",
    "^=",
    "<<",
    ">>",
    "..",
    "{",
    "}",
    "(",
    ")",
    "[",
    "]",
    "<",
    ">",
    ",",
    ";",
    ":",
    "=",
    "+",
    "-",
    "*",
    "/",
    "%",
    "&",
    "|",
    "^",
    "!",
    "~",
    "?",
    ".",
    "$",
]

_PREFIXES = ("u8", "u", "U", "L")

_IDENT_ESCAPES = ("__identifier__", "__identifer__")


@dataclass(kw_only=True)
class Interp:
    """One ``(expr)$`` (optionally ``(expr:spec)$``) interpolation."""

    src: str
    line: int
    col: int
    spec: Optional[str] = None


@dataclass(kw_only=True)
class Token:
    kind: str
    text: str = ""
    line: int = 1
    col: int = 1
    prefix: str = ""  # string/char encoding prefix, e.g. "u8"
    parts: Optional[list] = None  # string parts: list[str | Interp]
    raw_string: bool = False
    escaped_ident: bool = False

    def describe(self) -> str:
        if self.kind == EOF:
            return "end of file"
        if self.kind == STRING:
            return "string literal"
        if self.kind == CHAR:
            return "character literal"
        if self.kind == PREPROC:
            return "preprocessor line"
        if self.kind == AT:
            return f"'@{self.text}'"
        return repr(self.text)


class Lexer:
    def __init__(self, source: str, filename: str = "<input>", start_line: int = 1, start_col: int = 1):
        self.src = source
        self.filename = filename
        self.pos = 0
        self.n = len(source)
        self.line = start_line
        self.col = start_col
        self.line_starts = self._compute_line_starts()
        self.tokens: list[Token] = []

    # -- position helpers ---------------------------------------------------

    def _compute_line_starts(self) -> list[int]:
        starts = [0]
        for i, ch in enumerate(self.src):
            if ch == "\n":
                starts.append(i + 1)
        return starts

    def _linecol(self, idx: int) -> tuple[int, int]:
        idx = max(0, min(idx, self.n))
        line_idx = bisect.bisect_right(self.line_starts, idx) - 1
        return line_idx + 1, idx - self.line_starts[line_idx] + 1

    def error(self, message: str, idx: Optional[int] = None) -> CompileError:
        line, col = self._linecol(self.pos if idx is None else idx)
        return CompileError(message, line, col, self.filename)

    # -- character helpers --------------------------------------------------

    def peek(self, offset: int = 0) -> str:
        i = self.pos + offset
        return self.src[i] if 0 <= i < self.n else ""

    def advance(self, count: int = 1) -> None:
        for _ in range(count):
            if self.pos < self.n and self.src[self.pos] == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
            self.pos += 1

    def startswith(self, s: str) -> bool:
        return self.src.startswith(s, self.pos)

    @staticmethod
    def _is_ident_start(ch: str) -> bool:
        return ch.isalpha() or ch == "_"

    @staticmethod
    def _is_ident_char(ch: str) -> bool:
        return ch.isalnum() or ch == "_"

    # -- main loop ----------------------------------------------------------

    def tokenize(self) -> list[Token]:
        while True:
            self._skip_space_and_comments()
            if self.pos >= self.n:
                break
            line, col = self.line, self.col
            ch = self.peek()

            if ch == "#":
                self._scan_preproc()
                continue
            if ch == "@":
                start = self.pos
                self.advance()
                if not self._is_ident_start(self.peek()):
                    raise self.error("expected a metafunction name after '@'", start)
                name_start = self.pos
                while self._is_ident_char(self.peek()):
                    self.advance()
                self.tokens.append(Token(kind=AT, text=self.src[name_start:self.pos], line=line, col=col))
                continue
            if ch == '"' or ch == "'" or (ch == "R" and self.peek(1) == '"') or self._starts_prefixed_literal():
                self._scan_string_or_char()
                continue
            if ch.isdigit() or (ch == "." and self.peek(1).isdigit()):
                self._scan_number()
                continue
            if self._is_ident_start(ch):
                self._scan_ident()
                self._maybe_scan_cpp1_block(line, col)
                continue
            for op in _OPERATORS:
                if self.startswith(op):
                    self.advance(len(op))
                    self.tokens.append(Token(kind=OP, text=op, line=line, col=col))
                    break
            else:
                raise self.error(f"unexpected character {ch!r}")

        self.tokens.append(Token(kind=EOF, text="", line=self.line, col=self.col))
        return self.tokens

    def _skip_space_and_comments(self) -> None:
        while self.pos < self.n:
            ch = self.peek()
            if ch in " \t\r\n\f\v":
                self.advance()
            elif ch == "/" and self.peek(1) == "/":
                while self.pos < self.n and self.peek() != "\n":
                    self.advance()
            elif ch == "/" and self.peek(1) == "*":
                start = self.pos
                self.advance(2)
                closed = False
                while self.pos < self.n:
                    if self.peek() == "*" and self.peek(1) == "/":
                        self.advance(2)
                        closed = True
                        break
                    self.advance()
                if not closed:
                    raise self.error("unterminated stream comment", start)
            else:
                return

    # -- preprocessor -------------------------------------------------------

    def _scan_preproc(self) -> None:
        line, col = self.line, self.col
        start = self.pos
        while self.pos < self.n:
            if self.peek() == "\n":
                if self.pos > 0 and self.src[self.pos - 1] == "\\":
                    self.advance()
                    continue
                break
            self.advance()
        self.tokens.append(Token(kind=PREPROC, text=self.src[start:self.pos], line=line, col=col))

    # -- identifiers --------------------------------------------------------

    def _scan_ident(self) -> None:
        line, col = self.line, self.col
        start = self.pos
        while self._is_ident_char(self.peek()):
            self.advance()
        text = self.src[start:self.pos]
        escaped = False
        for esc in _IDENT_ESCAPES:
            if text.startswith(esc) and len(text) > len(esc):
                text = text[len(esc):]
                escaped = True
                break
        self.tokens.append(Token(kind=IDENT, text=text, line=line, col=col, escaped_ident=escaped))

    def _maybe_scan_cpp1_block(self, line: int, col: int) -> bool:
        """Turn ``cpp1 { ... }`` into a raw C++ block token."""
        token = self.tokens[-1]
        if token.kind != IDENT or token.text != "cpp1" or token.escaped_ident:
            return False
        save_pos, save_line, save_col = self.pos, self.line, self.col
        while self.peek() in " \t\r\n\f\v":
            self.advance()
        if self.peek() != "{":
            self.pos, self.line, self.col = save_pos, save_line, save_col
            return False
        self.tokens.pop()
        self._scan_cpp1_block(line, col, token)
        return True

    def _scan_cpp1_block(self, line: int, col: int, ident_token: Token) -> None:
        start = self.pos
        if self.peek() != "{":
            raise self.error("expected '{' to begin a 'cpp1' block", start)
        self.advance()  # opening brace
        content_start = self.pos
        depth = 1
        while self.pos < self.n:
            ch = self.peek()
            if ch == '"':
                nxt = self._skip_quoted_at(self.pos)
                if nxt is None:
                    raise self.error("unterminated string literal inside a 'cpp1' block", self.pos)
                self.advance(nxt - self.pos)
                continue
            if ch == "'":
                nxt = self._skip_char_at(self.pos)
                if nxt is None:
                    raise self.error("unterminated character literal inside a 'cpp1' block", self.pos)
                self.advance(nxt - self.pos)
                continue
            if ch == "/" and self.peek(1) == "/":
                while self.pos < self.n and self.peek() != "\n":
                    self.advance()
                continue
            if ch == "/" and self.peek(1) == "*":
                self.advance(2)
                while self.pos < self.n and not (self.peek() == "*" and self.peek(1) == "/"):
                    self.advance()
                if self.pos >= self.n:
                    raise self.error("unterminated comment inside a 'cpp1' block", start)
                self.advance(2)
                continue
            if ch == "{":
                depth += 1
                self.advance()
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    content = self.src[content_start:self.pos]
                    self.advance()
                    self.tokens.append(
                        Token(kind=CPP1, text=content, line=line, col=col)
                    )
                    return
                self.advance()
                continue
            self.advance()
        raise self.error("unterminated 'cpp1' block", start)

    # -- numbers ------------------------------------------------------------

    def _scan_number(self) -> None:
        line, col = self.line, self.col
        start = self.pos

        if self.peek() == "0" and self.peek(1) in "xX":
            self.advance(2)
            while self.peek().isdigit() or self.peek() in "abcdefABCDEF" or self.peek() == "'" or self.peek() == "_":
                self.advance()
            if self.peek() == "." and self.peek(1) != ".":
                self.advance()
                while self.peek().isdigit() or self.peek() in "abcdefABCDEF" or self.peek() == "'":
                    self.advance()
            if self.peek() in "pP" and (self.peek(1).isdigit() or self.peek(1) in "+-"):
                self.advance(2)
                while self.peek().isdigit() or self.peek() == "'":
                    self.advance()
        elif self.peek() == "0" and self.peek(1) in "bB":
            self.advance(2)
            while self.peek() in "01" or self.peek() == "'":
                self.advance()
        else:
            while self.peek().isdigit() or (self.peek() == "'" and self.peek(1).isdigit()):
                self.advance()
            if self.peek() == "." and self.peek(1) != "." and not self._is_ident_start(self.peek(1)):
                self.advance()
                while self.peek().isdigit() or self.peek() == "'":
                    self.advance()
            if self.peek() in "eE" and (self.peek(1).isdigit() or self.peek(1) in "+-"):
                self.advance(2)
                while self.peek().isdigit() or self.peek() == "'":
                    self.advance()

        # standard suffixes and Cpp1 user-defined literals pass through
        while self._is_ident_char(self.peek()):
            self.advance()

        self.tokens.append(Token(kind=NUMBER, text=self.src[start:self.pos], line=line, col=col))

    # -- literals -----------------------------------------------------------

    def _starts_prefixed_literal(self) -> bool:
        for p in _PREFIXES:
            if self.startswith(p):
                rest = self.peek(len(p))
                if rest in ("'", '"'):
                    return True
                if rest == "R" and self.peek(len(p) + 1) == '"':
                    return True
        return False

    def _scan_string_or_char(self) -> None:
        line, col = self.line, self.col
        prefix = ""
        for p in _PREFIXES:
            if self.startswith(p):
                rest = self.peek(len(p))
                if rest in ("'", '"') or (rest == "R" and self.peek(len(p) + 1) == '"'):
                    prefix = p
                    self.advance(len(p))
                    break
        if self.peek() == "R" and self.peek(1) == '"':
            self._scan_raw_string(prefix, line, col)
            return
        if self.peek() == "'":
            self._scan_char(prefix, line, col)
            return
        if self.peek() != '"':
            raise self.error("expected a string or character literal")
        self._scan_string(prefix, line, col)

    def _scan_raw_string(self, prefix: str, line: int, col: int) -> None:
        start = self.pos
        if self.peek() == "R":
            self.advance()
        if self.peek() != '"':
            raise self.error("expected '\"' to begin a raw string literal", start)
        self.advance()  # opening quote
        delim_start = self.pos
        while self.pos < self.n and self.peek() != "(":
            if self.peek() in ('"', "\n"):
                raise self.error("malformed raw string literal", start)
            self.advance()
        if self.pos >= self.n:
            raise self.error("unterminated raw string literal", start)
        delimiter = self.src[delim_start:self.pos]
        closing = ")" + delimiter + '"'
        idx = self.src.find(closing, self.pos)
        if idx < 0:
            raise self.error("unterminated raw string literal", start)
        self.advance(idx - self.pos + len(closing))
        self.tokens.append(
            Token(kind=STRING, text=self.src[start:self.pos], line=line, col=col, prefix=prefix, raw_string=True)
        )

    def _scan_string(self, prefix: str, line: int, col: int) -> None:
        start = self.pos
        self.advance()  # opening quote
        parts: list = []
        lit: list[str] = []

        def flush() -> None:
            if lit:
                parts.append("".join(lit))
                lit.clear()

        while True:
            if self.pos >= self.n:
                raise self.error("unterminated string literal", start)
            c = self.peek()
            if c == "\n":
                raise self.error("unterminated string literal (newline in string)", start)
            if c == '"':
                self.advance()
                break
            if c == "\\":
                lit.append(c)
                self.advance()
                if self.pos >= self.n:
                    raise self.error("unterminated string literal", start)
                lit.append(self.peek())
                self.advance()
                continue
            if c == "(":
                interp = self._try_scan_interpolation()
                if interp is not None:
                    flush()
                    parts.append(interp)
                    continue
                lit.append(c)
                self.advance()
                continue
            lit.append(c)
            self.advance()
        flush()
        if not parts:
            parts = [""]
        self.tokens.append(
            Token(kind=STRING, text=self.src[start:self.pos], line=line, col=col, prefix=prefix, parts=parts)
        )

    def _try_scan_interpolation(self) -> Optional[Interp]:
        start_idx = self.pos
        i = start_idx + 1
        depth = 1
        n = self.n
        while i < n:
            c = self.src[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                nxt = self._skip_quoted_at(i)
                if nxt is None:
                    return None
                i = nxt
                continue
            if c == "'":
                nxt = self._skip_char_at(i)
                if nxt is None:
                    return None
                i = nxt
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    if i + 1 < n and self.src[i + 1] == "$":
                        expr_src, spec = _split_format_spec(self.src[start_idx + 1 : i])
                        ln, cl = self._linecol(start_idx + 1)
                        self.advance(i + 2 - self.pos)
                        return Interp(src=expr_src, line=ln, col=cl, spec=spec)
                    return None
            i += 1
        return None

    def _skip_quoted_at(self, i: int) -> Optional[int]:
        """Skip a string literal starting at the opening quote at *i*."""
        src = self.src
        n = self.n
        raw = False
        for pref in ("u8R", "uR", "UR", "LR", "R"):
            if i >= len(pref) and src.startswith(pref + '"', i - len(pref)):
                before = i - len(pref) - 1
                if before < 0 or not (src[before].isalnum() or src[before] == "_"):
                    raw = True
                    break
        if raw:
            j = i + 1
            while j < n and src[j] != "(":
                if src[j] in ('"', "\n"):
                    return None
                j += 1
            if j >= n:
                return None
            delim = src[i + 1 : j]
            k = src.find(")" + delim + '"', j + 1)
            return None if k < 0 else k + len(delim) + 2
        j = i + 1
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == '"':
                return j + 1
            if c == "\n":
                return None
            j += 1
        return None

    def _skip_char_at(self, i: int) -> Optional[int]:
        src = self.src
        n = self.n
        j = i + 1
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == "'":
                return j + 1
            if c == "\n":
                return None
            j += 1
        return None

    def _scan_char(self, prefix: str, line: int, col: int) -> None:
        start = self.pos
        self.advance()  # opening quote
        while True:
            if self.pos >= self.n:
                raise self.error("unterminated character literal", start)
            c = self.peek()
            if c == "\n":
                raise self.error("unterminated character literal", start)
            if c == "\\":
                self.advance()
                if self.pos >= self.n:
                    raise self.error("unterminated character literal", start)
                self.advance()
                continue
            if c == "'":
                self.advance()
                break
            self.advance()
        self.tokens.append(Token(kind=CHAR, text=self.src[start:self.pos], line=line, col=col, prefix=prefix))


def _split_format_spec(src: str) -> tuple[str, Optional[str]]:
    """Split ``expr:spec`` at a top-level single colon (not ``::``)."""
    depth = 0
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ":" and depth == 0:
            if i + 1 < n and src[i + 1] == ":":
                i += 2
                continue
            if i > 0 and src[i - 1] == ":":
                i += 1
                continue
            return src[:i], src[i + 1 :]
        i += 1
    return src, None


def tokenize(source: str, filename: str = "<input>") -> list[Token]:
    return Lexer(source, filename).tokenize()
