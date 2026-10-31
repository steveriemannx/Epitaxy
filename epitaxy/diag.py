"""Diagnostics for Epitaxy.

All user-facing errors are raised as :class:`CompileError` with a source
location, so the CLI can render a consistent ``file:line:col: error:`` message.
"""

from __future__ import annotations


class CompileError(Exception):
    """A diagnostic about Epitaxy source code."""

    def __init__(self, message: str, line: int = 0, col: int = 0, filename: str = "<input>"):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col
        self.filename = filename

    def render(self) -> str:
        if self.line:
            return f"{self.filename}:{self.line}:{self.col}: error: {self.message}"
        return f"{self.filename}: error: {self.message}"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.render()


class InternalError(Exception):
    """An invariant of the compiler itself was violated."""


def error_at(token, message: str, filename: str = "<input>") -> CompileError:
    return CompileError(message, token.line, token.col, filename)
