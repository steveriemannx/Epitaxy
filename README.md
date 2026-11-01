# Epitaxy

**A small, from-scratch, MIT-licensed programming language with Cpp2 syntax that
compiles to C++20.**

Epitaxy is an independent implementation of the *syntax requirements* of
[Cpp2 / cppfront](https://hsutter.github.io/cppfront/) — Herb Sutter's
experimental "C++ syntax 2". It is written from scratch: **no cppfront code is
used, copied, or derived.** The language is defined by the specification in
[`docs/SPEC.md`](docs/SPEC.md), the compiler is implemented in Python, and every
program is lowered to C++20.

```cpp2
// hello.ep
#include <iostream>

main: () = {
    hello("world");
}

hello: (name: std::string_view) = {
    std::cout << "Hello, (name)$!\n";
}
```

```console
$ python3 -m epitaxy --run hello.ep
Hello, world!
```

- **MIT licensed** — see [`LICENSE`](LICENSE). No Apache-2.0-with-LLVM-exception
  code is included.
- **One grammar** — every declaration is `name : kind = value`, and the grammar
  is context free.
- **Safe by default** — subscript bounds, null dereferences, integer division by
  zero and mixed-sign comparisons are checked unless you opt out.
- **Ownership and borrowing** — `unique<T>` and `shared<T>` owning handles,
  `in T` / `inout T` borrows, move checking, and an explicit `unsafe` boundary
  for raw pointers.
- **C++ ecosystem, no bindings** — use C++ types, functions and objects
  directly; a curated set of C++ standard headers is pre-included by default
  (`-no-prelude` opts out), and `cpp1 { ... }` blocks pass raw C++ through when
  you need it. The output is ordinary C++20 with the same ABI, so there is no
  FFI/binding layer. (See the [interop model](#interop-model): Epitaxy is a new
  language, not a C++ syntax mode.)

## Status

Epitaxy 0.2 is a complete vertical slice: a working lexer, parser, ownership and
borrow checker, C++20 code generator, a runtime support header, a CLI, an
integration test suite and examples. It implements a substantial, coherent
subset of Cpp2 as documented for cppfront, and adds an ownership/borrow checking
extension on top; the precise coverage and the deliberate deviations are listed
in [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md).

The ownership checker is on by default and rejects use-after-move, conflicting
borrows, dangling borrow returns, move-only copies and unsafe operations outside
`unsafe` blocks. It is a conservative, Epitaxy-level analysis: it does not model
C++ library internals, threads/data races or full lifetime polymorphism. See
[`docs/SPEC.md`](docs/SPEC.md) and [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md)
for the exact rule set.

Everything in this repository is original work. cppfront was used only as a
*description of the desired syntax* (its documentation), never as source code.

## Requirements

- Python 3.10 or newer (the compiler itself)
- A C++20 compiler to build generated programs: clang++ or g++ (any recent
  version with `<format>` and `<ranges>` support)

No third-party Python packages are required.

## Quick start

From a checkout of this repository:

```console
# transpile hello.ep to hello.cpp
python3 -m epitaxy hello.ep

# transpile, compile with a C++20 compiler, and run
python3 -m epitaxy --run hello.ep

# just print the generated C++20
python3 -m epitaxy --emit-cpp hello.ep
```

Epitaxy source files use the `.ep` extension by convention; the compiler does
not require any particular extension.

To install the `epitaxy` command onto your `PATH`:

```console
python3 -m pip install .
epitaxy --version
```

## A short tour

### Unified declaration syntax

```cpp2
main: () = { }                          // function
count: i32 = 0;                         // object
answer: i32 == 42;                      // compile-time alias
BufferSize: i32 == 1'000'000;
imap: <T> type == std::map<i32, T>;     // type alias
chr: namespace == std::chrono;          // namespace alias
square: (i: i32) -> _ == i * i;         // constexpr function alias
```

### Functions, parameters, contracts

```cpp2
divide: (dividend: i32, divisor: i32) -> (quotient: i32, remainder: i32)
    pre(divisor != 0, "divisor must not be zero")
    post(divisor == divisor$)           // `$` captures the value on entry
= {
    quotient  = dividend / divisor;
    remainder = dividend % divisor;
}
```

Parameter passing is explicit: `in` (the default), `inout`, `out`, `copy`,
`move`, `forward`; `this` and `that` declare member functions and source
parameters.

### Types and special members

```cpp2
point: type = {
    x: i32 = 0;              // data members are private by default
    y: i32 = 0;

    operator=: (out this, xv: i32, yv: i32) = {
        x = xv;
        y = yv;
    }

    operator=: (out this, that) = { }     // copy/move ops, memberwise

    sum: (this) -> i32 = x + y;           // member functions are public
}

shape: @interface type = {
    area: (this) -> double;
}

circle: type = {
    this: shape;
    radius: double = 1.0;
    operator=: (out this, r: double) = { radius = r; }
    area: (override this) -> double = 3.14159 * radius * radius;
}

color: @enum type = { red; green; blue; }
flags: @flag_enum<u8> type = { cached; current; obsolete; }
rank:  @ordered type = { public value: i32 = 0; }
plain: @value type = { public a: i32 = 0; public b: i32 = 0; }
config: @singleton type = { public value: i32 = 42; }   // config::instance()
```

### String interpolation, inspect, UFCS, ranges

```cpp2
std::cout << "Hello, (name)$! pi=(3.14159:.2f)$\n";

label := inspect value -> std::string {
    is 0          = "zero";
    is i32        = "an integer";
    is std::string = "a string";
    is _          = "something else";
};

words.std::ranges::sort();     // UFCS: qualified free-function call
std::cout << words[0];         // bounds checked by default

for 1 ..= 100 do (i) { }       // closed range
for iter ..< end do (it) { }   // half-open range over any incrementable type
```

### Ownership, borrowing, and `unsafe`

```cpp2
// unique<T> is move-only; shared<T> is a shared owning handle.
make: (n: i32) -> unique<i32> = {
    p: unique<i32> = new<i32>(n);
    return p;                      // ownership transfer
}

consume: (move p: unique<i32>) -> i32 = p*;

read: (in value: i32) -> i32 = value;          // shared borrow
add_to: (inout value: i32, amount: i32) = { value += amount; }  // exclusive borrow

first: (in text: std::string) -> in std::string = text&;  // borrow tied to `text`

main: () = {
    a := make(7);
    b := consume(move a);          // `a` is moved; further use is an error
    x := 10;
    add_to(x, 5);                  // exclusive borrow for the call
    r: in i32 = x&;                // local shared borrow
    std::cout << "r=(r)$\n";       // reads through the borrow

    // Raw pointers require an explicit unsafe boundary:
    raw: *i32 = unsafe(x&);
    value := unsafe(raw*);
}
```

The checker rejects, among others: using a moved value; moving or mutating a
borrowed value; two conflicting borrows of the same place; returning a borrow of
a local; copying `unique<T>`; dereferencing a raw pointer outside `unsafe`.

### Safety you can see

```cpp2
v[idx]           // runtime bounds check (subscript)
p*               // runtime null check (dereference)
a / b            // runtime division-by-zero check
a < b            // compile-time error if the integers have mixed signedness

unchecked_subscript(v, idx)   // explicit opt-outs are always spelled out
unchecked_div(a, b)
unchecked_dereference(p)
unchecked_cmp_less(a, b)
```

The checks can also be disabled per translation unit with
`-no-subscript-checks`, `-no-null-checks`, `-no-div-zero-checks` and
`-no-comparison-checks`.

## Command line

```
usage: epitaxy [-h] [-o FILE.cpp] [--emit-cpp] [--run] [-c] [--cc CXX]
               [--keep] [--print-cpp] [-q] [-no-subscript-checks]
               [-no-null-checks] [-no-div-zero-checks] [-no-comparison-checks]
               [--version] FILE.ep [FILE.ep ...]
```

| Option | Meaning |
| --- | --- |
| `-o FILE.cpp` | write the generated C++ to `FILE.cpp` |
| `--emit-cpp` | print the generated C++ to standard output |
| `--run` | transpile, compile and run; program arguments follow `--` |
| `-c` | transpile and compile, but do not run |
| `--cc CXX` | compiler for `--run`/`-c` (default: `$CXX`, then `clang++`, `g++`) |
| `--keep` | keep the intermediate C++ file and executable |
| `--print-cpp` | also print the generated C++ to standard error |
| `-q` | suppress informational messages |
| `-no-*-checks` | disable a default-on safety check (`name`, `null`, `div-zero`, `comparison`, `ownership`) |
| `-no-prelude` | do not pre-include the curated C++ standard-library headers |
| `-strict-safety` | strict safety mode: all checks mandatory, checked arithmetic, `cpp1` only inside `unsafe` |
| `--sanitize` | compile with `-fsanitize=address,undefined` under `--run`/`-c` (detection layer for C++-side UB) |
| `--safe-only` | restrict the program to Epitaxy + the compiler runtime: no preprocessor, `cpp1`, `unsafe`, raw pointers, external C++ names, thread API or by-reference captures; implies `-strict-safety` |
| `--cpp-modules` | prototype C++20 modules build: inputs with `module name;` become C++ modules, the single input without it is the entry point (GCC or upstream Clang; Apple Clang lacks C++20 modules) |
| `--version` | print the compiler version |

Environment variables: `CXX` selects the C++ compiler and `CXXFLAGS` adds extra
flags when Epitaxy invokes it.

## Repository layout

```
epitaxy/                 the compiler
  lexer.py               tokens, literals, string interpolation
  parser.py              context-free recursive-descent parser
  ast.py                 abstract syntax tree
  ownership.py           ownership, borrow and unsafe-boundary checking
  compiler.py            pipeline: parse -> ownership check -> codegen
  codegen.py             C++20 lowering
  moveanalysis.py        conservative definite-last-use analysis
  cli.py                 command line interface
  support/epi_support.hpp  runtime support header (contracts, UFCS, safety, ...)
docs/
  SPEC.md                the Epitaxy language specification
  COMPATIBILITY.md       Cpp2 syntax coverage and deliberate deviations
  DESIGN.md              compiler architecture and implementation notes
tests/
  run_tests.py           integration suite: programs, expected output, errors
  unit/                  lexer/parser/codegen/CLI unit tests
  cases/                 .ep programs with expected output
  errors/                programs that must be rejected
examples/                runnable example programs
```

## Testing

```console
# integration suite: generates C++, compiles it, runs it, compares output;
# also runs ownership/borrow error tests and compiles every example
python3 tests/run_tests.py

# unit tests
python3 -m unittest discover -s tests/unit

# both
make test
```

The suite is compiler-agnostic; to test with a specific C++ compiler:

```console
CXX=g++-16 python3 tests/run_tests.py
CXX=clang++ python3 tests/run_tests.py
```

## Language documentation

- [`docs/SPEC.md`](docs/SPEC.md) — complete language reference: non-lexical
  structure, declarations, types, expressions, statements, contracts, safety,
  metafunctions, C++ lowering.
- [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) — what of Cpp2 is supported,
  what is intentionally different, and what is not implemented in 0.2.
- [`docs/DESIGN.md`](docs/DESIGN.md) — how the compiler is built and why.

## Safety guarantees

Epitaxy has two safety levels, documented in detail in
[`docs/SAFETY.md`](docs/SAFETY.md):

* **Default** — ownership/borrow checking, bounds/null/div-zero/sign checks and
  an `unsafe` boundary for raw pointers. Individual checks can be disabled;
  this is best-effort, not a guarantee.
* **Thread API** — `epi::thread` + `epi::spawn` is the checked threading
  surface: values moved into a thread must be `Send`, and `shared<T>` requires
  `T: Send + Sync`.
* **Strict** (`-strict-safety`) — all checks forced on, integer arithmetic
  checked, `cpp1` blocks only inside `unsafe`. This is the level with a
  **conditional guarantee**: Epitaxy code without `unsafe`/`cpp1`/raw pointers
  cannot trigger undefined behaviour *through Epitaxy's own operations*; C++
  calls remain a trusted boundary.

Epitaxy is **not Rust-certified**: borrow lifetimes are non-lexical but
conservative (no lifetime generics), safe C++ interop is trusted, and
`Send`/`Sync` checking covers only the explicit `epi::spawn` thread API — not
arbitrary C++ threading. An
unconditional "safe code has no UB" guarantee would require an owned
backend/runtime — see [`docs/SAFETY.md`](docs/SAFETY.md) §8.

### Safe-only mode

`--safe-only` removes the user-facing C++ escape hatches: no preprocessor
directives, no `cpp1` blocks, no `unsafe`, no raw pointers, no external C++
types/names, no `unchecked_*`, no thread API and no by-reference captures. Only
Epitaxy declarations, builtin scalars, user types, `unique`/`shared`, borrows
and the trusted compiler runtime (`std::string`, interpolation,
`epi::print`/`epi::println`, contracts, bounds/null/overflow helpers) are
allowed. The mode implies `-strict-safety`.

Safe-only makes the **conditional** guarantee precise: no user-written C++
interop is a trust boundary any more. It is still not an unconditional "safe
code has no UB" promise: the compiler runtime itself (standard library calls
inside `std::string`/format/I/O), recursion/stack exhaustion and resource
exhaustion remain outside the guarantee — see
[`docs/SAFETY.md`](docs/SAFETY.md).

### C++20 modules (prototype)

A minimal C++20 modules build mode is available. Mark library files with a
module declaration and import them from the entry file:

```cpp2
// math.ep
module math;

geo: namespace = {
    add: (a: i32, b: i32) -> i32 = a + b;
}
```

```cpp2
// main.ep
import math;

main: () = {
    epi::println("add=", geo::add(2, 3));
}
```

```console
$ python3 -m epitaxy --cpp-modules --run --cc g++-16 main.ep math.ep
add=5
```

Epitaxy builds the module dependency graph, generates one C++ module interface
unit per `module name;` file (`export module epi_<name>;` with everything
exported), generates the entry file as an ordinary translation unit with
`import epi_<name>;` lines, compiles the modules in dependency order and links
the executable.

The build driver probes the compiler for C++20 module support and handles
GCC (`-fmodules-ts` with its `gcm.cache`) and upstream Clang
(`-x c++-module --precompile`, then `-fmodule-file=`). Apple Clang does not
implement C++20 modules; with it you get a clear error and can pass e.g.
`--cc /opt/homebrew/opt/llvm/bin/clang++` or `--cc g++-16`.

Prototype limitations: module names are single identifiers; there are no
module partitions, export lists, implementation units or `import std;`; every
top-level name of a module is exported; the entry file is the only file allowed
to define `main`; imports are not transitive (import every module whose names
you use); and the generated code includes the compiler runtime headers in the
global module fragment, so the same include-before-import ordering is used.
Single-file compilation is unaffected.

## Projects (Cargo-like)

A project is a directory with an `Epitaxy.toml` manifest and a `src/` tree
(`src/main.ep` is the entry point, other `.ep` files are modules). `tests/*.ep`
files are test programs; each one is built and must exit 0.

```console
$ python3 -m epitaxy new myapp
$ cd myapp
$ python3 -m epitaxy run
Hello from myapp!
$ python3 -m epitaxy build          # -> target/myapp
$ python3 -m epitaxy test           # builds and runs tests/*.ep
$ python3 -m epitaxy check          # parse/lower every source file
```

Minimal manifest:

```toml
[package]
name = "myapp"
version = "0.1.0"

[dependencies]
util = "../util"                 # path dependency (shorthand)
util = { path = "../util" }      # or an inline table
```

Project builds use the C++20 modules pipeline, so they need a module-capable
compiler (GCC or upstream Clang); `--cc` selects one explicitly. A dependency's
`src/` modules are compiled together with the root project's modules and share
one flat module namespace for now (import the declared module names). Build
outputs go to `target/`. This is the first Cargo-like step: manifests, the
`src/`/`tests/` layout, path dependencies, and build/run/test/check commands —
workspace members, a package registry, lockfiles and `--lib` targets are future
work.

## Interop model

Epitaxy is a **new language**, not a "C++ syntax mode", so **implicit
Cpp1/Cpp2 mixing is an explicit non-goal**. Interop works at the
header/library level, plus two pragmatic escape hatches:

* By default the compiler pre-includes a curated set of C++ standard headers
  (`<vector>`, `<map>`, `<algorithm>`, `<numeric>`, `<chrono>`, `<format>`,
  `<iostream>`, …), so the standard library is available without writing
  `#include`. Use `-no-prelude` to turn this off and include exactly what you
  need.
* `cpp1 { ... }` blocks pass raw C++ through verbatim (at namespace scope, type
  scope, or statement scope). This is for constructs that cannot be expressed in
  Epitaxy (C++ macros, pointer-to-members, complex templates, …). Raw blocks are
  opaque to the ownership checker; keep them small and at the boundary.
* `#include` any C++ header; call its functions and use its types directly. This
  includes the standard library, third-party libraries and in-house C++ code.
* Epitaxy lowers to ordinary C++20, so exceptions, allocators, ABI, calling
  conventions and link-time behaviour are exactly C++'s: there is no marshalling
  or FFI layer, and no runtime interpreter.
* C++ raw-pointer APIs are usable through the explicit `unsafe` boundary
  (`unsafe { ... }`, `unsafe(expr)`, `unsafe` functions).
* C++ code calling Epitaxy from **another** translation unit currently needs a
  hand-written C++ header (or the generated `.cpp` compiled in the same unit).
  Generating an interface header (`--emit-header`) is future work, not part of
  the mixed-mode question.

[`examples/cpp_ecosystem.ep`](examples/cpp_ecosystem.ep) demonstrates direct use
of `<algorithm>`, `<map>`, `<numeric>`, `<optional>`, `<variant>`, `<chrono>`,
`<filesystem>` and smart pointers from Epitaxy.

## Known limitations (0.2)

* The borrow checker uses non-lexical lifetimes for non-escaping borrows but
  falls back to lexical scopes when a borrow may escape or is used across loop
  iterations; it has no lifetime generics beyond explicit `-> in(param) T`
  annotations and does not model C++ libraries.
* Declarations without an initializer are value-initialized (`T x{}`), so they
  cannot cause undefined behaviour; unlike Rust, Epitaxy does not reject reads
  of such objects at compile time.
* Data-race checking is limited to the explicit thread API: `epi::spawn`
  requires `Send` arguments/captures and `Send + Sync` for `shared<T>`. Raw
  `std::thread`/`std::async` and C++ library threading are outside the analysis.
* Safety is a **checked subset with an explicit trust boundary**, not a
  Rust-style blanket guarantee; see [`docs/SAFETY.md`](docs/SAFETY.md).
* Calls into C++ are trusted as safe unless they are declared `unsafe` in
  Epitaxy; raw pointer operations are gated by `unsafe`.
* `cpp1 { ... }` blocks are raw C++ and are not analysed by the ownership
  checker (they are an explicit escape hatch). The default C++ prelude is
  convenience, not a semantic layer: C++ headers are still resolved by the host
  C++ compiler.
* Versus Cpp2, the largest missing pieces are: reflection and user-defined
  metafunctions (`@union`, `@print`, `@regex`, `@autodiff` and custom
  metafunctions are absent); the full
  `is`/`as` query system (concepts, template names, value predicates,
  `std::expected`, customization); `out` parameter construction semantics; and
  cppfront's tooling/`cpp2::` namespace. Mixed Cpp1/Cpp2 source and modules are
  intentionally out of scope (see the interop model above).
* The complete gap inventory is in [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md)
  section 4.

## License and independence

Epitaxy is released under the **MIT license** (see [`LICENSE`](LICENSE)).

Epitaxy's *syntax* follows the Cpp2 syntax requirements documented for
cppfront. Cppfront itself is distributed under the Apache License 2.0 with the
LLVM exception; **no cppfront code, test code, or generated code is included in
this repository**, and Epitaxy is not a fork or derivative work of cppfront.
All compiler code, the support header, tests and documentation here were written
for Epitaxy.

"Epitaxy" is the name of this project. The name and this project are not
affiliated with or endorsed by Herb Sutter or the cppfront project.
