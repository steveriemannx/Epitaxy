"""Epitaxy: a small, from-scratch Cpp2-syntax-compatible language.

Epitaxy compiles ``.ep`` source files to C++20.  It is MIT licensed and is an
independent implementation: it follows the Cpp2 syntax requirements but shares
no code with cppfront.  On top of the Cpp2 syntax it adds an ownership and
borrow checking model (``unique<T>``, ``shared<T>``, ``in T`` / ``inout T``
borrows and an ``unsafe`` boundary).
"""

__version__ = "0.4.0"
__license__ = "MIT"
