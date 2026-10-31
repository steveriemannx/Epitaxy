# Epitaxy safety model

This document defines **what Epitaxy does and does not guarantee**. It exists
because "safe" is meaningless without a threat model, and because Epitaxy is
often compared to Rust — a comparison that only makes sense if the exact
guarantee boundary is written down.

**Epitaxy 0.5 is not Rust-certified.** It implements a Rust-style ownership and
borrow checker and a default-on checked runtime, but it compiles to C++20 and
lets you call arbitrary C++ code. A Rust-style *unconditional* guarantee — "no
undefined behaviour in safe code" — requires an execution model Epitaxy does not
have yet (see [the path to an unconditional guarantee](#the-path-to-an-unconditional-guarantee)).

---

## 1. Safety levels

| Level | How to enable | What you get |
| --- | --- | --- |
| **Default** | (default) | Ownership/borrow checking, bounds/null/div-zero/sign checks, `unsafe` boundary for raw pointers. Individual checks can be disabled with `-no-*` flags. **Best effort, not a guarantee.** |
| **Strict** | `-strict-safety` | All checks forced on (disabling flags are rejected), checked integer arithmetic, `cpp1` blocks require `unsafe`. This is the level the **conditional guarantee** applies to. |
| *(future)* **Certified** | own backend/runtime | Rust-equivalent guarantee in safe code. Not implemented. |

`-strict-safety` forces the following on, regardless of the other flags:

* `-no-subscript-checks`, `-no-null-checks`, `-no-div-zero-checks`,
  `-no-comparison-checks`, `-no-ownership-checks` are rejected as conflicting;
* `+`, `-`, `*`, `<<`, `>>`, `+=`, `-=`, `*=`, `++`, `--`, unary `-` and `/=`/`%=`
  are lowered to checked helpers (`epi::checked_*`) that abort on integer
  overflow, invalid shifts or division by zero;
* `cpp1 { ... }` blocks are rejected unless they are inside an `unsafe` block or
  an `unsafe` function;
* ownership/borrow/move checking cannot be disabled.

## 2. The conditional guarantee

> **In strict mode, Epitaxy code that contains no `unsafe` constructs, no
> `cpp1` blocks and no raw-pointer operations cannot trigger undefined behaviour
> *through Epitaxy's own operations*: every `+`, `-`, `*`, `/`, `%`, `<<`, `>>`,
> subscript, pointer dereference and compare that Epitaxy lowers is either
> statically rejected or checked at run time, and ownership/borrow/move errors
> are compile-time errors.**

"Through Epitaxy's own operations" is the important qualifier. Everything that
crosses into C++ is a **trust boundary**:

* Calling a C++ function or using a C++ type (including the C++ standard
  library) is trusted. Epitaxy cannot prove that `std::vector::operator[]`,
  a third-party library, or your in-house C++ code is free of undefined
  behaviour.
* `unsafe` blocks/functions and `cpp1` blocks are *outside* the guarantee by
  construction: they are where you promise to uphold C++'s rules yourself.
* `unchecked_*` operations (`unchecked_cast`, `unchecked_narrow`,
  `unchecked_subscript`, `unchecked_dereference`, `unchecked_div`, …) are
  explicit opt-outs: the operation they cover is outside the guarantee.
* The program still runs on the C++ toolchain, so C++-level UB that arises
  inside C++ libraries, in `unsafe` code, or from integer overflow in C++
  library internals is possible.

So the honest statement is:

> Epitaxy strict mode gives a **sound checked subset**, not Rust's blanket
> guarantee. The subset is exactly "Epitaxy code without `unsafe`, `cpp1`,
> raw pointers or `unchecked_*`, using C++ APIs whose own safety you trust".

## 3. What is checked where

| Failure mode | Compile time | Run time |
| --- | --- | --- |
| use-after-move, double move | ✔ | |
| move/mutate while borrowed | ✔ | |
| conflicting borrows / call aliasing | ✔ | |
| borrow of temporary, borrow outliving owner | ✔ | |
| returning a borrow of a local | ✔ | |
| copying `unique<T>` | ✔ | |
| `out` parameter not assigned on all paths | ✔ | |
| raw pointer creation/dereference outside `unsafe` | ✔ | |
| calling `unsafe` functions outside `unsafe` | ✔ | |
| `cpp1` outside `unsafe` (strict) | ✔ | |
| narrowing in `as` | ✔ | |
| mixed-sign relational comparisons | ✔ | |
| subscript out of bounds (sized random-access containers) | | ✔ |
| null raw-pointer dereference | | ✔ |
| integer division/remainder by zero | | ✔ (always, not a disableable contract) |
| integer overflow (strict) | | ✔ (always, abort) |
| invalid shift count (strict) | | ✔ (always, abort) |

Compile-time checks are reported by the Epitaxy compiler; runtime checks report
through the contract mechanism and terminate. In strict mode the arithmetic
checks cannot be turned off; a custom contract handler can observe the message
but the process still aborts, so a returning handler cannot hide an overflow.

## 4. The `unsafe` boundary

The only way to leave the checked subset is explicit:

```cpp2
// An unsafe function: its body may use raw pointers and cpp1 blocks.
read_raw: unsafe (p: *i32) -> i32 = {
    return p*;
}

main: () = {
    x := 42;
    raw: *i32 = unsafe(x&);          // creating a raw pointer needs unsafe
    value := unsafe(read_raw(raw));  // calling an unsafe function needs unsafe
}
```

Rules for keeping `unsafe` sound (documented, not machine-checked):

1. keep unsafe scopes as small as possible;
2. every raw pointer must point to a live object for the whole use, be
   correctly aligned and non-null;
3. do not let raw pointers/`unsafe` references escape into safe code unless the
   safe API's contract covers it;
4. `cpp1` blocks must be valid C++ that upholds the same invariants;
5. note in the code *why* each unsafe block is sound.

## 5. What is not guaranteed (yet)

These are the gaps relative to Rust. They are the reason this document says
"conditional guarantee" rather than "memory safe" or "Rust-level".

| Rust guarantee | Epitaxy 0.5 |
| --- | --- |
| no UB in safe code | conditional: excluded C++/unsafe/`unchecked_*` surface only |
| no data races | **not checked at all** — no `Send`/`Sync`-equivalent traits or thread model |
| full lifetimes / NLL | lexical borrow lifetimes only; no lifetime generics; no interior mutability model |
| no null | `nullptr` exists; runtime null checks instead |
| no uninitialized reads | `out` parameters are plain references; data members may be default-initialised |
| integer overflow never silent | strict mode checks arithmetic Epitaxy lowers; C++ library internals are not checked |
| panic = defined unwind | contract violations terminate; exceptions are C++ exceptions |
| exhaustive matching | `inspect` requires a catch-all but does not check type-set completeness |

## 6. Comparison to Rust in one table

| Mechanism | Rust | Epitaxy 0.5 |
| --- | --- | --- |
| ownership + move | yes | yes (for Epitaxy code; conservative automatic last-use move) |
| borrow checking | NLL, lifetimes, generics | lexical scopes, explicit borrow sources, no lifetime generics |
| bounds checking | always (arrays/Vec) | checked by default for sized random-access containers; can be disabled outside strict |
| integer overflow | debug checks / explicit APIs | strict mode checks lowered arithmetic; otherwise unchecked |
| raw pointers | `unsafe` | `unsafe` |
| FFI | `unsafe` boundary | C++ calls are *trusted*, raw C++ is `unsafe`/`cpp1` |
| data races | compile-time `Send`/`Sync` | not checked |
| no-UB guarantee | yes (safe code) | **no** (conditional subset only) |

## 7. Recommended practice

* Use `-strict-safety` for new Epitaxy code, especially libraries. It keeps
  arithmetic checked and prevents accidental raw C++.
* Keep `unsafe` scopes at library boundaries; wrap C++ pointer APIs in small
  `unsafe` helpers with a documented contract.
* Use `CXXFLAGS="-fsanitize=address,undefined -fno-sanitize-recover=all"` in CI
  and tests (`epitaxy --run` passes `CXXFLAGS` through) to catch C++-side
  undefined behaviour that the checker cannot see.
* Prefer value types, `unique<T>`/`shared<T>` and borrows over raw pointers.
* Run the test suite with both clang/libc++ and GCC/libstdc++.

## 8. The path to an unconditional guarantee

To make a Rust-equivalent promise, Epitaxy would need all of:

1. **a real front end** — typed IR and semantic analysis, so more properties can
   be checked at compile time;
2. **NLL and lifetime generics**, plus interior-mutability modelling;
3. **a thread model** — `Send`/`Sync`-equivalent traits and checked concurrency;
4. **an execution model that cannot silently produce UB** — either an owned
   backend/runtime (no C++), or a fully modelled, trusted C++ core with
   sanitizer-backed CI;
5. **a formalized safe subset** and a trusted-FFI mechanism.

Items 1–3 are incremental (weeks-to-months each); item 4 is essentially the
"own backend" project and is the real reason Epitaxy cannot claim Rust-level
safety while targeting C++. Until then, this document is the contract: a
**sound, checked subset with an explicit trust boundary**, documented and
tested, rather than a blanket promise.

---

*See also: [`SPEC.md`](SPEC.md) §10 (safety), [`COMPATIBILITY.md`](COMPATIBILITY.md)
§3.9 and §4.2, and [`DESIGN.md`](DESIGN.md).*
