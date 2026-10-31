# The Epitaxy Language Specification

**Version 0.2 (draft)**

This document defines the Epitaxy programming language: its source text, its
grammar, and the meaning of its constructs. Epitaxy's surface syntax follows the
Cpp2 syntax requirements documented for cppfront; this specification defines the
subset that Epitaxy 0.2 implements, together with the exact lowering to C++20
used by the Epitaxy compiler.

Conformance for version 0.2 means: a conforming implementation accepts the
programs described here and produces C++20 that compiles and executes with the
documented semantics. See [`COMPATIBILITY.md`](COMPATIBILITY.md) for constructs
that are deliberately not part of Epitaxy 0.2.

---

## 1. Overview

An Epitaxy source file (conventionally `*.ep`) is a sequence of *preprocessor
lines* and *declarations*. Every declaration has the same shape:

```
name : kind = value
```

Read as "`name` is a `kind` defined as `value`". Writing `==` instead of `=`
declares an alias: "`name` is a `kind` defined as a synonym for `value`".

Some parts may be omitted, and the same grammar is reused for functions, types,
namespaces, objects and aliases. The grammar is context free: the parser never
needs to look up names to decide how to parse a construct.

The compiler lowers a translation unit to one C++20 source file that includes
the Epitaxy support header `epi_support.hpp`. The generated program uses normal
C++ types, functions and libraries.

---

## 2. Lexical structure

### 2.1 Source text

Source files are UTF-8 text. Lines end with `\n` (a `\r\n` ending is also
accepted). Spaces, tabs, form feeds and vertical tabs are whitespace and are
ignored except as token separators. Layout carries no meaning beyond that: a
declaration or statement is delimited by its own grammar, not by newlines.

### 2.2 Comments

```
// line comment: runs to the end of the line
/* stream comment: runs to the next */
```

Stream comments do not nest.

### 2.3 Preprocessor lines

A line whose first non-whitespace character is `#` is a *preprocessor line*. It
is copied verbatim (including continuation lines ending in `\`) to the top of
the generated C++ file, in source order. This is how C++ headers are included:

```cpp2
#include <iostream>
#include <string>
```

Preprocessor lines are also accepted inside blocks and type bodies, where they
are copied at that position.

### 2.4 Identifiers

An identifier starts with a letter or `_` and continues with letters, digits or
`_`. A name that would otherwise be interpreted as an Epitaxy contextual
keyword can be forced to be an ordinary identifier by prefixing it with
`__identifier__`; the prefix is removed in the generated C++
(`__identifier__inspect` becomes the C++ identifier `inspect`). This does not
make C++ reserved words usable as C++ identifiers.

```cpp2
__identifier__inspect: i32 = 3;    // an object named "inspect"
__identifier__is:      i32 = 4;    // an object named "is"
```

Identifiers beginning with `epi__` are reserved for the compiler.

### 2.5 Numeric literals

Decimal, octal (`0…`), hexadecimal (`0x…`) and binary (`0b…`) integers are
supported, as are floating-point literals with exponents (`1.5e3`, `0x1p4`),
digit separators (`1'000'000`), standard suffixes (`u`, `l`, `ll`, `z`, `f`)
and C++ user-defined literal suffixes, which are passed through to C++.

### 2.6 Character literals

A character literal is a C++ character literal: `'a'`, `'\n'`, `u8'x'`, and so
on. It is passed through to C++.

### 2.7 String literals and interpolation

Epitaxy supports C++ string literal syntax, including encoding prefixes
(`u8`, `u`, `U`, `L`) and raw strings:

```cpp2
plain := "C++ string literal, escapes are C++ escapes: \n \t \"";
raw   := R"(no interpolation here: (x)$ )";
```

A non-raw string literal may contain *interpolations*. An interpolation is
`(expr)$` (the `$` is pronounced "paste the value of"):

```cpp2
name := "world";
std::cout << "Hello, (name)$!\n";        // prints: Hello, world!
```

* A string literal that contains at least one interpolation has type
  `std::string`; otherwise it is an ordinary C++ string literal (array of
  characters).
* An interpolation may carry a standard C++ *format specification*, written
  after a colon: `(value:spec)$`. Epitaxy lowers these to `std::format` with a
  compile-time-checked format string, so `(pi:.2f)$`, `(n:#x)$`, `(s:>8)$` all
  use the standard C++20 formatting rules.
* `$` and `(` inside string literal text are escaped as `\$` and `\(`.
  Interpolations do not nest.
* Multiple interpolations may appear in one literal.

### 2.8 Operators and punctuation

The following multi-character tokens are recognised (longest match first):

```
..<   ..=   <=>   <<=   >>=   ...   ::   :=   ==   !=   <=   >=
->    &&    ||    ++    --    +=   -=   *=   /=   %=   &=   |=   ^=
<<    >>    ..    {    }     (    )    [    ]    <    >    ,    ;
:     =     +     -     *     /    %    &    |    ^    !    ~    .    $
```

`@name` is a metafunction reference (only valid in type declarations).

---

## 3. Declarations

### 3.1 The unified declaration grammar

```
declaration     := preprocessor-line
                 | using-declaration
                 | named-declaration
                 | operator-declaration
named-declaration := IDENT [ "..." ] ":" kind [ "=" | "==" ] value
                   | IDENT ":=" expression ";"
operator-declaration := "operator" op-symbol ":" function-kind [ "=" | "==" ] value
```

`kind` starts with optional template parameters and ends with optional
`requires` constraints and contracts:

```
kind := [ template-parameters ]
        ( "type" | "namespace" | function-signature | object-type )
        [ contracts ] [ "requires" expression ]
```

The `value` is either a `{ ... }` body (block) or a single expression/statement
ending in `;`. The meaning of the declaration depends on `kind`:

| Kind | Declaration |
| --- | --- |
| `type` | a user-defined type (or an alias with `==`) |
| `namespace` | a namespace (or a namespace alias with `==`) |
| `( parameter-list ) …` | a function |
| a type expression | an object (or an object alias with `==`, or a variable template) |
| `operator op` | a special member function or operator function |

`=` means "defined as"; `==` means "defined as a synonym for" and declares a
constant/alias that is always the same value.

### 3.2 Declaration scopes

Declarations may appear at namespace scope (including the translation-unit top
level), inside a type body, and inside a function body. At function scope only
objects, aliases, `using` declarations and preprocessor lines are allowed;
functions, types and namespaces cannot be declared inside a function.

Declaration order does not matter for functions, types and namespace-scope
objects: the compiler emits the necessary forward declarations. The one
exception is a function whose return type is *deduced*: C++ cannot declare such
a function without its body, so a function with a deduced non-void return type
must be defined before it is called (the compiler emits deduced-return
functions before other functions to make this work in common cases).

### 3.3 Template parameters

A template parameter list is `<(parameter), …>` placed before the kind. Each
parameter has the form `name [: kind] [= default]`, and a parameter without a
kind is a type parameter. A name may be suffixed with `...` to declare a
parameter pack.

```cpp2
pair: <T: type, U: type> type = { first: T = (); second: U = (); }

array: <T: type, size: i32> type = { /* ... */ }

print: <Args...: type> (inout out: std::ostream, args...: Args)
    requires sizeof...(Args) >= 1
= {
    (out << ... << args);
}
```

Template parameters are lowered to C++ `template<...>` parameters (`typename T`,
`i32 N`, `typename... Ts`).

### 3.4 requires constraints

A `requires` clause may follow the function signature (or, in a templated
declaration, the kind). The condition is an expression; if it is false the
specialization is not a candidate. It is lowered to a C++20 requires-clause.

### 3.5 Contracts

Function declarations may carry preconditions and postconditions between the
signature and the body:

```cpp2
insert_at: (inout container, where: int, val: int)
    pre<bounds_safety>(0 <= where <= container.ssize(), "position (where)$ is out of bounds")
    post(container.ssize() == container.ssize()$ + 1)
= {
    // ...
}
```

See section 9 for the full contract model.

### 3.6 Access specifiers

Inside a type body, the keywords `public`, `private` and `protected` at the
start of a declaration set the access of that declaration:

```cpp2
point: type = {
    x: i32 = 0;              // data members are private by default
    public y: i32 = 0;       // explicitly public data member
    sum: (this) -> i32 = x + y;   // functions are public by default
    private helper: (this) = { };
}
```

### 3.7 `using` declarations

```cpp2
using geometry::point;      // make one name visible
using geometry::_;          // make a whole namespace visible (namespace import)
using namespace std::chrono_literals;  // C++ spelling also accepted
```

The `namespace::_` form is lowered to `using namespace namespace;`.

### 3.8 Unnamed declarations

Inside an expression, a declaration may omit its name (the declaration then
starts with `:`):

```cpp2
:(x: i32) = std::cout << x;      // a function expression (lambda)
:widget = 42;                    // a temporary object of type widget
```

---

## 4. Types

### 4.1 Type expressions

```
type        := { "const" | "*" } type-atom [ "is" constraint ]
             | ("in" | "inout" | "out") type                  // borrow type
type-atom   := name [ "<" template-arguments ">" ]
             | "_"
             | "(" parameter-list ")" [ "->" return-type ]     // function type
             | "type_of" "(" expression ")"
             | "decltype" "(" expression ")"
```

Types are written left to right, and each qualifier applies to what immediately
follows it. `const T` is a const `T`; `*T` is a pointer to `T`; a `*` after a
`const` makes the pointer const:

```cpp2
p: const * * const i32;   // const pointer to non-const pointer to const i32
q: const i32;             // const i32
r: *i32;                  // pointer to i32
s: std::function< (i: i32) -> std::string >;   // function type argument
```

A `*` before a function type is a function pointer type:

```cpp2
callback: * (i: i32) -> std::string = handler&;
```

### 4.2 Fundamental type aliases

The Epitaxy prelude provides these unqualified aliases:

| Alias | C++ type |
| --- | --- |
| `i8`, `i16`, `i32`, `i64` | `std::int8_t`, `std::int16_t`, `std::int32_t`, `std::int64_t` |
| `u8`, `u16`, `u32`, `u64` | `std::uint8_t`, `std::uint16_t`, `std::uint32_t`, `std::uint64_t` |
| `ushort`, `uint`, `ulong` | `unsigned short`, `unsigned int`, `unsigned long` |
| `longlong`, `ulonglong` | `long long`, `unsigned long long` |
| `longdouble` | `long double` |
| `_schar`, `_uchar` | `signed char`, `unsigned char` (interop only) |

All normal C++ fundamental types (`bool`, `char`, `int`, `double`, …) are also
available. Types from C++ headers are used by their C++ names.

### 4.3 Deduced type `_`

`_` in a type position means "deduce the type". In a declaration it is
equivalent to omitting the type. It may carry a constraint with `is`:

```cpp2
number: _ is std::regular = some_factory();
```

The constraint is checked with a generated `static_assert`.

### 4.4 Owning and borrowed types

Two owning handle types and two borrow types are available:

| Type | Meaning | Lowered C++ |
| --- | --- | --- |
| `unique<T>` | exclusive owning handle, move-only | `std::unique_ptr<T>` |
| `shared<T>` | shared owning handle, copyable | `std::shared_ptr<T>` |
| `in T` | shared (read-only) borrow of a `T` place | `T const&` |
| `inout T` | exclusive (read-write) borrow of a `T` place | `T&` |

`*T` remains a raw pointer, but is only usable inside `unsafe` contexts (see
section 11). Borrow types may be used for parameters (alternatively via the
`in`/`inout` parameter kinds), local variables and return types; they may not be
data members.

```cpp2
r: in i32 = total&;          // shared borrow of total
m: inout i32 = total&;       // exclusive borrow of total
p: unique<widget> = new<widget>();
s: shared<widget> = shared.new<widget>();
```

### 4.5 Constraints in parameter types

A deduced parameter type may be constrained:

```cpp2
print: (x: _) = { std::cout << x; }
print: (x)    = { std::cout << x; }            // same thing
calc:  (n: _ is std::integral) = { /* ... */ } // requires an integral type
```

---

## 5. Functions

### 5.1 Syntax

```
name ":" [template-parameters] "(" parameter-list ")" [ "->" return ] [contracts] "=" body
```

The body is a `{ ... }` block or a single expression/statement ending in `;`.
A function declared with `==` instead of `=` is lowered to a C++ `constexpr`
function:

```cpp2
square: (i: i32) -> _ == i * i;
```

### 5.2 Parameters

Each parameter is `[kind] [name] [":" type] ["=" default]`, and a name may be
followed by `...` to declare a parameter pack. The parameter kinds are:

| Kind | Meaning | Lowered C++ type |
| --- | --- | --- |
| `in` (default) | read-only; accepts anything | `T const` for scalars, enums and pointers, otherwise `T const&` (`auto const&` for deduced types) |
| `in_ref` | read-only, bound by reference | `T const&` |
| `copy` | a copy, modifiable local | `T` |
| `inout` | read and write an lvalue | `T&` |
| `out` | write to an lvalue | `T&` |
| `move` | consume an rvalue | `T&&` |
| `forward` | forward an rvalue/lvalue | `T&&` (`auto&&` for deduced types) |
| `forward_ref` | forward by reference | `T&&` |

Under the ownership model (section 11), these kinds are also the borrow/ownership
interface: `in`/`in_ref` are shared borrows, `inout`/`out` are exclusive borrows
(`out` additionally requires assignment), `copy` requires a copyable type, and
`move`/`forward` transfer ownership.

A parameter may omit its name (written `_`) when it is unused. Deduced
parameters use abbreviated C++ templates (`auto const&`, `auto&`, `auto`).

Default arguments are supported; they are emitted only on the generated C++
declaration.

### 5.3 `this` and `that`

Inside a type body, the first parameter may be `this`, which has the enclosing
type. `this` is not a pointer; inside the body, member names refer to the
current object.

```cpp2
print: (this, msg: std::string) = std::cout << data << msg;   // const member
translate: (inout this, dx: i32) = { x += dx; }               // non-const member
```

The kind of `this` selects the C++ member function form:

| `this` | C++ member |
| --- | --- |
| `this` (same as `in this`) | `const` member function |
| `inout this` | non-const member function |
| `move this` with no other parameters | destructor |
| `out this` | constructor (see section 6.3) |
| `virtual this`, `override this`, `final this` | virtual / overriding / final function |

`that` is the second parameter of a copy/move-style operation; its type is the
enclosing type. `that` is `T const&` by default and `T&&` for `move that`.

### 5.4 Return values

```
"->" "void"                     // no return value (the default)
"->" type                       // a single value of that type
"->" "_"                        // single value, deduce with auto
"->" "forward" "_"              // single value, deduce with decltype(auto)
"->" "(" name ":" type, ... ")" // named return values
"->" "in" type                  // borrow of a parameter (elided source)
"->" "in" "(" param ")" type    // borrow with an explicit source
"->" "inout" type               // exclusive borrow of a parameter
```

A function may return a borrow, which is checked by the ownership pass: the
borrow must come from an `in`/`inout` parameter or from `this`, never from a
local. If there is more than one borrowable parameter the source must be named
explicitly with `in(param)`; a method's `this` is the implicit source. Returning
`inout` requires an exclusive source.

A single-expression body implicitly returns the expression's value (as
`decltype(auto)`) unless the return type is `void`. If the return type is
`void`, the single expression is evaluated and discarded.

Named return values are assigned in the body and returned at the end, and an
explicit `return;` returns them:

```cpp2
divide: (a: i32, b: i32) -> (quotient: i32, remainder: i32) = {
    quotient  = a / b;
    remainder = a % b;
}
```

A function with more than one named return value returns a generated struct
whose members have the declared names; a single named return value is a plain
value.

### 5.5 Functions are `[[nodiscard]]`

Every user function with a non-void return type is emitted with
`[[nodiscard]]`. Discarding a return value must be explicit:

```cpp2
g();       // error: return value ignored
_ = g();   // ok: explicitly discarded
```

### 5.6 `main`

`main` is the program entry point. It may have no parameters, or one parameter
of implicit type named `args`:

```cpp2
main: () -> i32 = { return 0; }
main: (args) -> i32 = { /* args.argc, args.argv, args[0], args.size(), ... */ }
```

`main` returns `int` in the generated C++; a `void` Epitaxy `main` is lowered to
`int main` and returns 0. The implicit parameter type is `epi::args_t`, which
behaves like a range of `std::string_view` over the program arguments and also
exposes raw `argc` and `argv` members.

### 5.7 Function calls and UFCS

A call `f(x)` calls a free function. A member-style call `x.f(a)` is a *unified
function call*: it calls a member function `f` if one exists, otherwise it calls
the free function `f(x, a)`:

```cpp2
v.ssize()     // member function if present, otherwise ssize(v) (found by ADL)
```

`x..f(a)` calls a member function only. A qualified name after `.` always calls
the named free function: `v.std::ranges::sort()` calls `std::ranges::sort(v)`.

Explicit `move` and `forward` may be written before an argument name:
`consume(move p)`.

### 5.8 Operator functions

Inside a type, `operator<op>: (this, that) -> R = …` declares a member operator
function:

```cpp2
vec2: type = {
    x: i32 = 0; y: i32 = 0;
    operator=: (out this, xv: i32, yv: i32) = { x = xv; y = yv; }
    operator+: (this, that) -> vec2 = vec2(x + that.x, y + that.y);
    operator==: (this, that) -> bool = x == that.x && y == that.y;
}
```

`operator=` is special: it defines construction, assignment and destruction
(see section 6.3). Free-function operators and `->`/`new`/`delete` overloads are
not supported in Epitaxy 0.2.

### 5.9 Function expressions (lambdas)

A function may be written without a name inside an expression:

```cpp2
std::ranges::for_each(v, :(x) = std::cout << x);
f := :(x: i32) -> i32 = { return x * 2; };
```

The body of a function expression is evaluated where the expression is written;
captures with `$` (section 8.9) capture by value at that point, and other outer
variables are captured by reference.

---

## 6. Types and objects

### 6.1 Type declarations

```cpp2
name: [template-parameters] type = { members }
name: [template-parameters] type == other-type;    // type alias
```

A type body contains data members, member functions, nested types, type
aliases, `using` declarations and preprocessor lines. Data members are private
by default; functions and nested types are public by default.

Data members cannot be borrows (`in T` / `inout T`); store owned values or
handle types instead. Data members are declared with the ordinary object syntax
and may have a default value:

```cpp2
point: type = {
    x: i32 = 0;
    y: i32 = 0;
    points: std::vector<point> = ();   // default value: empty
}
```

### 6.2 Base types

A base type is written as a member named `this`:

```cpp2
shape: type = { /* ... */ }
circle: type = {
    this: shape;
    radius: double = 1.0;
}
```

Inheritance is public. Base subobjects are initialized before data members
(C++ ordering); Epitaxy 0.2 does not implement Cpp2's interleaved
base/member default-initialization order.

### 6.3 `operator=` — construction, assignment and destruction

All value operations are spelled `operator=`:

| Declaration | Meaning |
| --- | --- |
| `operator=: (out this) = { … }` | default constructor |
| `operator=: (out this, that) = { … }` | copy constructor; the compiler also generates the move constructor and copy/move assignments unless they are written explicitly |
| `operator=: (out this, move that) = { … }` | move constructor (plus a generated move assignment) |
| `operator=: (out this, arg: T) = { … }` | converting constructor; a converting assignment is also generated |
| `operator=: (inout this, that) = { … }` | copy assignment (and move assignment) |
| `operator=: (move this) = { … }` | destructor |

Constructors declared with `out this` are `explicit` unless the `this`
parameter is marked `implicit`:

```cpp2
widget: type = {
    id: i32 = 0;

    operator=: (out this, id_value: i32) = { id = id_value; }

    operator=: (out this, that) = { }   // memberwise copy/move, plus assignments
}
```

In the body of an `operator=` function, the leading statements of the form
`member = value;` are *member initializers*: for constructors they are used in
the generated C++ member-initializer list, and members not mentioned are
initialized memberwise (from `that`, for copy/move operations) or from their
default value. Remaining statements run in the constructor body. In assignment
operators the same leading assignments are ordinary assignments, and unmentioned
members are assigned memberwise. Writing `member = _;` skips a member.

A constructor's base subobject is initialized from its own `this: Base` default
value; generated copy/move operations copy/move the base.

### 6.4 Metafunctions

A metafunction is written `@name` before `type`; the supported metafunctions in
Epitaxy 0.2 are:

```cpp2
color: @enum type = { red; green; blue; }        // safe enumeration type
point: @value type = { public x: i32 = 0; public y: i32 = 0; }
copy:  @copyable type = { public value: i32 = 0; }
shape: @interface type = { area: (this) -> double; }
```

* `@value` generates default/copy/move construction and assignment and a
  defaulted `<=>`.
* `@copyable` generates default/copy/move construction and assignment.
* `@interface` makes every member function `virtual` (pure virtual when it has
  no body) and gives the type a virtual destructor. Interface types cannot have
  data members.
* `@enum` declares a safe enumeration type:
  * `name: @enum type = { a; b; c; }` — enumerators are numbered from 1;
  * `name: @enum<i16> type = { … }` selects the underlying type;
  * `enumerator := value;` sets an explicit value;
  * enumerators are accessed as `name::enumerator` (and unqualified within the
    enum's own member functions);
  * comparison with enumerators and the type's own kind is provided, as are
    `to_string` and `std::ostream` output;
  * the default constructed value is the first enumerator;
  * member functions declared in the body may use `this`.

Other Cpp2 metafunctions (`@flag_enum`, `@union`, `@singleton`, `@regex`,
`@autodiff`, `@print`, `@ordered`, `@value`-family variants) are not implemented
in Epitaxy 0.2; using them is a compile-time error.

---

## 7. Statements

```
statement := block
           | declaration
           | expression ";"
           | "return" [expression] ";"
           | "if" expression block [ "else" ( block | if ) ]
           | "while" expression [ "next" expression ] block
           | "do" block [ "next" expression ] "while" expression ";"
           | "for" range [ "next" expression ] "do" "(" parameter ")" statement
           | "break" [IDENT] ";"
           | "continue" [IDENT] ";"
           | "throw" [ "(" expression ")" | expression ] ";"
           | "try" block { "catch" "(" [parameter | "_"] ")" block } [ "catch" "(" ")" ... ]
           | "assert" [ "<" expression, … ">" ] "(" expression ["," expression] ")" ";"
           | "using" …
           | preprocessor-line
           | label ":" while/do/for statement
           | "(" parameter ")" statement
           | "unsafe" block
```

### 7.1 Blocks

`{ ... }` introduces a new scope.

### 7.2 Packages of local objects

A parenthesised parameter list followed by a statement declares scoped objects
and executes the statement with them in scope (the "RAII block" form):

```cpp2
(x: std::lock_guard = mut) {
    // x is in scope here and destroyed at the end
}
```

### 7.3 Conditionals

```cpp2
if x > 0 {
    // ...
}
else if x < 0 {
    // ...
}
else {
    // ...
}
```

Braces are required around `if`/`else` bodies. There is no `?:` conditional
operator in Epitaxy; use `inspect` or `if`.

### 7.4 Loops

```cpp2
while i < n next i++ { }

do { } next i-- while i > 0;

for words do (word) { }                 // range-for over a container
for 1 ..= 10 do (i) { }                 // closed range
for begin ..< end do (it) { }           // half-open range over any incrementable type
for (0, 1, 2) do (e) { }                // iterate over a fixed list
for v next i++ do (inout e) { }         // with a per-iteration `next` statement
```

The loop variable is a parameter: `(word)` is read-only, `(inout word)` can
modify the elements, `(copy word)` copies each element.

A loop may be named, and `break`/`continue` may name a loop:

```cpp2
outer: while i < m next i++ {
    inner: while j < n next j++ {
        if something() { continue inner; }
        if something_else() { break outer; }
    }
}
```

### 7.5 Exceptions

```cpp2
try {
    throw( "boom" );
}
catch (message: * const char) {
    std::cout << message;
}
catch (code: i32) {
    // ...
}
catch (_) {
    // catches anything
}
```

`throw(expr)` throws a value; `throw expr` is also accepted. `catch (_)` is
lowered to `catch (...)`.

### 7.6 `unsafe` blocks

`unsafe { ... }` executes a block in an unsafe context. Inside it (and inside
functions declared `unsafe`), raw pointers may be created and dereferenced; see
section 11.

### 7.7 Discarding values

`_ = expression;` evaluates an expression and explicitly discards its value.
This is required for `[[nodiscard]]` functions.

---

## 8. Expressions

### 8.1 Precedence

From lowest to highest:

| Level | Operators |
| --- | --- |
| assignment | `=` `+=` `-=` `*=` `/=` `%=` `<<=` `>>=` `&=` `^=` `\|=` (right associative) |
| logical or | `\|\|` |
| logical and | `&&` |
| bitwise or | `\|` |
| bitwise xor | `^` |
| bitwise and | `&` |
| equality | `==` `!=` |
| relational | `<` `>` `<=` `>=` (may be chained) |
| type/value queries | `is`, `as` |
| shift | `<<` `>>` |
| additive | `+` `-` |
| multiplicative | `*` `/` `%` |
| prefix | `!` `+` `-` |
| postfix | `.` `..` `*` `&` `~` `++` `--` `(` `)` `[` `]` `..<` `..=` `$` `...` |

### 8.2 Postfix unary operators

Unlike C++, dereference, address-of, bitwise complement and increment/decrement
are postfix:

| Epitaxy | C++ |
| --- | --- |
| `p*` | `*p` |
| `obj&` | `&obj` (via `std::addressof`) |
| `value~` | `~value` |
| `iter++` | `++iter` (in-place; take a copy first if needed) |
| `iter--` | `--iter` |

`a * b` and `a & b` are binary operators; the postfix forms are recognised when
the next token cannot begin an operand (`p*`, `p*.member`, `p*[i]`).

### 8.3 Borrows, raw pointers and `unsafe`

`place&` is the address/borrow expression. Its meaning depends on the context:

* initialising a local of borrow type (`r: in T = place&;`), or returning from a
  borrow-returning function (`return place&;`) creates a borrow;
* inside an `unsafe` block or `unsafe` function it creates a raw pointer;
* anywhere else it is an error.

Raw-pointer dereference (`p*` where `p: *T`) requires an unsafe context. An
expression can be evaluated in an unsafe context with `unsafe(expr)`.

At a call site, `out x` marks an argument as written by the callee (the
parameter is `out`), and `move x` transfers ownership; plain `x` is a shared or
exclusive borrow according to the parameter kind.

### 8.4 Calls and member access

* `f(x)` — free function call.
* `x.f(a)` — UFCS: member function if available, otherwise `f(x, a)`.
* `x..f(a)` — member function only.
* `x.f` — data member access.
* `x[i]` — subscript, bounds checked by default.
* `f<T>(x)` — call of a function template; `name<T>` without a call is a
  template-id expression.

### 8.5 `is` — type and value queries

`x is C` evaluates to `bool`:

* if `C` is a type: true when the type of `x` is exactly `C` (after removing
  references and top-level const), or, for `std::any` / `std::variant` /
  `std::optional`, a dynamic query;
* if `C` is `*D` and `x` is a pointer: true when `x` points to a `D` or a class
  derived from `D` (polymorphic dynamic cast);
* if `C` is a literal or a parenthesised expression: true when `x` compares
  equal to that value (`std::any` and `std::variant` are supported);
* `x is _` is always true (the catch-all used by `inspect`).

Template-name and concept queries (`x is std::vector`, `x is std::integral`) are
not supported in Epitaxy 0.2.

### 8.6 `as` — safe conversions

`x as T` performs a safe conversion, lowered to a helper that uses
list-initialisation so that narrowing conversions are C++ compile-time errors:

```cpp2
a := "text" as std::string;
n := i as i16;                    // error: possible narrowing
pi := unchecked_narrow<i16>(i);   // explicit opt-out
```

`as` also supports `std::any` (`any_cast`), `std::variant` (`std::get`) and
`std::optional` (`.value()`), and dynamic casts between pointer types.

### 8.7 `inspect` — pattern matching

```cpp2
label := inspect value -> std::string {
    is 0           = "zero";
    is 42          = "the answer";
    is int         = "an integer";
    is std::string = value as std::string;
    is _           = "something else";
};
```

The inspected expression is evaluated once; alternatives are tried in order with
`is` semantics; the first matching alternative's expression is the value of the
whole `inspect`. The result type is given after `->`. The last alternative must
be the catch-all `is _`.

### 8.8 Ranges

`first ..< last` is a half-open range and `first ..= last` is a closed range.
They iterate from `first` by `++` until `last` (excluded or included):

```cpp2
for 1 ..= 100 do (i) { }
for it ..< v.end() do (elem) { std::cout << elem*; }

sum := (1 ..= 100).sum();
ok  := (0 ..< v.ssize()).contains(index);
```

Numeric ranges also provide `.sum()`, `.contains()` and `.size()`. For `..=`
the last value must be valid to increment once.

### 8.9 `new`, `unique.new` and `shared.new`

```cpp2
owned := new<std::vector<i32>>(1, 2, 3);   // std::make_unique
shared_owner := shared.new<widget>();      // std::make_shared
unique_owner := unique.new<widget>();      // std::make_unique
```

### 8.10 `$` — captures

Suffix `$` captures the value of an expression at the point where it is written:

* inside a string literal, `(expr)$` interpolates the value (section 2.7);
* inside a postcondition, `expr$` captures the value on function entry;
* inside a function expression body, `expr$` is captured by value when the
  function expression is created;
* elsewhere it evaluates the expression at that point.

### 8.11 Fold expressions and packs

C++17 fold expressions and pack expansions are supported:

```cpp2
(out << ... << args);
(... << args);
sizeof...(Args)
f(args...)
```

### 8.12 Automatic move from last use

When an object is used for the last time, Epitaxy moves (or forwards) from it
automatically. The analysis is deliberately conservative: a use is only a
last use when it is the last lexical use in the function, it is not inside a
loop, the variable is not captured by a function expression and its address has
not been taken. Explicit `move`/`forward` is always available and is rarely
needed.

---

## 9. Contracts

### 9.1 Function contracts

```cpp2
divide: (a: i32, b: i32) -> i32
    pre(b != 0, "division by zero")
    post(a / b == result$)             // postcondition; `$` captures on entry
= {
    return a / b;
}
```

* Preconditions are evaluated before the body.
* Postconditions are evaluated immediately before each normal return; their `$`
  captures are evaluated on function entry. Postconditions are not evaluated
  when the function exits by throwing.
* Each contract may carry groups and predicates:
  `pre<bounds_safety>(condition, "message")`,
  `assert<audit, is_checked_build, checking_enabled>(condition)`.

A contract is checked only when its predicates are true and its group is active.
A violation calls `group.report_violation(message)`.

### 9.2 Assertions

`assert(condition, "message")` inside a body is an assertion statement, checked
when control passes through it. `assert<group>(condition)` selects a group.

### 9.3 Contract groups

The built-in groups are `default`, `type_safety`, `bounds_safety`,
`null_safety` and `testing` (lowered to `epi::default_`, `epi::type_safety`,
`epi::bounds_safety`, `epi::null_safety` and `epi::testing`). Any other name is
used as an expression: a group object must provide `is_active()` and
`report_violation(std::string)`.

```cpp2
handler: (msg: * const char) = { std::cout << msg; }

main: () = {
    epi::default_.set_handler(handler&);
    assert(false, "custom handling");
}
```

`epi::contract_group` is the standard group type; its constructor installs a
default report-and-terminate handler, `set_handler` replaces it and
`set_handler(null)` deactivates the group.

---

## 10. Safety

The following checks are on by default and can be disabled per translation unit
with the CLI flags shown, or avoided at a specific operation with the
`unchecked_*` functions:

| Check | Default | CLI opt-out | Explicit opt-out |
| --- | --- | --- | --- |
| subscripts are in bounds (containers with a size and random-access iterators) | on | `-no-subscript-checks` | `unchecked_subscript(c, i)` |
| raw pointer dereferences are non-null | on | `-no-null-checks` | `unchecked_dereference(p)` |
| integer division/remainder by zero | on | `-no-div-zero-checks` | `unchecked_div(a, b)`, `unchecked_mod(a, b)` |
| relational comparisons between mixed-sign integers | on (compile-time error) | `-no-comparison-checks` | `unchecked_cmp_less(a, b)`, `unchecked_cmp_equal(a, b)` |
| ownership/borrow/move/unsafe rules (section 11) | on (compile-time error) | `-no-ownership-checks` | explicit `unsafe` boundary, explicit `move` |

Checked operations call the `type_safety`, `bounds_safety` or `null_safety`
contract group when they fail.

Narrowing conversions and unsafe casts are errors by default; use
`unchecked_narrow<T>(x)` and `unchecked_cast<T>(x)` when you mean them.

---

## 11. Ownership and borrow checking

Epitaxy adds an ownership model on top of the Cpp2 syntax. The checker runs
after parsing and before lowering; violations are compile-time errors.

### 11.1 Ownership

* Every value is owned by exactly one place (a local variable, parameter or
  temporary).
* `unique<T>` values are **move-only**: copying them is an error. Ownership is
  transferred with `(move x)` or implicitly on the last use / return.
* `shared<T>` values are shared owning handles: they are copyable and all copies
  share the object.
* Moving a value invalidates the source: any later use of the source is an
  error until it is assigned again.

```cpp2
a: unique<i32> = new<i32>(1);
b: unique<i32> = a;          // error: unique<T> is move-only
c: unique<i32> = (move a);   // ok: ownership transferred
_ = a*;                      // error: use of moved value 'a'
```

### 11.2 Borrowing

A borrow is a non-owning view. Borrows are created from places (a variable,
parameter, `this`, or a member/deref chain of one):

* `in T` / `in` parameters: any number of shared borrows may coexist; they may
  read but not write.
* `inout T` / `inout` parameters: an exclusive borrow; nothing else may borrow
  the same place while it is live, and the place may not be read, written or
  moved directly.
* `out` parameters are exclusive borrows that must be assigned before the
  function returns.
* A borrow may not outlive its owner. A local borrow lives from its declaration
  to the end of its scope (lexical lifetimes; no NLL).
* A borrow cannot be moved, captured by a function expression, or stored in a
  data member.
* Borrowing a temporary is an error.

```cpp2
x := 10;
r: in i32 = x&;        // shared borrow
m: inout i32 = x&;     // error: x is already borrowed (shared)
```

### 11.3 Returning borrows

A function may return a borrow of a parameter or of `this` (never of a local,
and never of an owned parameter). With several borrowable parameters, the source
must be named: `-> in(text) T`. A method's `this` is the implicit source for
`-> in T`.

```cpp2
first: (in text: std::string) -> in std::string = text&;

value_box: type = {
    value: i32 = 0;
    get: (this) -> in(this) i32 = this.value&;
}
```

### 11.4 Calls and aliasing

For a call to an Epitaxy-declared function, argument borrows are checked:
passing the same place both exclusively and in any other way (or moving it) is
an error, and `inout`/`out` arguments must be lvalues.

```cpp2
swap: (inout a: i32, inout b: i32) = { /* ... */ }
swap(x, x);    // error: aliased by two conflicting borrows
```

### 11.5 The `unsafe` boundary

Raw pointers (`*T`) remain available for C++ interop, but creating one from a
place and dereferencing one require an unsafe context: an `unsafe { ... }`
block, an `unsafe(expr)` expression, or an `unsafe` function:

```cpp2
read_i32: unsafe (p: *i32) -> i32 = { return p*; }

raw: *i32 = unsafe(x&);
value := unsafe(read_i32(raw));
```

Calling an `unsafe` function outside an unsafe context is an error.

### 11.6 Scope of the analysis

The checker is deliberately conservative and sound for the constructs it
models:

* lexical borrow lifetimes, no NLL and no lifetime polymorphism beyond
  `-> in(param) T`;
* reads of a place while an exclusive borrow is live are rejected; writes/moves
  while any borrow is live are rejected; reading a place while only shared
  borrows are live is allowed;
* moves inside a loop of a variable declared outside the loop are rejected;
* function expressions may not capture borrows, moved values or move-only
  values;
* C++ library calls and unmodelled declarations are trusted, and their argument
  borrow relationships are not checked (except explicit `move` arguments,
  which are still tracked);
* there is no data-race/`Send`/`Sync` analysis;
* `unique<T>` and `shared<T>` are the only tracked owning handle types; other
  C++ types are treated as copyable values.

The checks can be disabled for a translation unit with `-no-ownership-checks`.

## 12. C++ interoperability and lowering

* Epitaxy is a source-to-source language: a `.ep` file becomes one `.cpp` file
  containing `#include "epi_support.hpp"` plus the lowered program.
* All preprocessor lines in the file are emitted at the top of the generated
  file, in source order.
* C++ names may be used directly: `std::vector`, `std::ranges::sort`,
  `std::cout`, `std::any`, and so on. C++20 CTAD, `[[nodiscard]]`, concepts and
  ranges are available.
* Function parameters of scalar, enum and pointer type are passed by const
  value for `in`; larger types are passed by `const&`.
* `const` and `*` type qualifiers are lowered to C++ const and pointer types;
  borrow types are lowered to references and `unique<T>`/`shared<T>` to
  `std::unique_ptr`/`std::shared_ptr`.
* `_` return types lower to `auto` (or `decltype(auto)` for `forward _`).
* Identifiers starting with `epi__` are reserved for generated declarations.
  The `::epi` namespace is reserved for the Epitaxy support library.
* Multi-file programs are not supported in 0.2: one translation unit at a time,
  with C++ headers for interop.

The support header `epi_support.hpp` (MIT licensed, part of Epitaxy) provides
the contract groups, UFCS macro, checked operations, `is`/`as` helpers, range
objects, string interpolation helpers and `epi::args_t`.

---

## 13. Diagnostics

The compiler reports `file:line:column: error: message` and stops at the first
error. Errors detected by the C++ compiler in generated code are reported by the
underlying C++ compiler.

---

## 14. Compatibility

See [`COMPATIBILITY.md`](COMPATIBILITY.md) for the exact status of each Cpp2
feature in Epitaxy 0.2 and for deliberate semantic differences.
