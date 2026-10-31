# Epitaxy compiler design

This document describes how the Epitaxy compiler is structured, the main design
decisions behind it, and where to extend it.

## Goals and non-goals

Goals:

* implement the Cpp2 syntax requirements faithfully enough that Cpp2
  documentation examples are readable and portable to Epitaxy;
* provide a small, complete, auditable implementation: lexer, parser, code
  generator, runtime support, CLI, tests and docs;
* be MIT licensed and independent — no cppfront code;
* generate ordinary C++20 that works with existing C++ libraries, so the
  language is useful immediately.

Non-goals for 0.2:

* C++ type checking or whole-program semantic analysis;
* reflection and user-defined metafunctions;
* separate compilation / modules;
* mixed Cpp1/Cpp2 source files — an intentional non-goal: Epitaxy is a new
  language, and C++ interop happens through headers and ordinary calls, not by
  pasting C++ declarations into `.ep` files;
* bootstrapping (implementation is in Python).

## Pipeline

```
 .ep source
     │
     ▼
 lexer.py        tokens with positions; string literals are pre-scanned for
                 interpolations and preprocessor lines are captured whole
     │
     ▼
 parser.py       context-free recursive-descent parser → AST
 ast.py
     │
     ▼
 ownership.py    ownership, borrow, move and unsafe-boundary checking
     │           (produces code-generation annotations)
     ▼
 codegen.py      C++20 lowering, with moveanalysis.py for automatic moves
     │
     ▼
 .cpp file  ──►  host C++ compiler (clang++ / g++)
     │
     ▼
 executable
```

The compiler is a pure Python package with no third-party dependencies, so it
runs anywhere Python 3.10+ runs. Generated programs need only a C++20 compiler
and the bundled `epi_support.hpp`.

`compiler.py` ties the pipeline together: `transpile(source, filename,
options)` parses, runs the ownership checker and generates C++. The CLI and the
test harness use it, so the default-on checks apply everywhere.

## Lexer

* Greedy longest-match operators. The only context-sensitive part is the
  postfix/binary distinction for `*` and `&`, which is resolved by the parser.
* String literals are decoded enough to find `(expr)$` interpolations: the
  scanner balances parentheses (skipping nested string and character literals)
  and stores each interpolated expression's source text plus its position, and
  an optional `:format-spec`. Raw strings are preserved verbatim.
* Numeric literals follow C++ syntax including digit separators and user-defined
  literal suffixes, which are passed through. A `.` is not consumed as a decimal
  point when followed by an identifier (so `123.u8()` is a UFCS call, not a
  user-defined literal) or by another `.` (so `1..<10` lexes as `1 ..< 10`).
* `#`-lines are captured as single `PREPROC` tokens with line continuations.

## Parser

The parser is a hand-written recursive-descent parser. The language is designed
to be context free, so the parser never performs name lookup. A few places need
bounded lookahead or backtracking:

| Ambiguity | Resolution |
| --- | --- |
| `f<T>(x)` call vs. `a < b > c` comparisons | try template arguments + `(`; otherwise a template-id is accepted only when the following token cannot continue an expression; otherwise backtrack |
| `*` / `&` postfix (deref/address) vs. binary | postfix if the next token cannot begin an operand |
| `x: (params) -> T = …` function vs. object of function type | a leading `(` after `:` is always a function; a function-type object starts with `*` |
| `is C`: type constraint vs. value query | literals and parenthesised expressions are values; everything else is a type |
| `x: = 0` vs. `x := 0` | both spellings accepted |
| `(x: T = v) { … }` scoped block vs. parenthesised expression | backtracking: a parameter list followed by a block/statement |
| `a op ... op pack` folds | the parenthesis body is scanned for a top-level `...` before parsing |
| `@enum` bodies with bare enumerators | inside type bodies, `ident ;` and `ident := expr ;` are enumerators |
| `operator=` and friends | parsed as `"operator" op ":" …` |
| statement vs. expression | a leading identifier followed by `:`/`:=` starts a declaration; labels are recognised by `name: while/for/do` |

The parser threads a *stop set* through the expression grammar (a stack of
frozensets) so constructs such as template argument lists, contract condition
lists, `inspect` alternatives and `for` headers have unambiguous boundaries.
Because the same stop set applies at every precedence level, no special cases
are needed in the precedence-climbing code.

Error state: the parser raises `CompileError` at the first problem with a source
position. Recovery is out of scope for 0.2.

## AST

`ast.py` uses small `@dataclass(kw_only=True)` nodes with `line`/`col` fields.
Every syntactic construct has its own node. The AST is directly consumed by the
code generator; there is no separate semantic-analysis IR, because C++'s own
compiler does all type checking.

## Code generator

`codegen.py` emits C++20 into an indentation-managed line buffer. It runs these
passes, each of which recurses into namespaces:

1. **Forward types** — `struct S;` (with template header) for every type.
2. **Forward aliases** — type aliases, namespace aliases, `using` declarations.
3. **Forward constants** — `inline constexpr` object aliases.
4. **Forward named-return structs** — so prototypes can mention them.
5. **Forward prototypes** — declarations for functions whose return type is not
   deduced (C++ cannot declare an `auto`/`decltype(auto)` function without its
   body).
6. **Type definitions** — classes, members, nested types, special members,
   metafunction expansion.
7. **Named-return struct definitions** — after the user types they mention.
8. **Global objects** — namespace-scope variable definitions (before functions,
   so function bodies can use them).
9. **Function definitions** — deduced-return functions first, then the rest, so
   the common "helper defined below the caller" pattern works.

Notes on specific lowerings:

* **Names and types** — `const`/`*` chains are lowered left-to-right (e.g.
  `const *T` becomes `T* const`). Function types become C++ function types.
* **Initialisation** — `x: T = (a, b);` is direct-list-initialisation
  (`T x{a, b};`), so explicit constructors and CTAD both work; `x: T = expr;` is
  copy-initialisation.
* **Special members** — `operator=` declarations are analysed by their `this`
  kind (`out`/`inout`/`move`) and source parameter (`that`, typed value, none).
  Generated constructors receive a member-initializer list built from the
  leading `member = value;` statements of the body plus memberwise defaults; the
  remaining statements go in the body. Copy/move assignment generation is
  suppressed if the user wrote an `inout this` operator=.
* **Contracts** — `pre` checks are emitted at function entry. `post` `$`
  captures become entry locals; postconditions are emitted before every normal
  return and at fall-through. The check shape is
  `if (preds && group.is_active() && !(cond)) group.report_violation(msg)`.
* **Safety** — checked operations call helpers in the support header
  (`epi::subscript`, `epi::deref`, `epi::div`, `epi::mod`) or comparison helpers
  (`epi::cmp_lt`, …); the CLI flags select unprotected lowerings.
* **`inspect`** — lowered to an immediately-invoked lambda that evaluates the
  subject once and tests alternatives in order.
* **String interpolation** — concatenation with `epi::to_string`, or
  `std::format` when a format specification is present.
* **UFCS** — member calls are emitted through the `EPI_UFCS(name)` macro, which
  uses a `requires`-expression to choose between a member call and a free call.
  Qualified names and member-only `..` calls are emitted directly.
* **Loops with `break`/`continue`** — if a function body contains loop control,
  loops are lowered to labelled `for`/`while`/`do` forms with `goto` targets so
  that `next` statements run correctly on `continue` and labelled
  `break`/`continue` work.
* **`main`** — special-cased to `int main()`, optionally with
  `int argc, char** argv` and a generated `epi::args_t args{argc, argv};`.
* **Auto-move** — `moveanalysis.py` collects uses with their loop depth,
  enclosing lambda state, address-taken state and parent expression context, and
  marks the last safe use for `std::move`/`std::forward` (conservatively).

## Ownership and borrow checking

`ownership.py` is a separate semantic pass over the AST. It builds lightweight
type tables (type aliases, user type data members, function/method signatures)
and then walks every Epitaxy function with an environment that tracks:

* locals and parameters with their type category (`value`, `unique<T>`,
  `shared<T>`, borrow, raw pointer), mutability, move state and `out`
  initialization state;
* active borrows (root variable, path, shared/exclusive kind, holder, scope);
* unsafe context depth and loop depth.

Places are modelled as `(root variable, path)` pairs, where members, subscripts
and dereferences extend the path. Borrow conflicts are decided by path overlap.
Checks are performed exactly where they are sound rather than as a separate data
flow analysis: reads/writes/moves validate against the active borrows, calls
build borrow "plans" from the callee's parameter kinds and check aliasing among
the arguments, and returns validate borrow provenance. Branches merge move/out
state (intersection), loops conservatively reject moving outer-declared
variables.

The pass also produces annotations used by the code generator:

* `deduced_borrow` — a deduced local whose initializer returns a borrow must be
  emitted as a reference (`auto const&` / `auto&`), not a copy;
* `borrow_names` — borrow locals are never automatically moved.

The checker is deliberately conservative: lexical borrow lifetimes, no NLL, no
lifetime polymorphism, no C++ library modelling, no `Send`/`Sync`. External
calls are trusted except for explicit `move` arguments. See
[`COMPATIBILITY.md`](COMPATIBILITY.md) §3.14 for the rule list.

## Runtime support header

`epitaxy/support/epi_support.hpp` is a self-contained C++20 header (MIT,
original) that provides:

* prelude type aliases (`i32`, `u64`, …) and `epi::in`;
* contract groups, handlers and `contract_check`;
* `EPI_UFCS` and string interpolation helpers;
* safety helpers and `unchecked_*` opt-outs;
* `is`, `is_value`, `is_polymorphic` and `as`;
* `iota_range` and the range builders;
* `epi::args_t`.

The CLI adds the header's directory to the compiler include path when it invokes
the C++ compiler; when generating standalone C++ it is the user's job to pass
`-I` (the generated file includes `"epi_support.hpp"`).

## Testing

* `tests/run_tests.py` — integration tests. Each program is transpiled,
  compiled with a real C++ compiler, executed, and its stdout/exit code compared
  with the expected files. Error tests assert diagnostics from either the
  Epitaxy compiler or the C++ compiler. Examples are compiled and executed to
  ensure the documentation stays true.
* `tests/unit/` — unittest suite for the lexer, parser, code generator and CLI.
* The suite is run with both clang/libc++ and GCC/libstdc++ locally; CI runs
  `clang++` and `g++` on Linux and macOS.

## Known limitations and future work

The language limitations are listed in
[`COMPATIBILITY.md`](COMPATIBILITY.md). Implementation-level work that would
unlock the most value:

1. non-lexical lifetimes (NLL) and lifetime variables for the ownership checker;
2. `Send`/`Sync` (or equivalent) analysis to prevent data races;
3. modelling of C++ library aliasing (e.g. `vector` reallocation invalidating
   borrowed elements) and library annotations;
4. a deducible `in`-like parameter type to remove the by-value/by-reference
   heuristic;
5. proper `out` parameter proxies for deferred construction;
6. semantic analysis for better diagnostics (e.g. mixed-sign checks for
   equality, name resolution errors at the Epitaxy stage);
7. more metafunctions, starting with `@flag_enum` and `@union`;
8. module/import support and multi-file projects;
9. an optional C++-implemented self-hosted compiler.

## Extending the compiler

Adding a statement or expression usually means:

1. add a node in `ast.py`;
2. parse it in `parser.py` (choose the right level; use stop sets for
   boundaries);
3. lower it in `codegen.py` (`gen_stmt` or `_expr_*`);
4. add an integration test in `tests/cases/` and an error test if applicable;
5. document it in `docs/SPEC.md` and `docs/COMPATIBILITY.md`.

Adding a checked rule starts in `ownership.py`; add a positive integration test,
an error test under `tests/errors/`, and document the rule in `docs/SPEC.md`
§11 and `docs/COMPATIBILITY.md` §3.14.

Adding runtime behaviour starts in `epi_support.hpp`; the generated code can
call anything in the `epi` namespace.
