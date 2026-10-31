"""Unit tests for the Epitaxy lexer, parser, code generator and CLI.

Run with::

    python3 -m unittest discover -s tests/unit -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from epitaxy import ast  # noqa: E402
from epitaxy.codegen import CodeGen, Options  # noqa: E402
from epitaxy.compiler import transpile  # noqa: E402
from epitaxy.diag import CompileError  # noqa: E402
from epitaxy.lexer import Lexer  # noqa: E402
from epitaxy.parser import parse  # noqa: E402


def kinds(source: str) -> list[str]:
    return [t.kind for t in Lexer(source).tokenize() if t.kind != "eof"]


def texts(source: str) -> list[str]:
    return [t.text for t in Lexer(source).tokenize() if t.kind != "eof"]


class LexerTests(unittest.TestCase):
    def test_comments_are_skipped(self):
        self.assertEqual(kinds("// line\n/* stream */ x"), ["ident"])

    def test_numbers(self):
        self.assertEqual(
            texts("1 0xFF 0b101 1'000'000 1.5e3 42ull"),
            ["1", "0xFF", "0b101", "1'000'000", "1.5e3", "42ull"],
        )

    def test_operators_longest_match(self):
        self.assertEqual(texts("a..<b ..= <=> -> :: :="), ["a", "..<", "b", "..=", "<=>", "->", "::", ":="])

    def test_string_interpolation_parts(self):
        tokens = [t for t in Lexer('"a (x)$ b (y:.2f)$ c"').tokenize() if t.kind == "string"]
        parts = tokens[0].parts
        self.assertEqual(len(parts), 5)
        self.assertEqual(parts[0], "a ")
        self.assertEqual(parts[1].src, "x")
        self.assertIsNone(parts[1].spec)
        self.assertEqual(parts[3].src, "y")
        self.assertEqual(parts[3].spec, ".2f")

    def test_parentheses_without_dollar_are_literal(self):
        tokens = [t for t in Lexer('"f(x)"').tokenize() if t.kind == "string"]
        self.assertEqual(tokens[0].parts, ["f(x)"])

    def test_raw_strings(self):
        tokens = [t for t in Lexer('R"(no (interp)$ here)"').tokenize() if t.kind == "string"]
        self.assertTrue(tokens[0].raw_string)

    def test_escaped_dollar_is_literal(self):
        tokens = [t for t in Lexer(r'"cost \$ and (x)"').tokenize() if t.kind == "string"]
        self.assertEqual(len(tokens[0].parts), 1)

    def test_cpp1_raw_block(self):
        tokens = Lexer('cpp1 { struct S { int a; }; /* } */ }\nx').tokenize()
        self.assertEqual(tokens[0].kind, "cpp1")
        self.assertIn("struct S", tokens[0].text)
        self.assertEqual(tokens[1].text, "x")

    def test_identifier_escape(self):
        tokens = Lexer("__identifier__struct").tokenize()
        self.assertEqual(tokens[0].text, "struct")
        self.assertTrue(tokens[0].escaped_ident)

    def test_preprocessor_line(self):
        tokens = Lexer("#include <vector>\nx").tokenize()
        self.assertEqual(tokens[0].kind, "preproc")
        self.assertEqual(tokens[0].text, "#include <vector>")

    def test_escaped_identifier(self):
        tokens = Lexer("__identifier__inspect: i32 = 1;").tokenize()
        self.assertEqual(tokens[0].text, "inspect")
        self.assertTrue(tokens[0].escaped_ident)

    def test_unterminated_string_reports_position(self):
        with self.assertRaises(CompileError) as ctx:
            Lexer('x := "abc', "t.ep").tokenize()
        self.assertIn("unterminated string", ctx.exception.message)
        self.assertEqual(ctx.exception.filename, "t.ep")


class ParserTests(unittest.TestCase):
    def test_function_forms(self):
        tu = parse(
            """
            main: () = { }
            add: (a: i32, b: i32) -> i32 = a + b;
            generic: (x) = x;
            alias: (i: i32) -> _ == i * i;
            """
        )
        kinds_ = [type(item).__name__ for item in tu.items]
        self.assertEqual(kinds_, ["FunctionDecl"] * 4)
        self.assertEqual(tu.items[1].returns.kind, "single")
        self.assertIsNone(tu.items[2].returns)
        self.assertEqual(tu.items[3].kind, "==")

    def test_declaration_forms(self):
        tu = parse(
            """
            x := 1;
            y: i32 = 2;
            z: _ = 3;
            BufferSize: i32 == 1000;
            imap: <T> type == std::map<i32, T>;
            n: namespace == std::chrono;
            chr: namespace = { main: () = { } }
            point: type = { x: i32 = 0; }
            color: @enum type = { red; green; }
            """
        )
        self.assertIsInstance(tu.items[0], ast.VarDecl)
        self.assertTrue(tu.items[0].deduced)
        self.assertTrue(tu.items[3].alias)
        self.assertIsInstance(tu.items[4], ast.TypeDecl)
        self.assertEqual(tu.items[4].kind, "==")
        self.assertIsInstance(tu.items[5], ast.NamespaceDecl)
        self.assertEqual(tu.items[5].kind, "==")
        self.assertIsInstance(tu.items[6], ast.NamespaceDecl)
        self.assertIsInstance(tu.items[7], ast.TypeDecl)
        self.assertEqual(tu.items[8].metafunctions[0][0], "enum")

    def test_operator_declaration(self):
        tu = parse("point: type = { operator=: (out this, that) = { } }")
        member = tu.items[0].members[0]
        self.assertTrue(member.is_operator)
        self.assertEqual(member.operator, "=")
        self.assertEqual(member.params[0].name, None)
        self.assertTrue(member.params[0].is_this)

    def test_types_left_to_right(self):
        tu = parse("p: const * * const i32 = ();")
        t = tu.items[0].type
        # const (pointer (pointer (const i32)))
        self.assertIsInstance(t, ast.ConstType)
        self.assertIsInstance(t.inner, ast.PointerType)
        self.assertIsInstance(t.inner.inner, ast.PointerType)
        self.assertIsInstance(t.inner.inner.inner, ast.ConstType)

    def test_statement_forms(self):
        tu = parse(
            """
            main: () = {
                if true { }
                while false next 1 { }
                do { } next 1 while false;
                for 1 ..= 3 do (i) { }
                try { throw(1); } catch (e: i32) { } catch (_) { }
                assert(true);
                _ = 1;
            }
            """
        )
        stmts = tu.items[0].body.stmt.stmts
        self.assertEqual(
            [type(s).__name__ for s in stmts],
            ["If", "While", "DoWhile", "For", "TryCatch", "Assert", "ExprStmt"],
        )

    def test_inspect_requires_catchall(self):
        tu = parse("f: (x) -> _ = inspect x -> i32 { is 0 = 1; is _ = 2; };")
        inspect = tu.items[0].body.stmt.expr
        self.assertIsInstance(inspect, ast.Inspect)
        self.assertEqual(len(inspect.alts), 2)

    def test_parse_error_location(self):
        with self.assertRaises(CompileError) as ctx:
            parse("main: () = {\n  x := ;\n}", "bad.ep")
        self.assertIn("expected an expression", ctx.exception.message)
        self.assertEqual(ctx.exception.line, 2)
        self.assertEqual(ctx.exception.filename, "bad.ep")

    def test_contracts(self):
        tu = parse(
            """
            f: (inout x: i32)
                pre(x > 0, "positive")
                post(x == x$ + 1)
            = { x++; }
            """
        )
        contracts = tu.items[0].contracts
        self.assertEqual([c.kind for c in contracts], ["pre", "post"])
        self.assertIsNotNone(contracts[0].message)


class CodegenTests(unittest.TestCase):
    def generate(self, source: str) -> str:
        return transpile(source, "t.ep", Options())

    def test_escaped_contextual_keywords(self):
        cpp = self.generate(
            "main: () = { __identifier__inspect: i32 = 1; __identifier__is: i32 = 2; }"
        )
        self.assertIn("i32 inspect = 1;", cpp)
        self.assertIn("i32 is = 2;", cpp)

    def test_hello(self):
        cpp = self.generate(
            '#include <iostream>\nmain: () = { std::cout << "hi\\n"; }\n'
        )
        self.assertIn("int main()", cpp)
        self.assertIn('#include "epi_support.hpp"', cpp)

    def test_direct_list_initialisation(self):
        cpp = self.generate("main: () = { v: std::vector = (1, 2, 3); }")
        self.assertIn("std::vector v{1, 2, 3};", cpp)

    def test_safety_checks_are_default(self):
        cpp = self.generate("main: () = { v: std::vector = (1); _ = v[0]; _ = 1 / 2; }")
        self.assertIn("::epi::subscript(", cpp)
        self.assertIn("::epi::div(", cpp)

    def test_safety_checks_can_be_disabled(self):
        opts = Options(subscript_checks=False, div_checks=False)
        cpp = CodeGen(opts, "t.ep").generate(
            parse("main: () = { v: std::vector = (1); _ = v[0]; _ = 1 / 2; }")
        )
        self.assertNotIn("::epi::subscript(", cpp)
        self.assertNotIn("::epi::div(", cpp)

    def test_interpolation(self):
        cpp = self.generate('main: () = { x := 1; std::cout << "x=(x)$\\n"; }')
        self.assertIn("::epi::concat(", cpp)

    def test_format_spec_uses_std_format(self):
        cpp = self.generate('main: () = { x := 1; std::cout << "x=(x:04d)$\\n"; }')
        self.assertIn("std::format(", cpp)
        self.assertIn("{:04d}", cpp)

    def test_ufcs_lowering(self):
        cpp = self.generate("f: (x) = { } main: () = { f(1); 2.f(); }")
        self.assertIn("EPI_UFCS(f)(2)", cpp)

    def test_enum_lowering(self):
        cpp = self.generate("color: @enum type = { red; green; blue; }\nmain: () = { c: color = (); }")
        self.assertIn("enum class epi__value_t", cpp)
        self.assertIn("red = 1", cpp)
        self.assertIn("green = 2", cpp)

    def test_flag_enum_lowering(self):
        cpp = self.generate(
            "flags: @flag_enum<u8> type = { a; b; both := a | b; }\nmain: () = { f: flags = (); }"
        )
        self.assertIn("struct flags", cpp)
        self.assertIn("enum class epi__value_t : u8", cpp)
        self.assertIn("both = 3", cpp)
        self.assertIn("constexpr bool has(", cpp)
        self.assertIn("constexpr void set(", cpp)

    def test_ordered_metafunction(self):
        cpp = self.generate(
            "point: @ordered type = { public x: i32 = 0; public y: i32 = 0; }\nmain: () = { }"
        )
        self.assertIn("auto operator<=>(point const&) const = default;", cpp)
        self.assertIn("bool operator==(point const&) const = default;", cpp)

    def test_singleton_metafunction(self):
        cpp = self.generate("cfg: @singleton type = { public v: i32 = 1; }\nmain: () = { }")
        self.assertIn("static cfg& instance()", cpp)
        self.assertIn("cfg(cfg const&) = delete;", cpp)

    def test_rejected_metafunction(self):
        with self.assertRaises(CompileError):
            self.generate("flags: @regex type = { a = 1; }\nmain: () = { }")


class OwnershipTests(unittest.TestCase):
    def compile(self, source: str, options: Options | None = None) -> str:
        return transpile(source, "t.ep", options or Options())

    def test_borrow_return_lowers_to_reference(self):
        cpp = self.compile(
            "name: (in text: std::string) -> in std::string = { return text&; }\n"
            "main: () = { }"
        )
        self.assertIn("std::string const& name", cpp)

    def test_unique_lowers_to_unique_ptr(self):
        cpp = self.compile("main: () = { p: unique<i32> = new<i32>(1); }")
        self.assertIn("std::unique_ptr<i32> p", cpp)

    def test_use_after_move_is_rejected(self):
        with self.assertRaises(CompileError) as ctx:
            self.compile(
                "take: (move p: unique<i32>) -> i32 = { return p*; }\n"
                "main: () = { a: unique<i32> = new<i32>(1); _ = take(move a); _ = a*; }"
            )
        self.assertIn("moved", ctx.exception.message)

    def test_borrow_conflict_is_rejected(self):
        with self.assertRaises(CompileError) as ctx:
            self.compile("main: () = { x := 1; r: in i32 = x&; m: inout i32 = x&; }")
        self.assertIn("borrowed", ctx.exception.message)

    def test_dangling_borrow_return_is_rejected(self):
        with self.assertRaises(CompileError) as ctx:
            self.compile("f: () -> in i32 = { local := 1; return local&; }")
        self.assertIn("local variable", ctx.exception.message)

    def test_raw_pointer_needs_unsafe(self):
        with self.assertRaises(CompileError) as ctx:
            self.compile("main: () = { x := 1; p: *i32 = unsafe(x&); _ = p*; }")
        self.assertIn("unsafe", ctx.exception.message)

    def test_cpp1_block_is_passed_through(self):
        cpp = self.compile(
            "cpp1 { inline int raw_add(int a, int b) { return a + b; } }\n"
            'main: () = { std::cout << raw_add(1, 2); }'
        )
        self.assertIn("inline int raw_add", cpp)

    def test_default_prelude_includes_stdlib(self):
        cpp = self.compile("main: () = { }")
        self.assertIn("#include <vector>", cpp)
        self.assertIn("#include <map>", cpp)

    def test_no_prelude_option(self):
        cpp = transpile("main: () = { }", "t.ep", Options(preload_std=False))
        self.assertNotIn("#include <vector>", cpp)

    def test_prelude_does_not_duplicate_user_includes(self):
        cpp = self.compile("#include <vector>\nmain: () = { }")
        self.assertEqual(cpp.count("#include <vector>"), 1)

    def test_ownership_checks_can_be_disabled(self):
        cpp = self.compile(
            "main: () = { x := 1; p: *i32 = unsafe(x&); _ = p*; }",
            Options(ownership_checks=False),
        )
        self.assertIn("int main", cpp)


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "epitaxy", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_emit_cpp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hello.ep"
            path.write_text('main: () = { }\n')
            result = self.run_cli("--emit-cpp", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("int main()", result.stdout)

    def test_compile_error_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.ep"
            path.write_text("main: () = { x := ; }\n")
            result = self.run_cli("--emit-cpp", str(path))
            self.assertEqual(result.returncode, 1)
            self.assertIn("error:", result.stderr)

    def test_missing_file(self):
        result = self.run_cli("--emit-cpp", "definitely-not-here.ep")
        self.assertEqual(result.returncode, 2)

    def test_version(self):
        result = self.run_cli("--version")
        self.assertIn("Epitaxy", result.stdout)


if __name__ == "__main__":
    unittest.main()
