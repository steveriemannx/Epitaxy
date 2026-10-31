# Cpp2 compatibility and deliberate deviations

Epitaxy follows the *syntax requirements* of Cpp2 (as documented for cppfront),
but it is an independent implementation with its own specification. On top of
the Cpp2 syntax it adds an **ownership and borrow checking extension** (§3.14) and explicit C++ escape hatches (`cpp1 { ... }`, default prelude).
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
| `cpp1 { ... }` raw C++ blocks (namespace/type/statement scope) | supported | Epitaxy escape hatch; opaque to the ownership checker |
| default C++ standard-library prelude | supported | curated `#include` set, disabled with `-no-prelude` |
| `#include`/preprocessor lines | supported (passed through) | |
| `__identifier__keyword` escaped identifiers | supported | |
| `@metafunction` type declarations | partial — see §4 | 8 built-in metafunctions |

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
| `@value`, `@copyable`, `@interface`, `@enum`, `@flag_enum`, `@ordered` family, `@basic_value`, `@singleton` | supported | see §3.6 |
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
| Local scoped parameter blocks `(x: T = v) { … }` and immediate declarations `(copy i := 0) while …` | supported | |
| Member `operator()` and `operator[]` | supported | lowered as member operator functions |
| Defaulted memberwise comparisons `operator<=>: (this, that) -> …;` / `operator==: …;` | supported | emitted as defaulted C++ comparison operators |
| Contract group `unevaluated` | supported | the contract is parsed and then ignored |

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

## 2. Cpp2 features not implemented in 0.2 (summary)

The short list is:

* **Reflection and metaprogramming**: no reflection API, no user-defined
  metafunctions, and only the built-in metafunctions (`@value`, `@copyable`,
  `@interface`, `@enum`, `@flag_enum`, the ordered family, `@basic_value`,
  `@singleton`).
* **Implicit mixed Cpp1/Cpp2 source**: only preprocessor lines and explicit
  `cpp1 { ... }` blocks are passed through; C++ declarations/statements cannot be
  freely interleaved.
* **Modules / multi-file builds**: none.
* **Full `is`/`as`**: no template-name, concept or value-predicate queries; no
  `std::expected` or user customization.
* **`out` parameter construction semantics**: plain references, no guaranteed
  initialisation proxy.
* **cppfront tooling parity** and the `cpp2::` support-library namespace.
* **Assorted syntax**: template-template parameters, general value-list
  expressions, `forward_ref` enforcement, memberwise defaulting beyond
  `<=>`/`==`.

The complete inventory, including narrower-than-Cpp2 behaviour (metafunctions,
`operator=` matrix, contracts, safety-check coverage, interpolation, `in`/`inout`
semantics, and so on) is in section 4.

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

* `@value` emits default/copy/move operations plus memberwise `operator==` and
  `operator<=>`; it does not enforce Cpp2's restrictions on virtual/protected
  members. `@basic_value` emits only the copy/move operations;
  `@weakly_ordered_value` / `@partially_ordered_value` add the corresponding
  ordering.
* `@ordered` / `@weakly_ordered` / `@partially_ordered` generate memberwise
  comparisons (`@ordered` uses a defaulted comparison; the weak/partial forms
  generate a lexicographic `std::weak_ordering` / `std::partial_ordering`).
* `@flag_enum` emits a bit-flag type: power-of-two enumerators by default,
  explicit values (including bitwise combinations of earlier enumerators),
  `|`, `&`, `^`, `~`, `.has`, `.set`, `.clear`, comparisons and printing. The
  default underlying type is `u32` (not minimal-size).
* `@singleton` makes construction/copy/move private or deleted and generates
  `static T& instance()`; the type must be default constructible.
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
* Strict safety mode (`-strict-safety`) forces every check on, lowers integer
  arithmetic to checked helpers and requires raw C++ (`cpp1`) to be inside
  `unsafe`. Its conditional guarantee and trust boundary are documented in
  [`SAFETY.md`](SAFETY.md).
* Epitaxy is **not Rust-certified**: no data-race/`Send`-`Sync` analysis, no NLL,
  and C++ interop is a trusted boundary. See [`SAFETY.md`](SAFETY.md) for the full
  comparison and the path to an unconditional guarantee.

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

## 4. Detailed gap inventory versus Cpp2

This section is the complete inventory of Cpp2 features that Epitaxy 0.2 does
**not** implement or implements with narrower semantics. (Section 3.14 describes
the one place where Epitaxy goes *beyond* Cpp2: ownership/borrow checking.)

### 4.1 Not implemented at all

1. **Reflection and metaprogramming.** Cppfront's metafunction library is
   written in Cpp2 on top of a reflection API. Epitaxy has no reflection API and
   no user-defined metafunctions, and it supports only these built-ins:
   `@value`, `@copyable`, `@interface`, `@enum`, `@flag_enum`, `@ordered`,
   `@weakly_ordered`, `@partially_ordered`, `@basic_value`,
   `@weakly_ordered_value`, `@partially_ordered_value`, `@singleton`.
   Missing: `@union`, `@regex`, `@autodiff`, `@print`, and any way to customize
   (suppress or extend) the members a metafunction generates.

2. **Implicit mixed Cpp1/Cpp2 source (intentional non-goal).** A `.ep` file
   cannot freely interleave C++ declarations, statements and expressions with
   Cpp2 code. Two explicit escape hatches are provided instead: `cpp1 { ... }`
   blocks pass raw C++ through verbatim at namespace, type or statement scope,
   and the default C++ standard-library prelude removes the need for most
   `#include` lines. Raw C++ blocks are opaque to the ownership checker and are
   emitted before Epitaxy type definitions and globals. This keeps Epitaxy a new
   language rather than a C++ syntax mode; C++ interop otherwise happens at the
   header/library level.

3. **Modules.** No `import`/`export`, no `-import-std`, no multi-file
   compilation: one translation unit at a time.
4. **The full `is` / `as` query system.**
   * no template-name queries (`x is std::vector`);
   * no concept queries (`x is std::integral`);
   * no value-predicate queries (`x is (in(10, 20))`); a parenthesised constraint
     is only an equality value;
   * dynamic queries are limited to `std::any`, `std::variant`,
     `std::optional` and polymorphic-pointer `*D`; there is no
     `std::expected` support (the target is C++20) and no customization/extension
     point for user types;
   * `as` has no customization points and no `std::expected` support.
5. **`out` parameter construction semantics.** Cpp2 `out` parameters support
   guaranteed initialisation and construction-instead-of-assignment through an
   `out<X>` proxy. Epitaxy lowers `out` to a plain `T&`, so the argument must
   already exist and be default-constructible; there is no deferred
   initialisation and no per-object initialisation tracking.
6. **cppfront's compiler options / tooling.** cppfront offers options such as
   `-clean`, `-import-std`, `-include`, syntax-only checking, warning modes,
   colour control and Cpp1-emission controls. Epitaxy has a deliberately small
   CLI (output/emit/run/compile, `--keep`, safety toggles).
7. **The `cpp2::` support-library surface.** Epitaxy's runtime is `epi::`, not
   `cpp2::`; code written against cppfront's `cpp2util.h` names
   (`cpp2::contract_group`, `cpp2::args_t`, `cpp2::to_string`, …) will not
   compile unmodified. `epi::args_t` is also a subset of the Cpp2 `args` API.
8. **Misc syntax**: template-template parameters; `@metafunction` arguments
   other than `@enum`'s underlying type; named-return and `operator=`
   interactions; non-standard `main` return types; general parenthesised value
   lists in arbitrary expressions; `noexcept`/`constexpr`/`explicit`
   control beyond what Epitaxy documents.

### 4.2 Implemented, but narrower than Cpp2

| Area | What is narrower |
| --- | --- |
| `is` / `as` | exact static type queries plus the dynamic cases above; no concepts/template names/predicates/customization |
| `@enum` | `i32` default underlying type; no implicit conversion to/from the underlying type; only member functions in the body |
| `@flag_enum` | `u32` default underlying type (not minimal-size); explicit values must be constant expressions evaluable at transpile time; generated `to_string` falls back to hexadecimal for combined values |
| `@value` / `@copyable` / `@basic_value` / `*_value` | generate the defaulted operations (and comparisons for the value family), but do not enforce Cpp2's restrictions (e.g. no virtual/protected members) |
| ordered family | `@ordered` uses a defaulted comparison; `@weakly_ordered` / `@partially_ordered` generate lexicographic comparisons over bases and data members. Member comparison categories must be convertible to the requested ordering. |
| `@singleton` | requires a default-constructible type; copy/move operations are deleted (Cpp2's sketch only privatizes the default constructor) |
| `@interface` | forces `virtual` and a virtual destructor, rejects data members; no `@print` or other interface helpers |
| `operator=` | supports the documented subset of the constructor/assignment matrix; explicit member assignments are used verbatim in generated move variants; contracts on `operator=` are an error, not applied |
| Memberwise defaulting | only `<=>` and `==` can be defaulted memberwise; other defaulted special members are not generated |
| Contracts | `pre`/`post`/`assert`, groups, predicates, messages and `$` captures work, including on constructors/assignment/destructor; message captures are evaluated at violation time, not entry; `unevaluated` is supported |
| `in` parameters | Epitaxy's by-value-vs-`const&` choice is a heuristic (and keeps template deduction working); Cpp2 uses a dedicated `cpp2::in<T>` type with slightly different observable behaviour |
| `forward` parameters | no extra "convertible-to" requires-constraint when a concrete type is named |
| Return deduction | `-> forward _` and `-> forward_ref _` are approximated with `decltype(auto)`; `forward_ref _` is not enforced to be a reference |
| Automatic last-use move | conservative approximation (no full control-flow analysis); suppressed where the address is taken |
| String interpolation | `std::format` for format specs; types must be formattable or streamable; no Cpp2 `to_string` customization point; `$`/`(` escapes are Epitaxy-specific (`\$`, `\(`) |
| Function expressions | capture outer variables by reference; `$` captures by value; borrows, moved values and move-only values cannot be captured |
| Safety checks | mixed-sign checks cover relational operators only (compile time); bounds checks only for sized random-access containers; null checks only for raw pointers; division checks only for integers |
| `args` | `epi::args_t` provides `argc`, `argv`, `[]`, `size`, `ssize`, `begin`/`end`; not every `std::array`-like operation |
| Immediate parameter blocks | supported, including `(copy i := 0)`; parameter kinds other than `copy`/`in` are not interpreted specially |
| Diagnostics | contract messages carry `basename:line: function: kind`; C++ diagnostics expose the generated file |

### 4.3 Deliberately enabled in 0.2.0 (previously gaps)

For the record, these Cpp2 forms were added while preparing this inventory:
immediate local declarations with `:=` in scoped parameter blocks, the
`unevaluated` contract group, defaulted memberwise `<=>`/`==`, and member
`operator()` / `operator[]` lowering (including the checked-subscript fallback
for types that only provide `operator[]`).

In 0.3.0: contracts on `operator=` special members are checked; the ordered
metafunction family, `@flag_enum`, `@singleton`, `@basic_value` and the
`@*_ordered_value` combinations are implemented; `@value` now also generates
memberwise `operator==`; qualified names with template arguments on inner
segments (e.g. `std::numeric_limits<double>::quiet_NaN()`) are accepted.

In 0.4.0: `cpp1 { ... }` raw C++ blocks (namespace/type/statement scope) and a
default curated C++ standard-library prelude (`-no-prelude` to disable) were
added as explicit interop escape hatches; this does not enable implicit mixed
source mode.

In 0.5.0: **strict safety mode** (`-strict-safety`) with mandatory checks,
checked integer arithmetic (`epi::checked_*`), and `cpp1` blocks confined to
`unsafe`; the conditionally-guaranteed safe subset, its trust boundary and the
non-guarantees are specified in [`SAFETY.md`](SAFETY.md).

## 5. What "syntax requirement" means here

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

## 6. Comparison with cppfront's runtime surface

Cppfront programs can name the support namespace `cpp2::`. Epitaxy programs use
`epi::` for the same purposes (`epi::default_`, `epi::contract_group`,
`epi::args_t`, `epi::subscript`, …). The support header included with Epitaxy is
original MIT-licensed code; it does not include or adapt cppfront's
`cpp2util.h`.
