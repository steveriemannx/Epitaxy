# Cpp2 compatibility and deliberate deviations

Epitaxy follows the *syntax requirements* of Cpp2 (as documented for cppfront),
but it is an independent implementation with its own specification. On top of
the Cpp2 syntax it adds an **ownership and borrow checking extension** (§3.14).
This document records what Epitaxy 0.2 supports, where its semantics differ, and
what is intentionally left out.

**Licensing note.** Epitaxy is MIT licensed. Cppfront is distributed under the
Apache License 2.0 with the LLVM exception. No cppfront source code, test code
or generated code is used in this project; cppfront's documentation was used as
the description of the desired syntax. See [`../LICENSE`](../LICENSE) and the
project [`README`](../README.md).

---

## 1. Supported Cpp2 syntax

Everything in this table is parsed with Cpp2-style spelling and lowered to
C++20.

### Declarations

| Cpp2 construct | Status | Notes |
| --- | --- | --- |
| `name: kind = value` unified declarations | supported | |
| `name: kind == value` aliases ("synonym for") | supported | |
| Function declarations, named and unnamed | supported | |
| Object declarations, `x: T = v`, `x := v`, `x: _ = v`, `x: T;` | supported | |
| Type declarations, type aliases, variable templates | supported | |
| Namespace declarations and namespace aliases | supported | |
| Function aliases `f: (…) -> _ == expr;` | supported | emitted as `constexpr` |
| Template parameters `<T: type, N: i32, Ts...: type>` with defaults | supported | |
| `requires` constraints | supported | |
| `using` declarations and `using ns::_;` namespace imports | supported | |
| `#include`/preprocessor lines | supported (passed through) | |
| `__identifier__keyword` escaped identifiers | supported | |
| `@metafunction` type declarations | partial — see §4 | |

### Functions

| Cpp2 construct | Status | Notes |
| --- | --- | --- |
| `in` (default), `inout`, `out`, `copy`, `move`, `forward` parameters | supported | `in_ref`/`forward_ref` also accepted |
| Deduced parameter types (`(x)`, `(x: _)`, constrained `(x: _ is C)`) | supported | |
| Default arguments | supported | emitted on the declaration |
| Variadic parameter packs | supported | |
| `this` / `that` parameters | supported | |
| `virtual this`, `override this`, `final this`, pure virtual functions | supported | |
| `implicit out this` converting constructors | supported | constructors are `explicit` by default |
| Return types: `void`, `T`, `_`, `forward _` | supported | |
| Named return values `-> (a: T, b: U)` | supported | see §3.7 |
| Single-expression bodies | supported | implicit `decltype(auto)` return unless `void` |
| `operator=:` construction/assignment/destruction | supported | see §3.5 |
| Other `operator<op>:` member functions | partial | member functions only; no free operators |
| Function expressions (lambdas) and `$` captures | supported | |

### Types and objects

| Cpp2 construct | Status | Notes |
| --- | --- | --- |
| `const`, `*` type qualifiers, left-to-right | supported | |
| Function types `(params) -> ret` and function pointers `* (…) -> ret` | supported | |
| `_` deduced types and `is` type constraints | supported | |
| `type_of(x)`, `decltype(x)` | supported | |
| Type bodies: data members, member functions, nested types/aliases | supported | |
| Access specifiers (`public`/`private`/`protected`) | supported | |
| Base types via `this: Base;` | supported | public inheritance |
| `@value`, `@copyable`, `@interface`, `@enum` | supported | see §3.6 |
| Memberwise-by-default `operator=` semantics | supported | see §3.5 |

### Expressions and statements

| Cpp2 construct | Status | Notes |
| --- | --- | --- |
| Full C++ binary operator set including `<=>`, `&&`, `\|\|` | supported | |
| Postfix `*` (deref), `&` (address-of), `~`, `++`, `--` | supported | |
| Comparison chaining `a < b < c` | partial | up to three operands |
| UFCS `x.f(args)`, member-only `x..f(args)` | supported | |
| Qualified UFCS `x.std::ranges::sort()` | supported | |
| Subscripts, calls, template-id calls | supported | |
| `is` queries and `as` casts | partial | see §3.2 |
| `inspect` expressions | supported | catch-all required |
| `..<` / `..=` ranges | supported | iterate by `++`; numeric extras |
| String interpolation `(expr)$` and format specs | supported | via `std::format` |
| `$` captures in strings, postconditions, function expressions | supported | |
| `new<T>`, `unique.new<T>`, `shared.new<T>` | supported | `make_unique`/`make_shared` |
| Fold expressions, pack expansions, `sizeof...(X)` | supported | |
| `if`/`else`, braced bodies | supported | |
| `while … next …`, `do … next … while …` | supported | |
| Range `for … do (param)`, `next`, labeled loops | supported | |
| `break`/`continue` with labels | supported | lowered via `goto` labels |
| `try`/`catch`/`throw` | supported | |
| `return`, `_ = expr` discard | supported | |
| `assert(cond, "msg")`, `pre(...)`, `post(...)`, groups and predicates | supported | |
| Local scoped parameter blocks `(x: T = v) { … }` | supported | |

### Safety and semantics

| Cpp2 feature | Status | Notes |
| --- | --- | --- |
| Bounds-checked subscripts by default | supported | containers with `std::size` and random-access iterators |
| Null-checked raw pointer dereference by default | supported | raw pointers only (not smart pointers) |
| Integer division-by-zero checks by default | supported | integral operands |
| Mixed-sign comparison errors by default | partial | relational operators only, at C++ compile time |
| `unchecked_*` opt-outs | supported | `narrow`, `cast`, `subscript`, `dereference`, `div`, `mod`, `cmp_less`, `cmp_equal` |
| Automatic move from last use | conservative approximation | see §3.4 |
| `[[nodiscard]]` on user functions | supported | generated for non-void returns |
| `unique<T>` / `shared<T>` owning handles | **Epitaxy extension** | lowered to `std::unique_ptr` / `std::shared_ptr` |
| `in T` / `inout T` borrow types (locals and returns) | **Epitaxy extension** | lowered to references; checked by the ownership pass |
| Ownership/borrow/move checking | **Epitaxy extension** | on by default; `-no-ownership-checks` opts out; see §3.14 |
| `unsafe` blocks, `unsafe(expr)` and `unsafe` functions | **Epitaxy extension** | raw-pointer boundary |

---

## 2. Cpp2 features not implemented in 0.2

These are parsed where the syntax is unambiguous, or rejected with a clear
diagnostic where they are not:

* **Custom metaprogramming / reflection.** Only `@value`, `@copyable`,
  `@interface` and `@enum` exist. There is no `@flag_enum`, `@union`,
  `@singleton`, `@regex`, `@autodiff`, `@print`, `@ordered`, and no user-defined
  metafunctions.
* **Template-name and concept `is` queries** such as `x is std::vector` or
  `x is std::integral`.
* **Rust-grade borrow checking**: Epitaxy has a conservative ownership/borrow
  checker (§3.14), but not non-lexical lifetimes, lifetime polymorphism, data
  race/`Send`/`Sync` analysis, or library-aware alias modelling.
* **Modules**: no `import`/`export`.
* **Mixed Cpp1/Cpp2 source.** Only preprocessor lines are passed through; you
  cannot put C++ declarations or C++ statement bodies in a `.ep` file.
* **Deferred `out` construction.** `out` parameters are plain references; there
  is no proxy that can construct an uninitialized argument, so arguments must
  already be initialized lvalues.
* **General C++ declarator syntax** for unusual types (e.g. pointer-to-member,
  references as object types, cv-qualified function types beyond the supported
  forms).
* **`?:` conditional operator**, `switch`, `goto`, `co_await`/coroutines.
* **Free-function operator overloads**, conversion operators, `operator()`
  overloads with arbitrary parameters, and `new`/`delete` overloads.
* **Static data members**, mutable members, `constexpr`/`consteval` qualifiers
  beyond the `==` alias form.
* **Multiple translation units / separate compilation** driven by Epitaxy
  itself; use C++ headers for interop.
* Cpp2's full flow-sensitive "definite last use" analysis (see §3.4).

---

## 3. Deliberate semantic deviations

These are choices made by this implementation; they are documented parts of the
Epitaxy language, not bugs.

### 3.1 `in` parameter passing

Cpp2's `in` is a smart pass-by-const-reference-or-value. Epitaxy emits, for a
named concrete type, `T const` when `T` is a fundamental type, enum or pointer,
and `T const&` otherwise; deduced parameters become `auto const&` (or the
appropriate form for the other parameter kinds). This keeps template argument
deduction working, which `cpp2::in<T>`-style aliases would break.

### 3.2 `is` and `as`

* `x is T` is an **exact static type query** (after removing cv/ref), with
  dynamic behaviour only for `std::any`, `std::variant`, `std::optional`, and
  polymorphic pointers via `x is *D`.
* Value queries are written with literals or **parenthesised** expressions:
  `x is 0`, `x is (category::tool)`. A bare qualified name after `is` is a type
  constraint. This keeps the grammar context free without symbol lookup.
* `as` uses list-initialisation (`T{x}`), so narrowing conversions are C++
  compile-time errors, matching Cpp2's "no maybe-lossy narrowing" rule.
* Template/concept queries are not available (see §2).

### 3.3 `$` captures

`$` capture is implemented in strings (interpolation), in postconditions
(captured on entry) and in function expressions (captured by value when the
expression is created). In other positions `expr$` simply evaluates the
expression where it is written.

Captures inside a postcondition *message* are evaluated when the message is
built (at violation time), not on function entry.

### 3.4 Automatic move from last use

The analysis is intentionally conservative and sound:

* only locals and parameters are considered;
* the use must be the last lexical use in the function, not inside a loop, not
  inside a function expression, and the variable's address must not have been
  taken;
* a returned local is not explicitly moved (C++ already implicitly moves it),
  but a returned `forward` parameter is forwarded.

Explicit `move`/`forward` is always available. With ownership checking enabled,
moves are also validated: moving from a borrowed place, moving twice, and using
a moved value are errors. The automatic last-use move is suppressed for
variables whose address is taken (and therefore for borrowed places), so it
cannot invalidate a live borrow.

### 3.5 `operator=` and special members

`operator=: (out this, that)` generates copy and move constructors and copy and
move assignments unless more specific functions are written. The memberwise
defaults are as follows:

* constructors initialise unmentioned members from `that` (copy/move) or from
  their declared default value;
* assignment operators assign unmentioned members from `that` (copy/move);
* leading `member = value;` statements are member initializers/assignments;
  `member = _;` skips a member;
* base subobjects are copy/moved automatically and default-constructed by
  converting constructors.

Explicit member initializers are used verbatim in the generated move variants
(i.e. a written `that.member` copies; it is not rewritten to a move).

### 3.6 Metafunctions

* `@value` emits default/copy/move operations and a defaulted `<=>`; it does not
  enforce Cpp2's restrictions on virtual/protected members.
* `@interface` forces `virtual` on member functions and gives the type a virtual
  destructor; data members are rejected.
* `@enum` uses `i32` as the default underlying type (not a minimal-size type),
  numbers enumerators from 1, and provides `to_string`/`ostream` output. It has
  no implicit conversion to or from the underlying type. `@flag_enum` is not
  implemented.
* A metafunction with unknown name or an unsupported metafunction is a
  compile-time error listing the supported set.

### 3.7 Named return values

A single named return value is returned as a plain value. Multiple named return
values are returned in a generated struct with the given member names, and the
named values must be default-constructible (they are default-constructed on
entry and assigned in the body).

### 3.8 Contract groups

C++ reserves the word `default`, so the Cpp2 group `default` is lowered to
`epi::default_`; in contract position you still write `default`. The other
built-in groups are `type_safety`, `bounds_safety`, `null_safety` and `testing`
(lowered to the same names in `::epi`). Any other group name is used verbatim
as an expression; user-defined groups may be `epi::contract_group` objects or
objects with `is_active()` and `report_violation(std::string)`.

### 3.9 Safety checks

* The mixed-sign rule currently covers the relational operators
  (`<`, `>`, `<=`, `>=`) and is a C++ `static_assert` in a helper template, so
  it is reported by the C++ compiler. Equality comparisons are not restricted.
* Bounds checks are applied when the container has `std::size` and random-access
  iterators; other subscripts (`std::map`, raw pointers) pass through unchanged.
* Null checks apply to raw pointers; smart pointers are not checked by the
  generated `deref`.
* Checks fire through the `epi::contract_group` objects, so they respect a
  replaced handler and can be disabled by deactivating the group.
* Ownership/borrow/move/unsafe rules are checked statically before lowering and
  are on by default; `-no-ownership-checks` disables them.

### 3.10 `main`

`main` may be declared as `main: ()` or `main: (args)`, with `void` or `int`
return; it is always lowered to `int main`. The implicit `args` parameter has
type `epi::args_t`.

### 3.11 String interpolation

Interpolated strings are lowered to concatenation via `epi::to_string` (which
handles booleans, characters, strings and streamable types), or to
`std::format` when a format specification is present. Types must therefore be
formattable or streamable to be interpolated.

### 3.12 `noexcept`, `constexpr`, `explicit`

For generated special members, move constructors/assignments are `noexcept`
(and assignments where written); user-declared special members keep their
declared form except that move operations generated from `(out this, that)` are
marked `noexcept`. Constructors are `explicit` unless marked `implicit`.
Functions declared with `==` are emitted `constexpr`.

### 3.13 Diagnostics

Errors are reported at the Epitaxy stage with `file:line:column` where possible.
Some errors (mixed-sign comparisons, narrowing in `as`, format-string errors,
rejected template queries) are diagnosed by the C++ compiler on generated code;
the C++ diagnostic mentions the generated file.

---

### 3.14 The ownership and borrow checking extension

This is the one deliberate **language extension** beyond Cpp2 syntax. Cpp2's
`in`/`inout`/`out`/`copy`/`move`/`forward` parameter kinds are its ownership and
borrow interface, but cppfront does not statically check moves, borrows or
lifetimes. Epitaxy adds that checking, plus the minimum syntax needed to express
it:

* `unique<T>` — move-only owning handle (`std::unique_ptr`);
* `shared<T>` — copyable shared owning handle (`std::shared_ptr`);
* `in T` / `inout T` — shared / exclusive borrow types, usable for local
  variables and return types (parameters continue to use `in`/`inout`/`out`);
* `-> in(param) T` — explicit borrow source annotation for returns;
* `unsafe { ... }`, `unsafe(expr)`, and `unsafe` functions — the boundary at
  which raw pointers (`*T`) may be created and dereferenced;
* `out x` at a call site — explicit out-argument marker.

Checked rules (compile-time errors):

1. use-after-move; double move; moving a borrowed place;
2. borrowing a moved value; a borrow outliving its owner; borrowing a
   temporary; borrow types as data members;
3. shared/exclusive borrow conflicts on the same place; direct reads while an
   exclusive borrow is live; direct writes/moves while any borrow is live;
4. returning a borrow of a local or of an owned parameter; ambiguous borrow
   returns without `in(param)`; `inout` returns from shared sources;
5. copying `unique<T>` (transfer requires `(move x)` or a return);
6. `inout`/`out` arguments that are not lvalues; aliased arguments with
   conflicting borrows; `out` parameters not assigned on all paths;
7. raw-pointer creation/dereference and calls to `unsafe` functions outside an
   unsafe context;
8. moving a variable declared outside the current loop inside the loop;
9. function expressions capturing borrows, moved values or move-only values.

Deliberate limitations: lexical borrow lifetimes (no NLL), no lifetime
variables/generics, no `Send`/`Sync`, no data-race detection, and no modelling
of C++ library aliasing (external calls are trusted; only explicit `move`
arguments are tracked). The checker never rejects a program because of C++
library internals it cannot see.

## 4. What "syntax requirement" means here

The syntax implemented here is the Cpp2 surface syntax as publicly documented
for cppfront, plus the ownership extension described in §3.14 (unified `name: kind = value` declarations, left-to-right types,
postfix unary operators, UFCS, `inspect`, `is`/`as`, contracts with `pre`/`post`/
`assert`, string interpolation with `$`, `..<`/`..=`, `@` metafunctions, named
`operator=` value operations, and so on). Where Cpp2's documented semantics
depend on whole-program C++ type information or on cppfront's own reflection
machinery — which Epitaxy 0.2 does not implement — this specification defines a
smaller, explicit behaviour rather than guessing, and the restriction is listed
above.

---

## 5. Comparison with cppfront's runtime surface

Cppfront programs can name the support namespace `cpp2::`. Epitaxy programs use
`epi::` for the same purposes (`epi::default_`, `epi::contract_group`,
`epi::args_t`, `epi::subscript`, …). The support header included with Epitaxy is
original MIT-licensed code; it does not include or adapt cppfront's
`cpp2util.h`.
