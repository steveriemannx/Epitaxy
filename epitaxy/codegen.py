"""C++20 code generator for Epitaxy.

The generator lowers Epitaxy declarations, statements and expressions to
C++20.  It is a source-to-source compiler: every program becomes a valid C++
translation unit that includes the Epitaxy support header
(``epi_support.hpp``).

Design notes:

* Free functions and types are forward declared, so declaration order does not
  matter -- except for functions whose return type is deduced, because C++
  cannot declare those without a body.  Such functions are emitted first.
* Class members are emitted in declaration order; functions are public by
  default and data members private by default, exactly as in the language.
* ``name: kind = value`` is lowered according to the kind; ``==`` marks
  constexpr aliases.
"""

from __future__ import annotations

import dataclasses
import os
import re
from typing import Optional

from . import ast
from .diag import CompileError
from .moveanalysis import analyze_moves

RELATIONAL_OPS = {"<", ">", "<=", ">="}
EQUALITY_OPS = {"==", "!="}

CONTRACT_GROUPS = {
    "default": "::epi::default_",
    "type_safety": "::epi::type_safety",
    "bounds_safety": "::epi::bounds_safety",
    "null_safety": "::epi::null_safety",
    "testing": "::epi::testing",
}

PRELUDE_HEADERS = [
    "algorithm",
    "any",
    "array",
    "chrono",
    "compare",
    "concepts",
    "cstddef",
    "cstdint",
    "cstdlib",
    "format",
    "functional",
    "iostream",
    "iterator",
    "map",
    "memory",
    "numeric",
    "optional",
    "ranges",
    "set",
    "sstream",
    "stdexcept",
    "string",
    "string_view",
    "thread",
    "type_traits",
    "unordered_map",
    "utility",
    "variant",
    "vector",
]

BY_VALUE_TYPE_NAMES = {
    "bool", "char", "signed char", "unsigned char", "short", "unsigned short",
    "int", "unsigned int", "long", "unsigned long", "long long", "unsigned long long",
    "float", "double", "long double", "wchar_t", "char8_t", "char16_t", "char32_t",
    "i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64",
    "ushort", "uint", "ulong", "longlong", "ulonglong", "longdouble",
    "_schar", "_uchar", "size_t",
}

COMPARISON_FUNCS = {
    "<": "cmp_lt",
    ">": "cmp_gt",
    "<=": "cmp_le",
    ">=": "cmp_ge",
}

SUPPORTED_METAFUNCTIONS = {
    "value",
    "copyable",
    "interface",
    "enum",
    "flag_enum",
    "ordered",
    "weakly_ordered",
    "partially_ordered",
    "basic_value",
    "weakly_ordered_value",
    "partially_ordered_value",
    "singleton",
}

UNSUPPORTED_METAFUNCTIONS = {
    "union",
    "autodiff",
    "regex",
    "print",
    "default_constructible",
}


@dataclasses.dataclass
class Options:
    subscript_checks: bool = True
    null_checks: bool = True
    div_checks: bool = True
    sign_checks: bool = True
    ownership_checks: bool = True
    preload_std: bool = True
    strict_safety: bool = False
    safe_only: bool = False


class FnCtx:
    def __init__(self, gen: "CodeGen"):
        self.gen = gen
        self.move_wrap: dict[int, str] = {}
        self.post_checks: list[str] = []
        self.post_captures: list[str] = []
        self.is_main = False
        self.return_kind = "void"
        self.return_type = "void"
        self.named_returns: list = []
        self.named_struct: Optional[str] = None
        self.named_local: Optional[str] = None
        self.goto_mode = False
        self.loop_stack: list[tuple[str, str, str]] = []
        self.loop_counter = 0
        self.in_lambda = False
        self.inside_class_name: Optional[str] = None
        self.has_return = False
        self.return_borrow: Optional[str] = None
        self.no_move_names: set[str] = set()


class TypeInfo:
    def __init__(self, decl: ast.TypeDecl):
        self.decl = decl
        self.name = decl.name
        self.template_params = decl.template_params
        self.metafunctions = [m[0] for m in decl.metafunctions]
        self.data_members: list[ast.VarDecl] = []
        self.bases: list[ast.VarDecl] = []
        self.functions: list[ast.FunctionDecl] = []
        for member in decl.members:
            if isinstance(member, ast.VarDecl):
                if member.name == "this":
                    self.bases.append(member)
                else:
                    self.data_members.append(member)
            elif isinstance(member, ast.FunctionDecl):
                self.functions.append(member)

    def member_names(self) -> list[str]:
        return [m.name for m in self.data_members if m.name]


class _SimpleInfo:
    """TypeInfo stand-in for @enum member functions."""

    def __init__(self, name: str):
        self.name = name
        self.metafunctions: list[str] = []
        self.data_members: list = []
        self.bases: list = []
        self.template_params: list = []


class CodeGen:
    def __init__(self, options: Optional[Options] = None, filename: str = "<input>"):
        self.options = options or Options()
        self.filename = filename
        self.lines: list[str] = []
        self.indent = 0
        self.counter = 0
        self._current_access = "private"
        self._inside_class: Optional[TypeInfo] = None

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def emit(self, text: str = "") -> None:
        if text:
            self.lines.append("    " * self.indent + text)
        else:
            self.lines.append("")

    def gensym(self, hint: str = "tmp") -> str:
        self.counter += 1
        return f"epi__{hint}_{self.counter}"

    def fail(self, message: str, node=None):
        line = getattr(node, "line", 0)
        col = getattr(node, "col", 0)
        raise CompileError(message, line, col, self.filename)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def generate(self, tu: ast.TranslationUnit, ownership_info=None) -> str:
        self.ownership_info = ownership_info
        self.emit("// Generated by the Epitaxy compiler. Do not edit.")
        self.emit(f"// Source: {self.filename}")
        self.emit("")
        preprocs = self._collect_preprocs(tu.items)
        for text in preprocs:
            self.emit(text)
        if self.options.preload_std:
            included = set()
            for text in preprocs:
                match = re.search(r"#\s*include\s*<([^>]+)>", text)
                if match:
                    included.add(match.group(1).strip())
            for header in PRELUDE_HEADERS:
                if header not in included:
                    self.emit(f"#include <{header}>")
        self.emit("")
        self.emit('#include "epi_support.hpp"')
        self.emit("")

        self.forward_pass_types(tu.items)
        self.forward_pass_aliases(tu.items)
        self.forward_pass_constants(tu.items)
        self.forward_pass_named_returns(tu.items)
        self.forward_pass_prototypes(tu.items)
        self.emit_cpp1_pass(tu.items)
        self.define_types_pass(tu.items)
        self.define_named_return_structs(tu.items)
        self.define_globals_pass(tu.items)
        self.define_functions_pass(tu.items)
        return "\n".join(self.lines) + "\n"

    def _collect_preprocs(self, items: list) -> list[str]:
        found = []
        for item in items:
            if isinstance(item, ast.Preproc):
                found.append(item.text)
            elif isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                found.extend(self._collect_preprocs(item.members))
        return found

    # ------------------------------------------------------------------
    # Passes
    # ------------------------------------------------------------------

    def _template_header(self, template_params: list) -> str:
        if not template_params:
            return ""
        parts = []
        for p in template_params:
            if p.is_type:
                prefix = "typename..." if p.variadic else "typename"
                parts.append(f"{prefix} {p.name}")
            else:
                t = self.cpp_type(p.type) if p.type is not None else "auto"
                parts.append(f"{t}... {p.name}" if p.variadic else f"{t} {p.name}")
        return "template<" + ", ".join(parts) + ">"

    def forward_pass_types(self, items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.forward_pass_types(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            elif isinstance(item, ast.TypeDecl) and item.kind == "=":
                header = self._template_header(item.template_params)
                prefix = header + " " if header else ""
                self.emit(f"{prefix}struct {item.name};")

    def forward_pass_aliases(self, items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.forward_pass_aliases(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            elif isinstance(item, ast.NamespaceDecl) and item.kind == "==":
                self.emit(f"namespace {item.name} = {item.alias.text()};")
            elif isinstance(item, ast.TypeDecl) and item.kind == "==":
                header = self._template_header(item.template_params)
                prefix = header + " " if header else ""
                self.emit(f"{prefix}using {item.name} = {self.cpp_type(item.alias)};")
            elif isinstance(item, ast.UsingDecl):
                self.emit(self.using_decl_code(item))
            elif isinstance(item, ast.FunctionDecl) and item.name is None:
                self.fail("anonymous functions are only allowed inside expressions", item)

    def forward_pass_constants(self, items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.forward_pass_constants(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            elif isinstance(item, ast.VarDecl) and item.alias and item.name:
                self.emit(self.object_alias_code(item))

    def _can_prototype(self, decl: ast.FunctionDecl) -> bool:
        return self.return_info(decl)[0] not in ("deduced", "deduced_forward")

    def forward_pass_prototypes(self, items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.forward_pass_prototypes(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            elif isinstance(item, ast.FunctionDecl) and item.name is not None:
                if self._can_prototype(item) and item.name != "main":
                    self.emit(self.function_signature(item, prototype=True) + ";")

    def forward_pass_named_returns(self, items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.forward_pass_named_returns(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            elif isinstance(item, ast.FunctionDecl) and item.name is not None:
                if self.return_info(item)[0] == "named_multi":
                    self.emit(f"struct {self.named_return_struct_name(item)};")

    def emit_cpp1_pass(self, items: list) -> None:
        for item in items:
            if isinstance(item, ast.Cpp1Block):
                self._emit_raw_text(item.text)
                self.emit("")
            elif isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.emit_cpp1_pass(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")

    def _emit_raw_text(self, text: str) -> None:
        lines = text.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return
        indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
        common = min(indents) if indents else 0
        for line in lines:
            self.emit(line[common:] if len(line) >= common else line.lstrip())

    def define_types_pass(self, items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.define_types_pass(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            elif isinstance(item, ast.TypeDecl) and item.kind == "=":
                self.gen_type(item)
                self.emit("")

    def define_named_return_structs(self, items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.define_named_return_structs(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            elif isinstance(item, ast.FunctionDecl) and item.name is not None:
                if self.return_info(item)[0] == "named_multi":
                    self.gen_named_return_struct(item)
                    self.emit("")

    def define_functions_pass(self, items: list) -> None:
        ordered = self._order_functions(items)
        for item in ordered:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.define_functions_pass(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            elif isinstance(item, ast.FunctionDecl) and item.name is not None:
                if item.body is not None:
                    self.gen_function(item)
                    self.emit("")

    def _order_functions(self, items: list) -> list:
        def deduced(item):
            return (
                isinstance(item, ast.FunctionDecl)
                and item.name is not None
                and item.body is not None
                and not self._can_prototype(item)
            )

        return [i for i in items if deduced(i)] + [i for i in items if not deduced(i)]

    def define_globals_pass(self, items: list) -> None:
        for item in items:
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                self.emit(f"namespace {item.name} {{")
                self.indent += 1
                self.define_globals_pass(item.members)
                self.indent -= 1
                self.emit("}")
                self.emit("")
            elif isinstance(item, ast.VarDecl) and not item.alias and item.name:
                self.emit(self.object_definition_code(item))
                self.emit("")

    # ------------------------------------------------------------------
    # Small declaration helpers
    # ------------------------------------------------------------------

    def using_decl_code(self, decl: ast.UsingDecl) -> str:
        if decl.is_namespace:
            return f"using namespace {decl.target};"
        return f"using {decl.target};"

    def object_alias_code(self, decl: ast.VarDecl) -> str:
        header = self._template_header(decl.template_params)
        prefix = header + " " if header else ""
        type_ = self.cpp_type(decl.type) if decl.type is not None else "auto"
        if isinstance(decl.init, ast.InitList):
            value = "{" + ", ".join(self.init_item_code(i) for i in decl.init.items) + "}"
            return f"{prefix}inline constexpr {type_} {decl.name}{value};"
        value = self.initializer_code(decl.init) if decl.init is not None else "{}"
        return f"{prefix}inline constexpr {type_} {decl.name} = {value};"

    def object_definition_code(self, decl: ast.VarDecl) -> str:
        header = self._template_header(decl.template_params)
        prefix = header + " " if header else ""
        if decl.type is None or isinstance(decl.type, ast.DeducedType):
            if decl.init is None:
                self.fail(f"object '{decl.name}' with a deduced type needs an initial value", decl)
            return f"{prefix}auto {decl.name} = {self.initializer_code(decl.init)};"
        type_ = self.cpp_type(decl.type)
        if decl.init is None:
            return f"{prefix}{type_} {decl.name}{{}};"
        if isinstance(decl.init, ast.InitList):
            return f"{prefix}{type_} {decl.name}{self.initializer_code(decl.init)};"
        return f"{prefix}{type_} {decl.name} = {decl.init and self.cxpr(decl.init)};"

    def initializer_code(self, init) -> str:
        if init is None:
            return ""
        if isinstance(init, ast.InitList):
            if not init.items:
                return "{}"
            return "{" + ", ".join(self.init_item_code(i) for i in init.items) + "}"
        return self.cxpr(init)

    def init_item_code(self, item) -> str:
        if isinstance(item, ast.DesignatedInit):
            return f".{item.name} = {self.cxpr(item.value)}"
        return self.cxpr(item)

    # ------------------------------------------------------------------
    # Return types
    # ------------------------------------------------------------------

    def return_info(self, decl: ast.FunctionDecl):
        """Return (kind, cpp_type).  kind: void, typed, named, named_multi,
        deduced, deduced_forward."""
        is_main = decl.name == "main" and self._inside_class is None
        r = decl.returns
        if r is None:
            if decl.body is not None and isinstance(decl.body.stmt, ast.ExprStmt):
                if is_main:
                    return ("void", "int")
                return ("deduced_forward", "decltype(auto)")
            return ("void", "int" if is_main else "void")
        if r.kind == "void":
            return ("void", "int" if is_main else "void")
        if getattr(r, "borrow_kind", None):
            if r.type is None:
                self.fail("a borrow return type needs a type", decl)
            inner = self.cpp_type(r.type)
            if r.borrow_kind == "in":
                return ("borrow", f"{inner} const&")
            if r.borrow_kind == "inout":
                return ("borrow", f"{inner}&")
            self.fail("'out' cannot be a return kind; use 'in' or 'inout'", decl)
        if r.kind == "named":
            if len(r.named) == 1:
                nr = r.named[0]
                if nr.type is None:
                    self.fail("named return values need an explicit type", decl)
                return ("named", self.cpp_type(nr.type))
            return ("named_multi", self.named_return_struct_name(decl))
        type_ = r.type
        if isinstance(type_, ast.ConstrainedType):
            type_ = type_.base
        if isinstance(type_, ast.DeducedType):
            if r.forward or r.forward_ref:
                return ("deduced_forward", "decltype(auto)")
            return ("deduced", "auto")
        if is_main:
            return ("typed", "int")
        return ("typed", self.cpp_type(type_))

    def named_return_struct_name(self, decl: ast.FunctionDecl) -> str:
        if self._inside_class is not None:
            return f"{decl.name}_epi__ret"
        return f"{decl.name}_epi__ret"

    def gen_named_return_struct(self, decl: ast.FunctionDecl) -> None:
        r = decl.returns
        assert r is not None and r.kind == "named"
        name = self.named_return_struct_name(decl)
        self.emit(f"struct {name} {{")
        self.indent += 1
        for nr in r.named:
            if nr.type is None:
                self.fail("named return values need an explicit type", decl)
            self.emit(f"{self.cpp_type(nr.type)} {nr.name};")
        self.indent -= 1
        self.emit("};")

    # ------------------------------------------------------------------
    # C++ type lowering
    # ------------------------------------------------------------------

    def cpp_type(self, t) -> str:
        if t is None:
            return "auto"
        if isinstance(t, ast.ConstrainedType):
            return self.cpp_type(t.base)
        if isinstance(t, ast.BorrowType):
            inner = self.cpp_type(t.inner)
            if t.kind == "in":
                return f"{inner} const&"
            if t.kind == "inout":
                return f"{inner}&"
            self.fail("'out' cannot be used as a value type; it is a parameter kind", t)
        if isinstance(t, ast.DeducedType):
            return "auto"
        if isinstance(t, ast.NamedType):
            name = t.name.text()
            if len(t.name.parts) == 1 and name in ("unique", "shared") and t.args:
                base = "std::unique_ptr" if name == "unique" else "std::shared_ptr"
                return f"{base}<{self.cpp_type_arg(t.args[0])}>"
            if name == "decltype" and t.args:
                return f"decltype({self.cxpr(t.args[0])})"
            if not t.args:
                return name
            return f"{name}<{', '.join(self.cpp_type_arg(a) for a in t.args)}>"
        if isinstance(t, ast.PointerType):
            if isinstance(t.inner, ast.FunctionType):
                return self.function_type_code(t.inner, func_pointer=True)
            return self.cpp_type(t.inner) + "*"
        if isinstance(t, ast.ConstType):
            inner = t.inner
            if isinstance(inner, ast.PointerType):
                if isinstance(inner.inner, ast.FunctionType):
                    return self.function_type_code(inner.inner, func_pointer=True, const_pointer=True)
                return self.cpp_type(inner.inner) + "* const"
            return self.cpp_type(inner) + " const"
        if isinstance(t, ast.FunctionType):
            return self.function_type_code(t)
        if isinstance(t, ast.TypeOfType):
            return f"std::remove_cvref_t<decltype({self.cxpr(t.expr)})>"
        self.fail(f"cannot lower this type to C++ (found {type(t).__name__})", t)

    def cpp_type_arg(self, arg) -> str:
        if isinstance(arg, ast.Expr):
            return self.cxpr(arg)
        return self.cpp_type(arg)

    def function_type_code(self, t: ast.FunctionType, func_pointer: bool = False,
                           const_pointer: bool = False) -> str:
        ret = "void"
        if t.returns is not None:
            if t.returns.kind == "void":
                ret = "void"
            elif t.returns.kind == "single":
                ret = self.cpp_type(t.returns.type)
            else:
                self.fail("named return values are not supported in function types", t)
        params = [self.param_type_code(p) for p in t.params]
        if func_pointer:
            suffix = "* const" if const_pointer else "*"
            return f"{ret} ({suffix})({', '.join(params)})"
        return f"{ret}({', '.join(params)})"

    # ------------------------------------------------------------------
    # Class definitions
    # ------------------------------------------------------------------

    def gen_type(self, decl: ast.TypeDecl) -> None:
        metas = self._metafunction_names(decl)
        if "enum" in metas:
            self.gen_enum(decl)
            return
        if "flag_enum" in metas:
            self.gen_flag_enum(decl)
            return
        for mf in metas:
            if mf not in SUPPORTED_METAFUNCTIONS:
                if mf in UNSUPPORTED_METAFUNCTIONS:
                    self.fail(
                        f"the @{mf} metafunction is not supported by Epitaxy 0.2; "
                        "supported metafunctions are @value, @copyable, @interface, "
                        "@enum, @flag_enum, the ordered family, @basic_value and "
                        "@singleton",
                        decl,
                    )
                self.fail(f"unknown metafunction '@{mf}'", decl)

        info = TypeInfo(decl)
        copyable = any(
            m in metas
            for m in ("copyable", "value", "basic_value",
                      "weakly_ordered_value", "partially_ordered_value")
        )
        if "value" in metas or "ordered" in metas:
            ordering = "strong"
        elif "weakly_ordered" in metas or "weakly_ordered_value" in metas:
            ordering = "weak"
        elif "partially_ordered" in metas:
            ordering = "partial"
        else:
            ordering = None
        singleton = "singleton" in metas

        prev_class = self._inside_class
        prev_access = self._current_access
        self._inside_class = info
        self._current_access = "private"

        header = self._template_header(decl.template_params)
        bases = []
        for b in info.bases:
            if b.type is None:
                self.fail("a base type needs a type", b)
            bases.append(f"public {self.cpp_type(b.type)}")
        decl_s = f"struct {decl.name}" + ((" : " + ", ".join(bases)) if bases else "")
        self.emit(f"{header + ' ' if header else ''}{decl_s} {{")
        self.indent += 1

        interface = "interface" in metas
        if interface and info.data_members:
            self.fail(
                "an @interface type cannot have data members; put state in a derived type",
                info.data_members[0],
            )
        for member in decl.members:
            if isinstance(member, ast.TypeDecl):
                if member.kind == "==":
                    self.set_access(member, "public")
                    h = self._template_header(member.template_params)
                    prefix = h + " " if h else ""
                    self.emit(f"{prefix}using {member.name} = {self.cpp_type(member.alias)};")
                else:
                    self.set_access(member, "public")
                    self.gen_type(member)
                continue
            if isinstance(member, ast.FunctionDecl):
                self.set_access(member, "public")
                self.gen_member_function(member, info, interface)
                continue
            if isinstance(member, ast.VarDecl):
                if member.name == "this":
                    continue
                self.set_access(member, "private")
                self.gen_member_object(member)
                continue
            if isinstance(member, ast.Preproc):
                self.emit(member.text)
                continue
            if isinstance(member, ast.Cpp1Block):
                self._emit_raw_text(member.text)
                continue
            if isinstance(member, ast.Enumerator):
                self.fail("enumerators are only allowed inside @enum types", member)
            if isinstance(member, ast.UsingDecl):
                self.set_access(member, "public")
                self.emit(self.using_decl_code(member))
                continue
            self.fail(f"cannot lower type member {type(member).__name__}", member)

        has_virtual = interface or self._has_virtual(info)
        has_destructor = any(
            f.is_operator and self.this_kind(f) == "move" and len(f.params) == 1
            for f in info.functions
        )
        if singleton:
            self.gen_singleton_members(info)
        self.gen_default_special_members(
            info, copyable=copyable, is_value=("value" in metas), singleton=singleton
        )
        if ordering is not None:
            self.gen_ordering_members(info, ordering)
        if has_virtual and not has_destructor:
            self.emit(f"virtual ~{decl.name}() = default;")

        self.indent -= 1
        self.emit("};")
        self._inside_class = prev_class
        self._current_access = prev_access

    def set_access(self, member, default: str) -> None:
        desired = getattr(member, "access", None) or default
        if desired != self._current_access:
            self.emit(f"{desired}:")
            self._current_access = desired

    def gen_member_object(self, member: ast.VarDecl) -> None:
        if member.alias:
            self.fail("constant data members are not supported by Epitaxy 0.2", member)
        if member.type is None:
            self.fail(f"data member '{member.name}' needs a type", member)
        type_ = self.cpp_type(member.type)
        if member.init is None:
            self.emit(f"{type_} {member.name}{{}};")
        elif isinstance(member.init, ast.InitList):
            self.emit(f"{type_} {member.name}{self.initializer_code(member.init)};")
        else:
            self.emit(f"{type_} {member.name} = {self.cxpr(member.init)};")

    def _has_virtual(self, info: TypeInfo) -> bool:
        for f in info.functions:
            p = self.this_param(f)
            if p and any(q in ("virtual", "override", "final") for q in p.qualifiers):
                return True
        return False

    def this_param(self, decl: ast.FunctionDecl):
        if decl.params and decl.params[0].is_this:
            return decl.params[0]
        return None

    def this_kind(self, decl: ast.FunctionDecl) -> Optional[str]:
        p = self.this_param(decl)
        if p is None:
            return None
        for q in ("out", "inout", "move", "in"):
            if q in p.qualifiers:
                return q
        return "in"

    def _is_pure(self, decl: ast.FunctionDecl) -> bool:
        return decl.body is None

    def gen_member_function(self, decl: ast.FunctionDecl, info: TypeInfo, interface: bool = False) -> None:
        if decl.is_operator and decl.operator == "=":
            self.gen_special_member_function(decl, info)
            return
        kind = self.return_info(decl)[0]
        if kind == "named_multi":
            self.gen_named_return_struct(decl)
        sig = self.function_signature(decl, prototype=False, class_info=info, force_virtual=interface)
        if decl.body is None:
            if interface or self._is_virtual_func(decl):
                self.emit(sig + " = 0;")
                return
            if decl.is_operator and decl.operator in ("<=>", "==") and self._is_defaulted_comparison(decl, info):
                if decl.operator == "<=>":
                    self.emit(f"auto operator<=>({info.name} const& that) const = default;")
                else:
                    self.emit(f"bool operator==({info.name} const& that) const = default;")
                return
            self.emit(sig + ";")
            return
        if decl.kind == "==":
            sig = self._add_constexpr(sig)
        self.emit(sig + " {")
        self.indent += 1
        self.gen_function_contents(decl, class_info=info)
        self.indent -= 1
        self.emit("}")

    def _is_defaulted_comparison(self, decl: ast.FunctionDecl, info: TypeInfo) -> bool:
        extra = [p for p in decl.params if not p.is_this]
        if len(extra) != 1 or not extra[0].is_that:
            return False
        return self.this_kind(decl) == "in"

    def _is_virtual_func(self, decl: ast.FunctionDecl) -> bool:
        p = self.this_param(decl)
        return bool(p and any(q in ("virtual", "override", "final") for q in p.qualifiers))

    # ------------------------------------------------------------------
    # Special members
    # ------------------------------------------------------------------

    def _special_contract_parts(self, decl: ast.FunctionDecl, ctx: FnCtx, fn_name: str):
        """Prepare captures/pre/post checks for a special member function."""
        post_specs = []
        for contract in decl.contracts:
            if self._is_unevaluated(contract):
                continue
            if contract.kind == "post":
                cond, msg, group, preds = self.prepare_contract(contract, ctx, capture=True)
                post_specs.append((cond, msg, group, preds, contract))
        captures = list(ctx.post_captures)
        post_checks = [
            self.contract_check_code(cond, msg, group, preds, contract, fn_name)
            for (cond, msg, group, preds, contract) in post_specs
        ]
        pre_checks = []
        for contract in decl.contracts:
            if self._is_unevaluated(contract) or contract.kind != "pre":
                continue
            cond, msg, group, preds = self.prepare_contract(contract, ctx, capture=False)
            pre_checks.append(
                self.contract_check_code(cond, msg, group, preds, contract, fn_name)
            )
        return captures, pre_checks, post_checks

    def gen_special_member_function(self, decl: ast.FunctionDecl, info: TypeInfo) -> None:
        this_kind = self.this_kind(decl)
        extra = [p for p in decl.params if not p.is_this]
        if decl.operator != "=":
            self.fail(
                "only operator= is supported for special member functions in Epitaxy 0.2", decl
            )
        if this_kind == "move":
            if extra:
                self.emit(self.function_signature(decl, prototype=False, class_info=info,
                                                  force_ref_qualifier=True) + " {")
                self.indent += 1
                self.gen_function_contents(decl, class_info=info)
                self.indent -= 1
                self.emit("}")
                return
            self.emit(f"~{info.name}() {{")
            self.indent += 1
            ctx = FnCtx(self)
            ctx.inside_class_name = info.name
            ctx.return_kind = "void"
            captures, pre_checks, post_checks = self._special_contract_parts(
                decl, ctx, info.name
            )
            for line in captures:
                self.emit(line)
            for check in pre_checks:
                self.emit(check)
            self.gen_block_contents(decl, ctx)
            for check in post_checks:
                self.emit(check)
            self.indent -= 1
            self.emit("}")
            return
        if this_kind == "out":
            self.gen_constructors(decl, info)
            return
        if this_kind == "inout":
            self.gen_assignments(decl, info)
            return
        self.fail("an operator= function needs an 'out', 'inout' or 'move' this parameter", decl)

    def gen_constructors(self, decl: ast.FunctionDecl, info: TypeInfo) -> None:
        extra = [p for p in decl.params if not p.is_this]
        this_p = self.this_param(decl)
        implicit = bool(this_p and "implicit" in this_p.qualifiers)
        if not extra:
            self.emit_constructor(decl, info, f"{info.name}()", "default")
            return
        if len(extra) == 1 and extra[0].is_that:
            self.emit_constructor(decl, info, f"{info.name}({info.name} const& that)", "copy")
            if "move" not in extra[0].qualifiers:
                self.emit_constructor(decl, info, f"{info.name}({info.name}&& that) noexcept", "move")
            self.gen_generated_assignments(info, "move" if "move" in extra[0].qualifiers else "copy")
            return
        if extra and not any(p.is_that for p in extra):
            params = ", ".join(self.param_decl(p) for p in extra)
            qualifier = "" if implicit else "explicit "
            self.emit_constructor(decl, info, f"{qualifier}{info.name}({params})", "value")
            has_inout = any(
                f.is_operator and f.operator == "=" and self.this_kind(f) == "inout"
                for f in info.functions
            )
            if len(extra) == 1 and not has_inout:
                self.emit_converting_assignment(decl, info, extra[0])
            return
        self.fail("unsupported operator= constructor parameter list", decl)

    def emit_constructor(self, decl, info: TypeInfo, signature: str, source: str,
                         value_param: Optional[str] = None) -> None:
        entries, rest = self.ctor_initializers(decl, info, source)
        self.emit(signature)
        if entries:
            self.emit("    : " + ", ".join(entries))
        self.emit("{")
        self.indent += 1
        ctx = FnCtx(self)
        ctx.inside_class_name = info.name
        ctx.return_kind = "void"
        captures, pre_checks, post_checks = self._special_contract_parts(
            decl, ctx, info.name
        )
        for line in captures:
            self.emit(line)
        for check in pre_checks:
            self.emit(check)
        for stmt in rest:
            self.gen_stmt(stmt, ctx)
        for check in post_checks:
            self.emit(check)
        self.indent -= 1
        self.emit("}")

    def ctor_initializers(self, decl, info: TypeInfo, source: str):
        explicit, rest = self.split_member_initializers(decl, info)
        entries = []
        for base in info.bases:
            base_type = self.cpp_type(base.type)
            if source in ("copy", "move"):
                arg = "that" if source == "copy" else "std::move(that)"
                entries.append(f"{base_type}({arg})")
            elif base.init is not None:
                init = self.initializer_code(base.init)
                entries.append(f"{base_type}{init}" if init.startswith("{") else f"{base_type}({init})")
            else:
                entries.append(f"{base_type}()")
        explicit_map = {name: rhs for name, rhs in explicit}
        for member in info.data_members:
            if member.name in explicit_map:
                entries.append(f"{member.name}({self.cxpr(explicit_map[member.name])})")
                continue
            if source in ("copy", "move"):
                if source == "copy":
                    entries.append(f"{member.name}(that.{member.name})")
                else:
                    entries.append(f"{member.name}(std::move(that.{member.name}))")
                continue
            if member.init is not None:
                init = self.initializer_code(member.init)
                entries.append(f"{member.name}{init}" if init.startswith("{") else f"{member.name}({init})")
        return entries, rest

    def split_member_initializers(self, decl, info: TypeInfo):
        member_names = set(info.member_names())
        if decl.body is None or not isinstance(decl.body.stmt, ast.Block):
            return [], []
        stmts = decl.body.stmt.stmts
        explicit = []
        idx = 0
        while idx < len(stmts):
            stmt = stmts[idx]
            if not isinstance(stmt, ast.ExprStmt):
                break
            e = stmt.expr
            if not (isinstance(e, ast.Assign) and e.op == "="):
                break
            if not (isinstance(e.lhs, ast.NameExpr) and len(e.lhs.name.parts) == 1):
                break
            name = e.lhs.name.parts[0]
            if name not in member_names:
                break
            if isinstance(e.rhs, ast.Id) and e.rhs.name == "_":
                idx += 1
                continue
            explicit.append((name, e.rhs))
            idx += 1
        return explicit, stmts[idx:]

    def gen_generated_assignments(self, info: TypeInfo, source: str) -> None:
        has_inout = any(f.is_operator and self.this_kind(f) == "inout" for f in info.functions)
        if has_inout:
            return
        self.emit_generated_assignment(info, "copy")
        if source == "copy":
            self.emit_generated_assignment(info, "move")

    def gen_assignments(self, decl: ast.FunctionDecl, info: TypeInfo) -> None:
        extra = [p for p in decl.params if not p.is_this]
        if len(extra) == 1 and extra[0].is_that:
            self.emit_assignment(decl, info, f"{info.name}& operator=({info.name} const& that)", "copy")
            if "move" not in extra[0].qualifiers:
                self.emit_assignment(decl, info, f"{info.name}& operator=({info.name}&& that) noexcept", "move")
            return
        if len(extra) == 1:
            p = extra[0]
            self.emit_converting_assignment(decl, info, p)
            return
        if not extra:
            self.emit_assignment(decl, info, f"{info.name}& operator=()", "default")
            return
        self.fail("unsupported operator= assignment parameter list", decl)

    def emit_assignment(self, decl, info: TypeInfo, signature: str, source: str,
                        value_param: Optional[str] = None) -> None:
        self.emit(signature + " {")
        self.indent += 1
        ctx = FnCtx(self)
        ctx.inside_class_name = info.name
        ctx.return_kind = "void"
        captures, pre_checks, post_checks = self._special_contract_parts(
            decl, ctx, "operator="
        )
        for line in captures:
            self.emit(line)
        for check in pre_checks:
            self.emit(check)
        explicit, rest = self.split_member_initializers(decl, info)
        mentioned = set()
        for name, rhs in explicit:
            mentioned.add(name)
            self.emit(f"{name} = {self.cxpr(rhs)};")
        if source in ("copy", "move"):
            for base in info.bases:
                base_type = self.cpp_type(base.type)
                arg = "that" if source == "copy" else "std::move(that)"
                self.emit(f"{base_type}::operator=({arg});")
        for member in info.data_members:
            if member.name in mentioned:
                continue
            if source == "copy":
                self.emit(f"{member.name} = that.{member.name};")
            elif source == "move":
                self.emit(f"{member.name} = std::move(that.{member.name});")
            elif member.init is not None:
                self.emit(f"{member.name} = {self.initializer_code(member.init)};")
        for stmt in rest:
            self.gen_stmt(stmt, ctx)
        for check in post_checks:
            self.emit(check)
        self.emit("return *this;")
        self.indent -= 1
        self.emit("}")

    def emit_generated_assignment(self, info: TypeInfo, source: str) -> None:
        signature = (
            f"{info.name}& operator=({info.name} const& that)"
            if source == "copy"
            else f"{info.name}& operator=({info.name}&& that) noexcept"
        )
        self.emit(signature + " {")
        self.indent += 1
        for base in info.bases:
            base_type = self.cpp_type(base.type)
            arg = "that" if source == "copy" else "std::move(that)"
            self.emit(f"{base_type}::operator=({arg});")
        for member in info.data_members:
            if source == "copy":
                self.emit(f"{member.name} = that.{member.name};")
            else:
                self.emit(f"{member.name} = std::move(that.{member.name});")
        self.emit("return *this;")
        self.indent -= 1
        self.emit("}")

    def emit_converting_assignment(self, decl, info: TypeInfo, param: ast.Param) -> None:
        param_decl = self.param_decl(param)
        self.emit(f"{info.name}& operator=({param_decl}) {{")
        self.indent += 1
        ctx = FnCtx(self)
        ctx.inside_class_name = info.name
        ctx.return_kind = "void"
        captures, pre_checks, post_checks = self._special_contract_parts(
            decl, ctx, "operator="
        )
        for line in captures:
            self.emit(line)
        for check in pre_checks:
            self.emit(check)
        if decl.body is not None and isinstance(decl.body.stmt, ast.Block):
            explicit, rest = self.split_member_initializers(decl, info)
        else:
            explicit, rest = [], []
        mentioned = set()
        for name, rhs in explicit:
            mentioned.add(name)
            self.emit(f"{name} = {self.cxpr(rhs)};")
        for member in info.data_members:
            if member.name in mentioned:
                continue
            if member.init is not None:
                self.emit(f"{member.name} = {self.initializer_code(member.init)};")
        ctx = FnCtx(self)
        ctx.inside_class_name = info.name
        ctx.return_kind = "void"
        for stmt in rest:
            self.gen_stmt(stmt, ctx)
        self.emit("return *this;")
        self.indent -= 1
        self.emit("}")

    def gen_default_special_members(self, info: TypeInfo, copyable: bool = False,
                                    is_value: bool = False, singleton: bool = False) -> None:
        user_ops = [f for f in info.functions if f.is_operator and f.operator == "="]
        if singleton:
            return
        if is_value or copyable:
            if user_ops:
                return
            self.emit(f"{info.name}() = default;")
            self.emit(f"{info.name}({info.name} const&) = default;")
            self.emit(f"{info.name}({info.name}&&) = default;")
            self.emit(f"{info.name}& operator=({info.name} const&) = default;")
            self.emit(f"{info.name}& operator=({info.name}&&) = default;")
            return
        if not user_ops and "interface" not in info.metafunctions:
            self.emit(f"{info.name}() = default;")

    # ------------------------------------------------------------------
    # Metafunction members: @singleton and the ordered family
    # ------------------------------------------------------------------

    def _ensure_public(self) -> None:
        if self._current_access != "public":
            self.emit("public:")
            self._current_access = "public"

    def gen_singleton_members(self, info: TypeInfo) -> None:
        user_ops = [f for f in info.functions if f.is_operator and f.operator == "="]
        if self._current_access != "private":
            self.emit("private:")
            self._current_access = "private"
        if not user_ops:
            self.emit(f"{info.name}() = default;")
            self.emit(f"{info.name}({info.name} const&) = delete;")
            self.emit(f"{info.name}({info.name}&&) = delete;")
            self.emit(f"{info.name}& operator=({info.name} const&) = delete;")
            self.emit(f"{info.name}& operator=({info.name}&&) = delete;")
        self._ensure_public()
        self.emit(
            f"static {info.name}& instance() {{ static {info.name} epi__instance{{}}; "
            "return epi__instance; }"
        )

    def gen_ordering_members(self, info: TypeInfo, kind: str) -> None:
        user_eq = any(f.is_operator and f.operator == "==" for f in info.functions)
        user_cmp = any(f.is_operator and f.operator == "<=>" for f in info.functions)
        self._ensure_public()
        if not user_eq:
            self.emit(f"bool operator==({info.name} const&) const = default;")
        if user_cmp:
            return
        if kind == "strong":
            self.emit(f"auto operator<=>({info.name} const&) const = default;")
            return
        ret = "std::weak_ordering" if kind == "weak" else "std::partial_ordering"
        self.emit(f"{ret} operator<=>({info.name} const& that) const {{")
        self.indent += 1
        for base in info.bases:
            base_type = self.cpp_type(base.type)
            self.emit(
                f"if (auto epi__cmp = static_cast<{base_type} const&>(*this) <=> "
                f"static_cast<{base_type} const&>(that); epi__cmp != 0) "
                f"{{ return static_cast<{ret}>(epi__cmp); }}"
            )
        for member in info.data_members:
            self.emit(
                f"if (auto epi__cmp = this->{member.name} <=> that.{member.name}; "
                f"epi__cmp != 0) {{ return static_cast<{ret}>(epi__cmp); }}"
            )
        self.emit(f"return {ret}::equivalent;")
        self.indent -= 1
        self.emit("}")

    # ------------------------------------------------------------------
    # @enum
    # ------------------------------------------------------------------

    def _metafunction_names(self, decl: ast.TypeDecl) -> list[str]:
        return [name for name, _args in decl.metafunctions]

    def _is_enum(self, decl: ast.TypeDecl) -> bool:
        return "enum" in self._metafunction_names(decl)

    def _is_flag_enum(self, decl: ast.TypeDecl) -> bool:
        return "flag_enum" in self._metafunction_names(decl)

    # ------------------------------------------------------------------
    # @flag_enum
    # ------------------------------------------------------------------

    def _flag_bits(self, underlying: str) -> int:
        widths = {
            "u8": 8, "i8": 8, "u16": 16, "i16": 16, "u32": 32, "i32": 32,
            "u64": 64, "i64": 64, "ushort": 16, "uint": 32, "ulong": 64,
            "ulonglong": 64, "longlong": 64,
        }
        return widths.get(underlying, 32)

    def _eval_flag_value(self, expr, values: dict, mask: int):
        if isinstance(expr, ast.Literal) and expr.kind == "number":
            return _try_int_literal(expr.raw) if hasattr(expr, "raw") else None
        if isinstance(expr, ast.NameExpr) and len(expr.name.parts) == 1:
            return values.get(expr.name.parts[0])
        if isinstance(expr, ast.Unary) and expr.op == "~":
            inner = self._eval_flag_value(expr.expr, values, mask)
            return None if inner is None else (~inner) & mask
        if isinstance(expr, ast.Binary) and expr.op in ("|", "&", "^", "<<", ">>"):
            left = self._eval_flag_value(expr.left, values, mask)
            right = self._eval_flag_value(expr.right, values, mask)
            if left is None or right is None:
                return None
            if expr.op == "|":
                return (left | right) & mask
            if expr.op == "&":
                return left & right
            if expr.op == "^":
                return (left ^ right) & mask
            if expr.op == "<<":
                return (left << right) & mask
            return left >> right
        return None

    def gen_flag_enum(self, decl: ast.TypeDecl) -> None:
        if self._inside_class is not None:
            self.fail("nested @flag_enum types are not supported by Epitaxy 0.2", decl)
        args = None
        for name, meta_args in decl.metafunctions:
            if name == "flag_enum":
                args = meta_args
                break
        underlying = self.cpp_type(args[0]) if args else "u32"
        bits = self._flag_bits(underlying)
        mask = (1 << bits) - 1
        enumerators = [m for m in decl.members if isinstance(m, ast.Enumerator)]
        functions = [m for m in decl.members if isinstance(m, ast.FunctionDecl)]
        if not enumerators:
            self.fail("@flag_enum types need at least one enumerator", decl)
        for member in decl.members:
            if isinstance(member, ast.VarDecl):
                self.fail("@flag_enum types cannot have data members in Epitaxy 0.2", member)

        values: list[tuple[str, int]] = []
        seen: dict[str, int] = {}
        next_bit = 1
        for enumerator in enumerators:
            if enumerator.value is None:
                value = next_bit
                next_bit <<= 1
            else:
                value = self._eval_flag_value(enumerator.value, seen, mask)
                if value is None:
                    self.fail(
                        f"cannot evaluate flag value for '{enumerator.name}'; use "
                        "integer literals and bitwise combinations of earlier enumerators",
                        enumerator,
                    )
            if enumerator.name in seen:
                self.fail(f"duplicate enumerator '{enumerator.name}'", enumerator)
            seen[enumerator.name] = value
            values.append((enumerator.name, value))
            if value:
                candidate = 1 << (value.bit_length() - 1)
                if candidate >= next_bit:
                    next_bit = candidate << 1

        name = decl.name
        self.emit(f"struct {name} {{")
        self.indent += 1
        self.emit(
            f"enum class epi__value_t : {underlying} {{ "
            + ", ".join(f"{n} = {v}" for n, v in values)
            + " };"
        )
        self.emit(f"using epi__underlying_t = {underlying};")
        self.emit("epi__value_t epi__value{};")
        self.emit("")
        self.emit(f"constexpr {name}() = default;")
        self.emit(f"constexpr {name}(epi__value_t value) : epi__value{{value}} {{}}")
        self.emit(f"constexpr {name}({name} const&) = default;")
        self.emit(f"constexpr {name}& operator=({name} const&) = default;")
        self.emit(
            f"constexpr {name}& operator=(epi__value_t value) "
            "{ epi__value = value; return *this; }"
        )
        self.emit(f"constexpr epi__value_t epi__get() const noexcept {{ return epi__value; }}")
        for enumerator_name, _value in values:
            self.emit(
                f"static constexpr epi__value_t {enumerator_name} = "
                f"epi__value_t::{enumerator_name};"
            )
        self.emit("static constexpr epi__value_t none = static_cast<epi__value_t>(0);")
        self.emit("")
        self.emit(
            f"constexpr bool has(epi__value_t flags) const noexcept "
            "{ return (static_cast<epi__underlying_t>(epi__value) & "
            "static_cast<epi__underlying_t>(flags)) == "
            "static_cast<epi__underlying_t>(flags); }"
        )
        self.emit(f"constexpr bool has({name} const& flags) const noexcept {{ return has(flags.epi__get()); }}")
        self.emit(
            f"constexpr void set(epi__value_t flags) noexcept "
            "{ epi__value = static_cast<epi__value_t>("
            "static_cast<epi__underlying_t>(epi__value) | "
            "static_cast<epi__underlying_t>(flags)); }"
        )
        self.emit(f"constexpr void set({name} const& flags) noexcept {{ set(flags.epi__get()); }}")
        self.emit(
            f"constexpr void clear(epi__value_t flags) noexcept "
            "{ epi__value = static_cast<epi__value_t>("
            "static_cast<epi__underlying_t>(epi__value) & "
            "~static_cast<epi__underlying_t>(flags)); }"
        )
        self.emit(f"constexpr void clear({name} const& flags) noexcept {{ clear(flags.epi__get()); }}")
        enum_info = _SimpleInfo(name)
        for f in functions:
            self.gen_member_function(f, enum_info)
        self.indent -= 1
        self.emit("};")
        self.emit("")
        underlying_access = f"{name}::epi__underlying_t"
        value_t = f"{name}::epi__value_t"
        # Namespace-scope bitwise operators for the underlying enumerator type.
        for op, cppop in (("|", "|"), ("&", "&"), ("^", "^")):
            self.emit(
                f"constexpr {value_t} operator{cppop}({value_t} a, {value_t} b) noexcept "
                f"{{ return static_cast<{value_t}>(static_cast<{underlying_access}>(a) "
                f"{cppop} static_cast<{underlying_access}>(b)); }}"
            )
        self.emit(
            f"constexpr {value_t} operator~({value_t} a) noexcept "
            f"{{ return static_cast<{value_t}>(~static_cast<{underlying_access}>(a)); }}"
        )
        for cppop in ("|", "&", "^"):
            self.emit(
                f"constexpr {name} operator{cppop}({name} a, {name} b) noexcept "
                f"{{ return {name}(operator{cppop}(a.epi__get(), b.epi__get())); }}"
            )
        self.emit(f"constexpr {name} operator~({name} a) noexcept {{ return {name}(~a.epi__get()); }}")
        self.emit(f"constexpr bool operator==({name} a, {name} b) noexcept {{ return a.epi__get() == b.epi__get(); }}")
        self.emit(f"constexpr bool operator!=({name} a, {name} b) noexcept {{ return !(a == b); }}")
        self.emit("")
        self.emit(f"inline std::string to_string({name} const& value) {{")
        self.emit("    switch (value.epi__get()) {")
        for enumerator_name, _value in values:
            self.emit(
                f'        case {name}::epi__value_t::{enumerator_name}: '
                f'return "{enumerator_name}";'
            )
        self.emit(
            '        default: return std::format("0x{:x}", '
            f"static_cast<unsigned long long>(value.epi__get()));"
        )
        self.emit("    }")
        self.emit("}")
        self.emit(
            f"inline std::ostream& operator<<(std::ostream& os, {name} const& value) "
            "{ return os << to_string(value); }"
        )

    def gen_enum(self, decl: ast.TypeDecl) -> None:
        if self._inside_class is not None:
            self.fail("nested @enum types are not supported by Epitaxy 0.2", decl)
        mf, args = decl.metafunctions[0]
        if mf == "flag_enum":
            self.fail("@flag_enum is not supported by Epitaxy 0.2; use @enum", decl)
        underlying = self.cpp_type(args[0]) if args else "i32"
        enumerators = [m for m in decl.members if isinstance(m, ast.Enumerator)]
        if not enumerators:
            self.fail("@enum types need at least one enumerator", decl)
        functions = [m for m in decl.members if isinstance(m, ast.FunctionDecl)]
        for m in decl.members:
            if isinstance(m, ast.VarDecl):
                self.fail("@enum types cannot have data members in Epitaxy 0.2", m)

        values = []
        next_value: Optional[int] = 1
        for e in enumerators:
            if e.value is not None:
                code = self.cxpr(e.value)
                values.append((e.name, code))
                parsed = _try_int_literal(code)
                next_value = None if parsed is None else parsed + 1
            else:
                if next_value is None:
                    self.fail("cannot auto-number this enumerator after a computed value", e)
                values.append((e.name, str(next_value)))
                next_value += 1

        self.emit(f"struct {decl.name} {{")
        self.indent += 1
        self.emit(
            f"enum class epi__value_t : {underlying} {{ "
            + ", ".join(f"{n} = {v}" for n, v in values)
            + " };"
        )
        self.emit(f"epi__value_t epi__value{{epi__value_t::{values[0][0]}}};")
        self.emit("")
        self.emit(f"constexpr {decl.name}() = default;")
        self.emit(f"constexpr {decl.name}(epi__value_t value) : epi__value{{value}} {{}}")
        self.emit(f"constexpr {decl.name}({decl.name} const&) = default;")
        self.emit(f"constexpr {decl.name}& operator=({decl.name} const&) = default;")
        self.emit(
            f"constexpr {decl.name}& operator=(epi__value_t value) {{ epi__value = value; return *this; }}"
        )
        self.emit(f"constexpr epi__value_t epi__get() const noexcept {{ return epi__value; }}")
        for name, _ in values:
            self.emit(f"static constexpr epi__value_t {name} = epi__value_t::{name};")
        self.emit("")
        self.emit(
            f"friend constexpr bool operator==({decl.name} const& a, {decl.name} const& b) "
            "{ return a.epi__value == b.epi__value; }"
        )
        self.emit(
            f"friend constexpr bool operator==({decl.name} const& a, epi__value_t b) "
            "{ return a.epi__value == b; }"
        )
        self.emit(
            f"friend constexpr bool operator==(epi__value_t a, {decl.name} const& b) "
            "{ return a == b.epi__value; }"
        )
        self.emit(
            f"friend constexpr bool operator!=({decl.name} const& a, {decl.name} const& b) "
            "{ return a.epi__value != b.epi__value; }"
        )
        self.emit(
            f"friend constexpr bool operator!=({decl.name} const& a, epi__value_t b) "
            "{ return a.epi__value != b; }"
        )
        self.emit(
            f"friend constexpr bool operator!=(epi__value_t a, {decl.name} const& b) "
            "{ return a != b.epi__value; }"
        )
        self.emit(
            f"friend constexpr auto operator<=>({decl.name} const& a, {decl.name} const& b) "
            "{ return a.epi__value <=> b.epi__value; }"
        )
        enum_info = _SimpleInfo(decl.name)
        for f in functions:
            self.gen_member_function(f, enum_info)
        self.indent -= 1
        self.emit("};")
        self.emit("")
        self.emit(f"constexpr std::string_view to_string({decl.name} const& value) {{")
        self.emit("    switch (value.epi__get()) {")
        for name, _ in values:
            self.emit(f"        case {decl.name}::epi__value_t::{name}: return \"{name}\";")
        self.emit(f'        default: return "(invalid {decl.name})";')
        self.emit("    }")
        self.emit("}")
        self.emit(
            f"inline std::ostream& operator<<(std::ostream& os, {decl.name} const& value) "
            "{ return os << to_string(value); }"
        )

    # ------------------------------------------------------------------
    # Functions
    # ------------------------------------------------------------------

    def function_signature(self, decl: ast.FunctionDecl, prototype: bool,
                           class_info: Optional[TypeInfo] = None,
                           force_ref_qualifier: bool = False,
                           force_virtual: bool = False) -> str:
        is_main = decl.name == "main" and class_info is None
        if is_main:
            params = decl.params
            if not params:
                return "int main()"
            if len(params) == 1 and params[0].type is None:
                return "int main(int argc, char** argv)"
            self.fail("main may have no parameters or one parameter of implicit type", decl)

        header = self._template_header(decl.template_params)
        kind, ret_type = self.return_info(decl)
        name = decl.name or ""
        if decl.is_operator and decl.operator != "=":
            name = "operator" + decl.operator

        modifiers = []
        qualifiers = []
        suffixes = []
        this_p = self.this_param(decl)
        this_k = self.this_kind(decl) if this_p is not None else None
        if this_p is not None:
            if "virtual" in this_p.qualifiers or force_virtual:
                modifiers.append("virtual")
            for q in ("override", "final"):
                if q in this_p.qualifiers:
                    suffixes.append(q)
            if this_k == "in":
                qualifiers.append("const")
            elif this_k == "move" and force_ref_qualifier:
                qualifiers.append("&&")

        params = []
        constraints = []
        for p in decl.params:
            if p.is_this:
                continue
            if p.is_that:
                base = class_info.name if class_info else "auto"
                params.append(f"{base}&& that" if "move" in p.qualifiers else f"{base} const& that")
                continue
            params.append(self.param_decl(p))
            c = self.param_constraint(p)
            if c:
                constraints.append(c)

        requires = []
        if decl.requires is not None:
            requires.append(f"({self.cxpr(decl.requires)})")
        requires.extend(constraints)

        nodiscard = ""
        if kind not in ("void",) and not decl.is_operator:
            nodiscard = "[[nodiscard]] "

        head = " ".join(modifiers + [ret_type])
        sig = f"{nodiscard}{head} {name}({', '.join(params)})"
        if qualifiers:
            sig += " " + " ".join(qualifiers)
        if suffixes:
            sig += " " + " ".join(suffixes)
        if requires:
            sig += " requires " + " && ".join(requires)
        if header:
            sig = header + " " + sig
        return sig

    def param_constraint(self, p: ast.Param) -> Optional[str]:
        t = p.type
        if not isinstance(t, ast.ConstrainedType):
            return None
        if isinstance(t.base, ast.DeducedType) and p.name:
            constraint = t.constraint
            if isinstance(constraint, ast.NamedType):
                return f"requires {self.cpp_type(constraint)}<std::remove_cvref_t<decltype({p.name})>>"
        return None

    def param_kind(self, p: ast.Param) -> str:
        for q in ("in_ref", "forward_ref", "in", "inout", "out", "copy", "move", "forward"):
            if q in p.qualifiers:
                return q
        return "in"

    def param_decl(self, p: ast.Param) -> str:
        name = p.name or ""
        t = p.type
        if isinstance(t, ast.ConstrainedType):
            t = t.base
        deduced = t is None or isinstance(t, ast.DeducedType)
        kind = self.param_kind(p)
        variadic = p.variadic
        if deduced:
            mapping = {
                "in": "auto const&",
                "in_ref": "auto const&",
                "inout": "auto&",
                "out": "auto&",
                "copy": "auto",
                "move": "auto&&",
                "forward": "auto&&",
                "forward_ref": "auto&&",
            }
            decl = f"{mapping[kind]}... {name}" if variadic else f"{mapping[kind]} {name}".strip()
        else:
            tcode = self.cpp_type(t)
            if isinstance(t, ast.BorrowType):
                decl = f"{tcode} {name}".strip()
                if p.default is not None:
                    decl += f" = {self.cxpr(p.default)}"
                return decl
            if variadic:
                decl = f"{tcode}... {name}".strip()
            else:
                mapping = {
                    "in": self.in_param_type(t),
                    "in_ref": f"{tcode} const&",
                    "inout": f"{tcode}&",
                    "out": f"{tcode}&",
                    "copy": f"{tcode}",
                    "move": f"{tcode}&&",
                    "forward": f"{tcode}&&",
                    "forward_ref": f"{tcode}&&",
                }
                decl = f"{mapping[kind]} {name}".strip()
        if p.default is not None:
            decl += f" = {self.cxpr(p.default)}"
        return decl

    def in_param_type(self, t) -> str:
        tcode = self.cpp_type(t)
        if isinstance(t, ast.PointerType) or isinstance(t, ast.FunctionType):
            return f"{tcode} const"
        if isinstance(t, ast.NamedType) and t.args is None:
            name = t.name.text()
            if name in BY_VALUE_TYPE_NAMES:
                return f"{tcode} const"
        return f"{tcode} const&"

    def param_type_code(self, p: ast.Param) -> str:
        t = p.type
        if isinstance(t, ast.ConstrainedType):
            t = t.base
        if t is None or isinstance(t, ast.DeducedType):
            return "auto"
        return self.cpp_type(t)

    def gen_function(self, decl: ast.FunctionDecl) -> None:
        sig = self.function_signature(decl, prototype=False)
        if decl.kind == "==":
            sig = self._add_constexpr(sig)
        self.emit(sig + " {")
        self.indent += 1
        self.gen_function_contents(decl, class_info=None)
        self.indent -= 1
        self.emit("}")

    def gen_function_contents(self, decl: ast.FunctionDecl, class_info: Optional[TypeInfo]) -> None:
        ctx = FnCtx(self)
        ctx.inside_class_name = class_info.name if class_info else None
        kind, ret_type = self.return_info(decl)
        ctx.return_kind = kind
        ctx.return_type = ret_type
        ctx.is_main = decl.name == "main" and class_info is None
        r = decl.returns
        if r is not None and getattr(r, "borrow_kind", None):
            ctx.return_borrow = r.borrow_kind
        if self.ownership_info is not None:
            ctx.no_move_names = set(self.ownership_info.borrow_names.get(id(decl), ()))
        if r is not None and r.kind == "named":
            ctx.named_returns = r.named
            if len(r.named) > 1:
                ctx.named_struct = self.named_return_struct_name(decl)
            else:
                ctx.named_local = r.named[0].name

        if decl.body is not None:
            ctx.move_wrap = analyze_moves(decl)
            if ctx.no_move_names:
                ctx.move_wrap = {
                    k: v for k, v in ctx.move_wrap.items() if k not in ()
                }
            ctx.goto_mode = contains_loop_control(decl.body.stmt)

        # Named return storage.
        if ctx.named_struct:
            self.emit(f"{ctx.named_struct} epi__ret{{}};")
            for nr in ctx.named_returns:
                self.emit(f"auto& {nr.name} = epi__ret.{nr.name};")
        elif ctx.named_local:
            self.emit(f"{ctx.return_type} {ctx.named_local}{{}};")

        # Postconditions are captured on entry.
        post_specs = []
        for contract in decl.contracts:
            if self._is_unevaluated(contract):
                continue
            if contract.kind == "post":
                cond, msg, group, preds = self.prepare_contract(contract, ctx, capture=True)
                post_specs.append((cond, msg, group, preds, contract))
        for cap in ctx.post_captures:
            self.emit(cap)
        ctx.post_checks = [
            self.contract_check_code(cond, msg, group, preds, contract, decl.name or "function")
            for (cond, msg, group, preds, contract) in post_specs
        ]

        for contract in decl.contracts:
            if self._is_unevaluated(contract):
                continue
            if contract.kind == "pre":
                self.gen_contract_check(contract, ctx, decl.name or "function")

        if ctx.is_main and decl.params:
            pname = decl.params[0].name or "args"
            self.emit(f"epi::args_t {pname}{{argc, argv}};")

        if decl.body is None:
            return
        if isinstance(decl.body.stmt, ast.ExprStmt):
            body_expr = decl.body.stmt.expr
            if ctx.return_borrow and isinstance(body_expr, ast.Postfix) and body_expr.op == "&":
                code = self.cxpr(body_expr.expr, ctx)
            else:
                code = self.cxpr(body_expr, ctx)
            if kind == "void":
                self.emit(f"(void)({code});")
            else:
                self.gen_return_value_expr(code, ctx)
        else:
            for stmt in decl.body.stmt.stmts:
                self.gen_stmt(stmt, ctx)

        if kind == "void":
            for check in ctx.post_checks:
                self.emit(check)
            if ctx.is_main:
                self.emit("return 0;")
        elif ctx.is_main and not ctx.has_return:
            for check in ctx.post_checks:
                self.emit(check)
            self.emit("return 0;")
        elif ctx.named_struct is not None and not ctx.has_return:
            for check in ctx.post_checks:
                self.emit(check)
            self.emit("return epi__ret;")
        elif ctx.named_local is not None and not ctx.has_return:
            for check in ctx.post_checks:
                self.emit(check)
            self.emit(f"return {ctx.named_local};")

    def _add_constexpr(self, signature: str) -> str:
        if signature.startswith("[[nodiscard]] "):
            return "[[nodiscard]] constexpr " + signature[len("[[nodiscard]] "):]
        return "constexpr " + signature

    def gen_block_contents(self, decl: ast.FunctionDecl, ctx: FnCtx) -> None:
        if decl.body is None:
            return
        if isinstance(decl.body.stmt, ast.Block):
            for stmt in decl.body.stmt.stmts:
                self.gen_stmt(stmt, ctx)
        else:
            self.gen_stmt(decl.body.stmt, ctx)

    def gen_return_value_expr(self, expr_code: str, ctx: FnCtx) -> None:
        ctx.has_return = True
        if ctx.post_checks:
            tmp = self.gensym("ret")
            self.emit(f"decltype(auto) {tmp} = {expr_code};")
            for check in ctx.post_checks:
                self.emit(check)
            self.emit(f"return {tmp};")
        else:
            self.emit(f"return {expr_code};")

    def gen_return_stmt(self, stmt: ast.Return, ctx: FnCtx) -> None:
        ctx.has_return = True
        if stmt.expr is not None and ctx.return_borrow and isinstance(stmt.expr, ast.Postfix) \
                and stmt.expr.op == "&":
            code = self.cxpr(stmt.expr.expr, ctx)
        else:
            code = self.cxpr(stmt.expr, ctx) if stmt.expr is not None else None
        if ctx.return_kind == "named":
            for check in ctx.post_checks:
                self.emit(check)
            if code is not None:
                self.emit(f"return {code};")
            else:
                self.emit(f"return {ctx.named_local};")
            return
        if ctx.return_kind == "named_multi":
            for check in ctx.post_checks:
                self.emit(check)
            self.emit(f"return {code if code is not None else 'epi__ret'};")
            return
        if ctx.return_kind == "void":
            for check in ctx.post_checks:
                self.emit(check)
            self.emit("return 0;" if ctx.is_main else "return;")
            return
        if code is None:
            for check in ctx.post_checks:
                self.emit(check)
            self.emit("return;")
            return
        self.gen_return_value_expr(code, ctx)

    # ------------------------------------------------------------------
    # Contracts
    # ------------------------------------------------------------------

    def _is_unevaluated(self, contract) -> bool:
        groups = getattr(contract, "groups", None) or []
        if not groups:
            return False
        expr = groups[0]
        return (
            isinstance(expr, ast.NameExpr)
            and len(expr.name.parts) == 1
            and expr.name.parts[0] == "unevaluated"
        )

    def contract_group_code(self, contract, ctx: FnCtx) -> str:
        groups = contract.groups
        if not groups:
            return "::epi::default_"
        expr = groups[0]
        if isinstance(expr, ast.NameExpr) and len(expr.name.parts) == 1:
            name = expr.name.parts[0]
            if name in CONTRACT_GROUPS:
                return CONTRACT_GROUPS[name]
            if name == "this":
                return "(*this)"
            return name
        if isinstance(expr, ast.Id) and expr.name == "this":
            return "(*this)"
        return self.cxpr(expr, ctx)

    def contract_message_code(self, contract, ctx: FnCtx) -> str:
        if contract.message is None:
            return '"contract violation"'
        code = self.cxpr(contract.message, ctx)
        if code.startswith('"') and code.endswith('"'):
            return code
        return f"std::string({code})"

    def prepare_contract(self, contract, ctx: FnCtx, capture: bool):
        cond = contract.cond
        if capture and cond is not None:
            cond = self.substitute_captures(cond, ctx, "post")
        cond_cpp = self.cxpr(cond, ctx) if cond is not None else "false"
        msg_cpp = self.contract_message_code(contract, ctx)
        group_cpp = self.contract_group_code(contract, ctx)
        preds_cpp = [self.cxpr(p, ctx) for p in contract.predicates]
        return cond_cpp, msg_cpp, group_cpp, preds_cpp

    def contract_check_code(self, cond, msg, group, preds, contract, fn_name: str) -> str:
        prefix = (f"{os.path.basename(self.filename)}:{getattr(contract, 'line', 0)}: "
                  f"{fn_name}: {getattr(contract, 'kind', 'contract')}condition failed: ")
        message = f'std::string("{_escape_cxx_string(prefix)}") + std::string({msg})'
        conditions = list(preds) + [f"{group}.is_active()", f"!({cond})"]
        return f"if ({' && '.join(conditions)}) {{ {group}.report_violation({message}); }}"

    def gen_contract_check(self, contract, ctx: FnCtx, fn_name: str) -> None:
        cond_cpp, msg_cpp, group_cpp, preds_cpp = self.prepare_contract(contract, ctx, capture=False)
        self.emit(self.contract_check_code(cond_cpp, msg_cpp, group_cpp, preds_cpp, contract, fn_name))

    def substitute_captures(self, node, ctx: FnCtx, hint: str):
        def walk(value):
            if isinstance(value, list):
                return [walk(v) for v in value]
            if isinstance(value, tuple):
                return tuple(walk(v) for v in value)
            if isinstance(value, ast.Postfix) and getattr(value, "op", None) == "$":
                name = self.gensym(hint)
                ctx.post_captures.append(f"auto {name} = {self.cxpr(value.expr, ctx)};")
                return ast.Id(name=name, line=value.line, col=value.col)
            if dataclasses.is_dataclass(value):
                for f in dataclasses.fields(value):
                    setattr(value, f.name, walk(getattr(value, f.name)))
                return value
            return value

        return walk(node)

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    def gen_stmt(self, stmt, ctx: FnCtx) -> None:
        if stmt is None:
            return
        if isinstance(stmt, ast.Block):
            self.emit("{")
            self.indent += 1
            for s in stmt.stmts:
                self.gen_stmt(s, ctx)
            self.indent -= 1
            self.emit("}")
        elif isinstance(stmt, ast.Preproc):
            self.emit(stmt.text)
        elif isinstance(stmt, ast.Cpp1Block):
            self._emit_raw_text(stmt.text)
        elif isinstance(stmt, ast.UsingDecl):
            self.emit(self.using_decl_code(stmt))
        elif isinstance(stmt, ast.VarDecl):
            self.gen_local_var(stmt, ctx)
        elif isinstance(stmt, ast.ExprStmt):
            self.emit(self.cxpr(stmt.expr, ctx) + ";")
        elif isinstance(stmt, ast.Return):
            self.gen_return_stmt(stmt, ctx)
        elif isinstance(stmt, ast.If):
            self.gen_if(stmt, ctx)
        elif isinstance(stmt, ast.While):
            self.gen_while(stmt, ctx)
        elif isinstance(stmt, ast.DoWhile):
            self.gen_do_while(stmt, ctx)
        elif isinstance(stmt, ast.For):
            self.gen_for(stmt, ctx)
        elif isinstance(stmt, ast.Break):
            self.gen_break(stmt, ctx)
        elif isinstance(stmt, ast.Continue):
            self.gen_continue(stmt, ctx)
        elif isinstance(stmt, ast.TryCatch):
            self.gen_try(stmt, ctx)
        elif isinstance(stmt, ast.Throw):
            self.emit(f"throw {self.cxpr(stmt.expr, ctx)};")
        elif isinstance(stmt, ast.Assert):
            if self._is_unevaluated(stmt):
                return
            cond, msg, group, preds = self.prepare_contract(stmt, ctx, capture=False)
            self.emit(self.contract_check_code(cond, msg, group, preds, stmt, "assert"))
        elif isinstance(stmt, ast.UnsafeBlock):
            self.emit("{")
            self.indent += 1
            if isinstance(stmt.stmt, ast.Block):
                for inner in stmt.stmt.stmts:
                    self.gen_stmt(inner, ctx)
            else:
                self.gen_stmt(stmt.stmt, ctx)
            self.indent -= 1
            self.emit("}")
        elif isinstance(stmt, ast.RaiiScope):
            self.gen_raii(stmt, ctx)
        elif isinstance(stmt, ast.Enumerator):
            self.fail("enumerators are only allowed inside @enum types", stmt)
        else:
            self.fail(f"cannot lower statement {type(stmt).__name__} to C++", stmt)

    def gen_local_var(self, decl: ast.VarDecl, ctx: FnCtx) -> None:
        if decl.name is None:
            self.fail("a local object needs a name", decl)
        if isinstance(decl.type, ast.BorrowType):
            if decl.init is None:
                self.fail(f"borrow '{decl.name}' needs an initial value", decl)
            init = decl.init
            if isinstance(init, ast.Postfix) and init.op == "&":
                init_code = self.cxpr(init.expr, ctx)
            else:
                init_code = self.cxpr(init, ctx)
            ctx.no_move_names.add(decl.name)
            self.emit(f"{self.cpp_type(decl.type)} {decl.name} = {init_code};")
            return
        if decl.alias:
            if decl.type is None:
                self.fail("a local constant alias needs a type", decl)
            t = self.cpp_type(decl.type)
            if isinstance(decl.init, ast.InitList):
                self.emit(f"constexpr {t} {decl.name}{self.initializer_code(decl.init)};")
            else:
                self.emit(f"constexpr {t} {decl.name} = {self.cxpr(decl.init)};")
            return
        if decl.type is None or isinstance(decl.type, ast.DeducedType):
            if decl.init is None:
                self.fail(f"object '{decl.name}' with a deduced type needs an initial value", decl)
            deduced_borrow = None
            if self.ownership_info is not None:
                deduced_borrow = self.ownership_info.deduced_borrow.get(id(decl))
            if deduced_borrow is not None:
                init = decl.init
                if isinstance(init, ast.Postfix) and init.op == "&":
                    init_code = self.cxpr(init.expr, ctx)
                else:
                    init_code = self.cxpr(init, ctx)
                base = "auto const&" if deduced_borrow == "in" else "auto&"
                ctx.no_move_names.add(decl.name)
                self.emit(f"{base} {decl.name} = {init_code};")
                return
            if isinstance(decl.init, ast.InitList):
                if not decl.init.items:
                    self.fail("an empty initialiser cannot deduce a type", decl)
                self.emit(
                    f"auto {decl.name} = {{"
                    + ", ".join(self.init_item_code(i) for i in decl.init.items)
                    + "};"
                )
            else:
                self.emit(f"auto {decl.name} = {self.cxpr(decl.init, ctx)};")
            self.gen_constraint_assert(decl, ctx)
            return
        type_ = self.cpp_type(decl.type)
        if decl.init is None:
            self.emit(f"{type_} {decl.name}{{}};")
        elif isinstance(decl.init, ast.InitList):
            self.emit(f"{type_} {decl.name}{self.initializer_code(decl.init)};")
        else:
            self.emit(f"{type_} {decl.name} = {self.cxpr(decl.init)};")
        self.gen_constraint_assert(decl, ctx)

    def gen_constraint_assert(self, decl: ast.VarDecl, ctx: FnCtx) -> None:
        if not isinstance(decl.type, ast.ConstrainedType):
            return
        constraint = decl.type.constraint
        if isinstance(constraint, ast.NamedType):
            concept = self.cpp_type(constraint)
            self.emit(
                f"static_assert({concept}<std::remove_cvref_t<decltype({decl.name})>>, "
                f"\"Epitaxy: '{decl.name}' does not satisfy its type constraint\");"
            )

    def gen_if(self, stmt: ast.If, ctx: FnCtx) -> None:
        cond = self.cxpr(stmt.cond, ctx)
        self.emit(f"if ({cond}) {{")
        self.indent += 1
        for s in stmt.then.stmts:
            self.gen_stmt(s, ctx)
        self.indent -= 1
        if stmt.else_ is None:
            self.emit("}")
            return
        if isinstance(stmt.else_, ast.If):
            self.emit("} else {")
            self.indent += 1
            self.gen_if(stmt.else_, ctx)
            self.indent -= 1
            self.emit("}")
            return
        self.emit("} else {")
        self.indent += 1
        for s in stmt.else_.stmts:
            self.gen_stmt(s, ctx)
        self.indent -= 1
        self.emit("}")

    def _new_loop_labels(self, ctx: FnCtx, label: Optional[str]) -> tuple[str, str, str]:
        ctx.loop_counter += 1
        suffix = str(ctx.loop_counter)
        return (label or "", f"epi__brk_{suffix}", f"epi__cont_{suffix}")

    def gen_while(self, stmt: ast.While, ctx: FnCtx) -> None:
        name, brk, cont = self._new_loop_labels(ctx, stmt.label)
        cond = self.cxpr(stmt.cond, ctx)
        ctx.loop_stack.append((name, brk, cont))
        if ctx.goto_mode:
            self.emit(f"while ({cond}) {{")
            self.indent += 1
            self._emit_loop_body_struct(stmt.body, ctx)
            self.emit(f"{cont}: ;")
            if stmt.next_expr is not None:
                self.emit(self.cxpr(stmt.next_expr, ctx) + ";")
            self.indent -= 1
            self.emit("}")
            self.emit(f"{brk}: ;")
        elif stmt.next_expr is not None:
            self.emit(f"for (; {cond}; {self.cxpr(stmt.next_expr, ctx)}) {{")
            self.indent += 1
            for s in stmt.body.stmts:
                self.gen_stmt(s, ctx)
            self.indent -= 1
            self.emit("}")
        else:
            self.emit(f"while ({cond}) {{")
            self.indent += 1
            for s in stmt.body.stmts:
                self.gen_stmt(s, ctx)
            self.indent -= 1
            self.emit("}")
        ctx.loop_stack.pop()

    def _emit_loop_body_struct(self, body: ast.Block, ctx: FnCtx) -> None:
        self.emit("{")
        self.indent += 1
        for s in body.stmts:
            self.gen_stmt(s, ctx)
        self.indent -= 1
        self.emit("}")

    def gen_do_while(self, stmt: ast.DoWhile, ctx: FnCtx) -> None:
        name, brk, cont = self._new_loop_labels(ctx, stmt.label)
        ctx.loop_stack.append((name, brk, cont))
        cond = self.cxpr(stmt.cond, ctx)
        self.emit("do {")
        self.indent += 1
        self._emit_loop_body_struct(stmt.body, ctx)
        self.emit(f"{cont}: ;")
        if stmt.next_expr is not None:
            self.emit(self.cxpr(stmt.next_expr, ctx) + ";")
        self.indent -= 1
        self.emit(f"}} while ({cond});")
        self.emit(f"{brk}: ;")
        ctx.loop_stack.pop()

    def range_code(self, node, ctx: FnCtx) -> str:
        if isinstance(node, ast.CommaList):
            return "{" + ", ".join(self.cxpr(i, ctx) for i in node.items) + "}"
        if isinstance(node, ast.Range):
            first = self.cxpr(node.first, ctx)
            last = self.cxpr(node.last, ctx)
            func = "closed_range" if node.closed else "range"
            return f"::epi::{func}({first}, {last})"
        return self.cxpr(node, ctx)

    def gen_for(self, stmt: ast.For, ctx: FnCtx) -> None:
        name, brk, cont = self._new_loop_labels(ctx, stmt.label)
        ctx.loop_stack.append((name, brk, cont))
        rng = self.range_code(stmt.range, ctx)
        p = stmt.param
        pname = p.name or self.gensym("_")
        kind = self.param_kind(p)
        attr = "[[maybe_unused]] " if p.is_wildcard or p.name is None else ""
        if kind == "copy":
            decl = f"{attr}auto {pname}"
        elif kind in ("inout", "out", "move", "forward"):
            decl = f"{attr}auto&& {pname}"
        else:
            decl = f"{attr}auto const& {pname}"
        self.emit(f"for ({decl} : {rng}) {{")
        self.indent += 1
        if isinstance(stmt.body, ast.Block):
            self._emit_loop_body_struct(stmt.body, ctx)
        else:
            self.emit("{")
            self.indent += 1
            self.gen_stmt(stmt.body, ctx)
            self.indent -= 1
            self.emit("}")
        self.emit(f"{cont}: ;")
        if stmt.next_expr is not None:
            self.emit(self.cxpr(stmt.next_expr, ctx) + ";")
        self.indent -= 1
        self.emit("}")
        self.emit(f"{brk}: ;")
        ctx.loop_stack.pop()

    def find_loop(self, ctx: FnCtx, label: Optional[str]):
        if label is None:
            return ctx.loop_stack[-1] if ctx.loop_stack else None
        for entry in reversed(ctx.loop_stack):
            if entry[0] == label:
                return entry
        return None

    def gen_break(self, stmt: ast.Break, ctx: FnCtx) -> None:
        loop = self.find_loop(ctx, stmt.label)
        if loop is None:
            self.fail("break outside a matching loop", stmt)
        self.emit(f"goto {loop[1]};")

    def gen_continue(self, stmt: ast.Continue, ctx: FnCtx) -> None:
        loop = self.find_loop(ctx, stmt.label)
        if loop is None:
            self.fail("continue outside a matching loop", stmt)
        self.emit(f"goto {loop[2]};")

    def gen_try(self, stmt: ast.TryCatch, ctx: FnCtx) -> None:
        self.emit("try {")
        self.indent += 1
        for s in stmt.body.stmts:
            self.gen_stmt(s, ctx)
        self.indent -= 1
        first = True
        for catch in stmt.catches:
            if catch.is_catchall:
                self.emit(("} catch (...) {" if first else "} catch (...) {"))
            else:
                p = catch.param
                t = self.cpp_type(p.type) if p.type is not None else "std::exception"
                pname = p.name or self.gensym("e")
                self.emit(f"}} catch ({t} const& {pname}) {{")
            first = False
            self.indent += 1
            for s in catch.body.stmts:
                self.gen_stmt(s, ctx)
            self.indent -= 1
        self.emit("}")

    def gen_raii(self, stmt: ast.RaiiScope, ctx: FnCtx) -> None:
        self.emit("{")
        self.indent += 1
        for p in stmt.params:
            name = p.name or self.gensym("scoped")
            init = p.default
            if p.type is None or isinstance(p.type, ast.DeducedType):
                if init is None:
                    self.fail("a scoped parameter with a deduced type needs an initial value", p)
                self.emit(f"auto {name} = {self.cxpr(init, ctx)};")
            else:
                t = self.cpp_type(p.type)
                if init is None:
                    self.emit(f"{t} {name};")
                elif isinstance(init, ast.InitList):
                    self.emit(f"{t} {name}{self.initializer_code(init)};")
                else:
                    self.emit(f"{t} {name} = {self.cxpr(init, ctx)};")
        self.gen_stmt(stmt.body, ctx)
        self.indent -= 1
        self.emit("}")

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    def cxpr(self, node, ctx: Optional[FnCtx] = None) -> str:
        if node is None:
            return "void()"
        if ctx is None:
            ctx = FnCtx(self)
        method = getattr(self, "_expr_" + type(node).__name__.lower(), None)
        if method is None:
            self.fail(f"cannot lower expression {type(node).__name__} to C++", node)
        return method(node, ctx)

    def _expr_literal(self, node: ast.Literal, ctx) -> str:
        return node.raw

    def _expr_stringliteral(self, node: ast.StringLiteral, ctx) -> str:
        parts = node.parts
        prefix = getattr(node, "prefix", "") or ""
        if getattr(node, "raw_string", False):
            return prefix + node.raw_text
        has_interp = any(isinstance(p, ast.StrInterp) for p in parts)
        if not has_interp:
            text = "".join(p.text for p in parts if isinstance(p, ast.StrLit))
            return f'{prefix}"{text}"'
        has_spec = any(isinstance(p, ast.StrInterp) and p.spec is not None for p in parts)
        if has_spec:
            fmt = []
            args = []
            for p in parts:
                if isinstance(p, ast.StrLit):
                    fmt.append(p.text.replace("{", "{{").replace("}", "}}"))
                else:
                    spec = p.spec if p.spec is not None else ""
                    fmt.append("{" + (":" + spec if spec else "") + "}")
                    args.append(self.cxpr(p.expr, ctx))
            return f'std::format("{prefix}' + "".join(fmt) + f'", {", ".join(args)})'
        pieces = []
        for p in parts:
            if isinstance(p, ast.StrLit):
                pieces.append(f'"{prefix}{p.text}"')
            else:
                pieces.append(f"::epi::to_string({self.cxpr(p.expr, ctx)})")
        return f"::epi::concat({', '.join(pieces)})"

    def _expr_id(self, node: ast.Id, ctx) -> str:
        name = node.name
        if name in ctx.no_move_names:
            wrap = None
        else:
            wrap = ctx.move_wrap.get(id(node))
        if wrap == "move":
            return f"std::move({name})"
        if wrap == "forward":
            return f"std::forward<decltype({name})>({name})"
        if name == "this":
            return "(*this)"
        if name == "that":
            return "that"
        if name == "_":
            self.fail("'_' can only be used as a discard target or initialiser", node)
        return name

    def _expr_nameexpr(self, node: ast.NameExpr, ctx) -> str:
        parts = node.name.parts
        text = node.name.text()
        if node.template_args is not None:
            text += self.template_args_code(node.template_args, ctx)
        if len(parts) == 1 and not node.name.leading_global:
            if text in ctx.no_move_names:
                return text
            wrap = ctx.move_wrap.get(id(node))
            if wrap == "move":
                return f"std::move({text})"
            if wrap == "forward":
                return f"std::forward<decltype({text})>({text})"
        return text

    def _expr_qualifiednameexpr(self, node: ast.QualifiedNameExpr, ctx) -> str:
        parts = []
        for name, args in node.segments:
            if args:
                parts.append(name + self.template_args_code(args, ctx))
            else:
                parts.append(name)
        return "::".join(parts)

    def _expr_parenthesized(self, node: ast.Parenthesized, ctx) -> str:
        return f"({self.cxpr(node.expr, ctx)})"

    def _expr_unary(self, node: ast.Unary, ctx) -> str:
        inner = self.cxpr(node.expr, ctx)
        if node.op == "-" and self.options.strict_safety:
            return f"::epi::checked_neg({inner})"
        return f"({node.op}{inner})"

    def _expr_postfix(self, node: ast.Postfix, ctx) -> str:
        op = node.op
        inner = self.cxpr(node.expr, ctx)
        if op == "*":
            return f"::epi::deref({inner})" if self.options.null_checks else f"(*{inner})"
        if op == "&":
            return f"::epi::address({inner})"
        if op == "~":
            return f"(~{inner})"
        if op == "++":
            return f"::epi::checked_inc({inner})" if self.options.strict_safety else f"(++{inner})"
        if op == "--":
            return f"::epi::checked_dec({inner})" if self.options.strict_safety else f"(--{inner})"
        if op == "$":
            return inner
        self.fail(f"unsupported postfix operator '{op}'", node)

    def _expr_binary(self, node: ast.Binary, ctx) -> str:
        op = node.op
        lhs = self.cxpr(node.left, ctx)
        rhs = self.cxpr(node.right, ctx)
        if op in EQUALITY_OPS:
            return f"({lhs} {op} {rhs})"
        if op == "/":
            return f"::epi::div({lhs}, {rhs})" if self.options.div_checks else f"({lhs} / {rhs})"
        if op == "%":
            return f"::epi::mod({lhs}, {rhs})" if self.options.div_checks else f"({lhs} % {rhs})"
        if self.options.strict_safety:
            checked = {
                "+": "checked_add",
                "-": "checked_sub",
                "*": "checked_mul",
                "<<": "checked_shl",
                ">>": "checked_shr",
            }.get(op)
            if checked is not None:
                return f"::epi::{checked}({lhs}, {rhs})"
        return f"({lhs} {op} {rhs})"

    def _expr_comparechain(self, node: ast.CompareChain, ctx) -> str:
        operands = [node.first] + [expr for _op, expr in node.rest]
        ops = [op for op, _expr in node.rest]
        codes = [self.cxpr(o, ctx) for o in operands]
        if len(ops) > 2:
            self.fail("comparison chains with more than three operands are not supported", node)
        if len(ops) == 2:
            tmp = self.gensym("cmp")
            cmp1 = self.comparison_code(ops[0], codes[0], tmp)
            cmp2 = self.comparison_code(ops[1], tmp, codes[2])
            return f"([&]{{ auto&& {tmp} = {codes[1]}; return {cmp1} && {cmp2}; }}())"
        return f"({self.comparison_code(ops[0], codes[0], codes[1])})"

    def comparison_code(self, op: str, lhs: str, rhs: str) -> str:
        if self.options.sign_checks and op in RELATIONAL_OPS:
            return f"::epi::{COMPARISON_FUNCS[op]}({lhs}, {rhs})"
        return f"({lhs} {op} {rhs})"

    def _expr_assign(self, node: ast.Assign, ctx) -> str:
        rhs = self.cxpr(node.rhs, ctx)
        if isinstance(node.lhs, ast.Id) and node.lhs.name == "_":
            return f"((void)({rhs}))"
        lhs = self.cxpr(node.lhs, ctx)
        if node.op in ("/=", "%=") and self.options.div_checks:
            helper = "checked_div_assign" if node.op == "/=" else "checked_mod_assign"
            return f"::epi::{helper}({lhs}, {rhs})"
        if self.options.strict_safety:
            assign_helpers = {
                "+=": "checked_add_assign",
                "-=": "checked_sub_assign",
                "*=": "checked_mul_assign",
            }.get(node.op)
            if assign_helpers is not None:
                return f"::epi::{assign_helpers}({lhs}, {rhs})"
        return f"({lhs} {node.op} {rhs})"

    def _expr_call(self, node: ast.Call, ctx) -> str:
        args = [self.cxpr(a, ctx) for a in node.args]
        callee = node.callee
        if isinstance(callee, ast.Member):
            obj = self.cxpr(callee.obj, ctx)
            if callee.member_only:
                name = callee.name.split("::")[-1]
                if callee.template_args:
                    targs = self.template_args_code(callee.template_args, ctx)
                    return f"{obj}.template {name}{targs}({', '.join(args)})"
                return f"{obj}.{name}({', '.join(args)})"
            if "::" in callee.name:
                return f"{callee.name}({obj}{', ' if args else ''}{', '.join(args)})"
            if callee.template_args:
                targs = self.template_args_code(callee.template_args, ctx)
                return f"{callee.name}{targs}({obj}{', ' if args else ''}{', '.join(args)})"
            return f"EPI_UFCS({callee.name})({obj}{', ' if args else ''}{', '.join(args)})"
        base = self.cxpr(callee, ctx)
        if node.template_args is not None:
            return f"{base}{self.template_args_code(node.template_args, ctx)}({', '.join(args)})"
        return f"{base}({', '.join(args)})"

    def template_args_code(self, args: list, ctx) -> str:
        return "<" + ", ".join(self.cpp_type_arg(a) for a in args) + ">"

    def _expr_member(self, node: ast.Member, ctx) -> str:
        obj = self.cxpr(node.obj, ctx)
        return f"{obj}.{node.name.split('::')[-1]}"

    def _expr_subscript(self, node: ast.Subscript, ctx) -> str:
        obj = self.cxpr(node.obj, ctx)
        idx = self.cxpr(node.index, ctx)
        if self.options.subscript_checks:
            return f"::epi::subscript({obj}, {idx})"
        return f"({obj}[{idx}])"

    def _expr_is(self, node: ast.Is, ctx) -> str:
        return self.is_query_code(node.expr, node.constraint, ctx)

    def is_query_code(self, value, constraint, ctx) -> str:
        val = self.cxpr(value, ctx)
        if isinstance(constraint, ast.IsWildcard):
            return "true"
        if isinstance(constraint, ast.Expr):
            return f"::epi::is_value({val}, {self.cxpr(constraint, ctx)})"
        if isinstance(constraint, ast.PointerType) and isinstance(constraint.inner, ast.NamedType):
            return f"::epi::is_polymorphic<{self.cpp_type(constraint.inner)}>({val})"
        if isinstance(constraint, ast.ConstrainedType):
            constraint = constraint.base
        return f"::epi::is<{self.cpp_type(constraint)}>({val})"

    def _expr_as(self, node: ast.As, ctx) -> str:
        return f"::epi::as<{self.cpp_type(node.type)}>({self.cxpr(node.expr, ctx)})"

    def _expr_range(self, node: ast.Range, ctx) -> str:
        first = self.cxpr(node.first, ctx)
        last = self.cxpr(node.last, ctx)
        func = "closed_range" if node.closed else "range"
        return f"::epi::{func}({first}, {last})"

    def _expr_inspect(self, node: ast.Inspect, ctx) -> str:
        alts = node.alts
        if not alts:
            self.fail("inspect needs at least one alternative", node)
        last = alts[-1]
        if not isinstance(last.constraint, ast.IsWildcard):
            self.fail("the last inspect alternative must be 'is _' (a catch-all)", node)
        value_code = self.cxpr(node.expr, ctx)
        tmp = self.gensym("inspect")
        ret = self.cpp_type(node.type) if node.type is not None else ""
        lines = [f"[&]() -> {ret} {{" if ret else "[&]() {"]
        lines.append(f"    auto&& {tmp} = {value_code};")
        for alt in alts[:-1]:
            cond = self.is_query_code(ast.Id(name=tmp, line=alt.line, col=alt.col), alt.constraint, ctx)
            lines.append(f"    if ({cond}) {{ return {self.cxpr(alt.value, ctx)}; }}")
        lines.append(f"    return {self.cxpr(last.value, ctx)};")
        lines.append("}()")
        return "\n            ".join(lines)

    def _expr_lambda(self, node: ast.Lambda, ctx) -> str:
        lambda_ctx = FnCtx(self)
        lambda_ctx.in_lambda = True
        if node.returns is not None and node.returns.kind == "void":
            lambda_ctx.return_kind = "void"
        else:
            lambda_ctx.return_kind = "value"
        captures = self.collect_lambda_captures(node, lambda_ctx)
        template_header = self._template_header(node.template_params)
        params = ", ".join(self.param_decl(p) for p in node.params)
        ret = ""
        if node.returns is not None:
            _kind, rtype = self.return_info(
                ast.FunctionDecl(name=None, returns=node.returns, params=node.params, body=node.body)
            )
            ret = f" -> {rtype}"
        elif isinstance(node.body.stmt, ast.ExprStmt):
            ret = " -> decltype(auto)"
        capture_list = "&"
        if captures:
            capture_list += ", " + ", ".join(f"{name} = {expr}" for name, expr in captures)
        prefix = template_header + " " if template_header else ""
        head = f"{prefix}[{capture_list}]({params}){ret} {{"
        if isinstance(node.body.stmt, ast.ExprStmt):
            body = f"    return {self.cxpr(node.body.stmt.expr, lambda_ctx)};"
        else:
            saved_lines, saved_indent = self.lines, self.indent
            self.lines = []
            self.indent = 1
            for s in node.body.stmt.stmts:
                self.gen_stmt(s, lambda_ctx)
            body = "\n".join(self.lines)
            self.lines, self.indent = saved_lines, saved_indent
        return "\n            ".join([head, body, "}"])

    def collect_lambda_captures(self, node: ast.Lambda, lambda_ctx: FnCtx):
        captures: list[tuple[str, str]] = []

        def walk(value):
            if isinstance(value, list):
                return [walk(v) for v in value]
            if isinstance(value, tuple):
                return tuple(walk(v) for v in value)
            if isinstance(value, ast.Lambda) and value is not node:
                return value
            if isinstance(value, ast.Postfix) and getattr(value, "op", None) == "$":
                name = self.gensym("cap")
                captures.append((name, self.cxpr(value.expr, lambda_ctx)))
                return ast.Id(name=name, line=value.line, col=value.col)
            if dataclasses.is_dataclass(value):
                for f in dataclasses.fields(value):
                    setattr(value, f.name, walk(getattr(value, f.name)))
                return value
            return value

        walk(node.body)
        return captures

    def _expr_temporary(self, node: ast.Temporary, ctx) -> str:
        t = self.cpp_type(node.type)
        init = node.init
        if isinstance(init, ast.InitList):
            return f"{t}()" if not init.items else t + self.initializer_code(init)
        return f"{t}({self.cxpr(init, ctx)})"

    def _expr_new(self, node: ast.New, ctx) -> str:
        t = self.cpp_type(node.type) if node.type is not None else "auto"
        args = [self.cxpr(a, ctx) for a in node.args]
        maker = "std::make_shared" if node.ownership == "shared" else "std::make_unique"
        return f"{maker}<{t}>({', '.join(args)})"

    def _expr_move(self, node: ast.Move, ctx) -> str:
        return f"std::move({self.cxpr(node.expr, ctx)})"

    def _expr_forward(self, node: ast.Forward, ctx) -> str:
        inner = self.cxpr(node.expr, ctx)
        return f"std::forward<decltype({inner})>({inner})"

    def _expr_fold(self, node: ast.Fold, ctx) -> str:
        pack = self.cxpr(node.pack, ctx)
        if node.init is not None:
            return f"({self.cxpr(node.init, ctx)} {node.op} ... {node.op} {pack})"
        if node.left:
            return f"({pack} {node.op} ...)"
        return f"(... {node.op} {pack})"

    def _expr_packexpansion(self, node: ast.PackExpansion, ctx) -> str:
        return f"{self.cxpr(node.expr, ctx)}..."

    def _expr_sizeofpack(self, node: ast.SizeofPack, ctx) -> str:
        return f"sizeof...({self.cxpr(node.expr, ctx)})"

    def _expr_unsafeexpr(self, node: ast.UnsafeExpr, ctx) -> str:
        return f"({self.cxpr(node.expr, ctx)})"

    def _expr_outarg(self, node: ast.OutArg, ctx) -> str:
        return self.cxpr(node.expr, ctx)

    def _expr_commalist(self, node: ast.CommaList, ctx) -> str:
        self.fail("a parenthesised comma list is only valid as a for-loop range", node)


def _escape_cxx_string(text: str) -> str:
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        else:
            out.append(ch)
    return "".join(out)


def _try_int_literal(text: str):
    cleaned = text.replace("'", "")
    cleaned = cleaned.rstrip("uUlLzZ")
    try:
        return int(cleaned, 0)
    except ValueError:
        return None


def contains_loop_control(node) -> bool:
    if isinstance(node, (ast.Break, ast.Continue)):
        return True
    if isinstance(node, ast.Node):
        for f in dataclasses.fields(node):
            if contains_loop_control(getattr(node, f.name)):
                return True
        return False
    if isinstance(node, list):
        return any(contains_loop_control(v) for v in node)
    if isinstance(node, tuple):
        return any(contains_loop_control(v) for v in node)
    return False
