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
// hello.epi
#include <iostream>

main: () = {
    hello("world");
}

hello: (name: std::string_view) = {
    std::cout << "Hello, (name)$!\n";
}
```

```console
$ python3 -m epitaxy --run hello.epi
Hello, world!
```

- **MIT licensed** — see [`LICENSE`](LICENSE). No Apache-2.0-with-LLVM-exception
  code is included.
- **One grammar** — every declaration is `name : kind = value`, and the grammar
  is context free.
- **Safe by default** — subscript bounds, null dereferences, integer division by
  zero and mixed-sign comparisons are checked unless you opt out.
- **C++ interop** — include C++ headers with `#include` and use C++ types,
  functions and objects directly. The output is ordinary C++20.

## Status

Epitaxy 0.1 is a complete vertical slice: a working lexer, parser, semantic
lowering and C++20 code generator, a runtime support header, a CLI, an
integration test suite and examples. It implements a substantial, coherent
subset of Cpp2 as documented for cppfront; the precise coverage and the
deliberate deviations are listed in
[`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md).

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
# transpile hello.epi to hello.cpp
python3 -m epitaxy hello.epi

# transpile, compile with a C++20 compiler, and run
python3 -m epitaxy --run hello.epi

# just print the generated C++20
python3 -m epitaxy --emit-cpp hello.epi
```

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
plain: @value type = { public a: i32 = 0; public b: i32 = 0; }
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
               [--version] FILE.epi [FILE.epi ...]
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
| `-no-*-checks` | disable a default-on safety check |
| `--version` | print the compiler version |

Environment variables: `CXX` selects the C++ compiler and `CXXFLAGS` adds extra
flags when Epitaxy invokes it.

## Repository layout

```
epitaxy/                 the compiler
  lexer.py               tokens, literals, string interpolation
  parser.py              context-free recursive-descent parser
  ast.py                 abstract syntax tree
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
  cases/                 .epi programs with expected output
  errors/                programs that must be rejected
examples/                runnable example programs
```

## Testing

```console
# integration suite: generates C++, compiles it, runs it, compares output
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

- [`docs/SPEC.md`](docs/SPEC.md) — complete language reference: lexical
  structure, declarations, types, expressions, statements, contracts, safety,
  metafunctions, C++ lowering.
- [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) — what of Cpp2 is supported,
  what is intentionally different, and what is not implemented in 0.1.
- [`docs/DESIGN.md`](docs/DESIGN.md) — how the compiler is built and why.

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
