// Epitaxy runtime support header
//
// Copyright (c) 2025 The Epitaxy contributors
// SPDX-License-Identifier: MIT
//
// This header is part of the Epitaxy project and is distributed under the MIT
// license.  It is included by every C++ file that the Epitaxy compiler
// generates.  It contains:
//
//   * the fixed-width type aliases of the Epitaxy prelude,
//   * contract groups and contract violation reporting,
//   * the UFCS (unified function call syntax) macro,
//   * string interpolation helpers,
//   * default-on safety checks (subscript, null dereference, division by
//     zero, mixed-sign comparison) and their `unchecked_*` opt-outs,
//   * `is` / `as` queries,
//   * range objects for `..<` and `..=`,
//   * `epi::args_t`, the type of `main`'s `args` parameter.
//
// This file is an independent implementation.  It does not contain or derive
// from cppfront code (cppfront is Apache-2.0-with-LLVM-exception); Epitaxy is
// MIT licensed.

#ifndef EPITAXY_SUPPORT_HPP_INCLUDED
#define EPITAXY_SUPPORT_HPP_INCLUDED

#include <any>
#include <compare>
#include <concepts>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <format>
#include <iostream>
#include <iterator>
#include <limits>
#include <memory>
#include <optional>
#include <ranges>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <type_traits>
#include <utility>
#include <variant>

namespace epi {

// ---------------------------------------------------------------------------
// Prelude type aliases
// ---------------------------------------------------------------------------

using i8 = std::int8_t;
using i16 = std::int16_t;
using i32 = std::int32_t;
using i64 = std::int64_t;
using u8 = std::uint8_t;
using u16 = std::uint16_t;
using u32 = std::uint32_t;
using u64 = std::uint64_t;

using ushort = unsigned short;
using uint = unsigned int;
using ulong = unsigned long;
using longlong = long long;
using ulonglong = unsigned long long;
using longdouble = long double;

// For C++ interop only; prefer i8/u8.
using _schar = signed char;
using _uchar = unsigned char;

// ---------------------------------------------------------------------------
// Parameter passing helpers
// ---------------------------------------------------------------------------

// `in` parameters are read-only and cheap to pass by value when the type is
// trivially small; otherwise they are passed by const reference.
template <class T>
using in = std::conditional_t<std::is_scalar_v<T> || std::is_enum_v<T>, T const, T const&>;

template <class T>
using in_ref = T const&;

// ---------------------------------------------------------------------------
// Contract groups
// ---------------------------------------------------------------------------

using contract_handler = void (*)(char const* message);

inline void report_and_terminate(char const* message) noexcept {
    std::cerr << "Epitaxy contract violation: " << (message ? message : "(no message)") << "\n";
    std::terminate();
}

class contract_group {
public:
    constexpr contract_group() noexcept = default;

    void set_handler(contract_handler handler) noexcept { handler_ = handler; }
    contract_handler get_handler() const noexcept { return handler_; }

    // A group is active when a handler is installed.  All predefined groups
    // start active with the default reporting handler.
    bool is_active() const noexcept { return handler_ != nullptr; }

    void report_violation(std::string const& message) const noexcept {
        if (handler_) {
            handler_(message.c_str());
        } else {
            std::terminate();
        }
    }

    template <class Condition>
    void enforce(Condition&& condition, std::string const& message) const {
        if (!static_cast<bool>(condition)) {
            report_violation(message);
        }
    }

private:
    contract_handler handler_ = &report_and_terminate;
};

inline contract_group default_{};
inline contract_group type_safety{};
inline contract_group bounds_safety{};
inline contract_group null_safety{};
inline contract_group testing{};

// The compiler lowers pre/post/assert contracts to this helper.
inline void contract_check(bool condition, contract_group const& group,
                           std::string const& message) {
    if (!condition && group.is_active()) {
        group.report_violation(message);
    }
}

// ---------------------------------------------------------------------------
// to_string / string interpolation
// ---------------------------------------------------------------------------

inline std::string to_string(std::string const& value) { return value; }
inline std::string to_string(std::string_view value) { return std::string{value}; }
inline std::string to_string(char const* value) { return std::string{value ? value : ""}; }
inline std::string to_string(char value) { return std::string(1, value); }
inline std::string to_string(bool value) { return value ? "true" : "false"; }
inline std::string to_string(std::nullptr_t) { return "nullptr"; }

template <class T>
std::string to_string(T const& value) {
    if constexpr (requires(std::ostringstream& os, T const& v) { os << v; }) {
        std::ostringstream os;
        os << value;
        return os.str();
    } else if constexpr (requires { std::format("{}", value); }) {
        return std::format("{}", value);
    } else {
        static_assert(sizeof(T) == 0, "Epitaxy: this type cannot be formatted; add an operator<< for it");
    }
}

inline void append_to(std::string& out, std::string const& value) { out += value; }
inline void append_to(std::string& out, std::string_view value) { out += value; }
inline void append_to(std::string& out, char const* value) { out += value ? value : ""; }
inline void append_to(std::string& out, char value) { out += value; }

template <class T>
void append_to(std::string& out, T const& value) {
    out += to_string(value);
}

// Concatenate literal fragments and interpolated values into a std::string.
inline std::string concat() { return {}; }

template <class First, class... Rest>
std::string concat(First&& first, Rest&&... rest) {
    std::string result;
    append_to(result, first);
    (append_to(result, rest), ...);
    return result;
}

// Format-spec interpolation, e.g. "height is (h:.1f)$": Epitaxy lowers this to
// std::format with a compile-time-checked format string.
template <class... Args>
std::string format(char const* fmt, Args&&... args) {
    if constexpr (sizeof...(Args) == 0) {
        return fmt;
    } else {
        return std::format(fmt, std::forward<Args>(args)...);
    }
}

// ---------------------------------------------------------------------------
// UFCS: unified function call syntax
// ---------------------------------------------------------------------------

// `x.f(args)` calls the member f if it exists, otherwise f(x, args).
// The name is textual, so this has to be a macro.
#define EPI_UFCS(FNAME)                                                              \
    [](auto&& epi_ufcs_obj, auto&&... epi_ufcs_args) -> decltype(auto) {             \
        if constexpr (requires {                                                     \
                          std::forward<decltype(epi_ufcs_obj)>(epi_ufcs_obj)         \
                              .FNAME(std::forward<decltype(epi_ufcs_args)>(          \
                                  epi_ufcs_args)...);                                \
                      }) {                                                           \
            return std::forward<decltype(epi_ufcs_obj)>(epi_ufcs_obj)                \
                .FNAME(std::forward<decltype(epi_ufcs_args)>(epi_ufcs_args)...);     \
        } else {                                                                     \
            return FNAME(std::forward<decltype(epi_ufcs_obj)>(epi_ufcs_obj),         \
                         std::forward<decltype(epi_ufcs_args)>(epi_ufcs_args)...);   \
        }                                                                            \
    }

// ---------------------------------------------------------------------------
// Safety: checked / unchecked operations
// ---------------------------------------------------------------------------

namespace detail {

template <class I, class N>
constexpr bool index_in_range(I index, N size) {
    using UI = std::remove_cvref_t<I>;
    using UN = std::remove_cvref_t<N>;
    if constexpr (std::is_signed_v<UI> && std::is_unsigned_v<UN>) {
        if (index < 0) {
            return false;
        }
        return static_cast<std::make_unsigned_t<UI>>(index) < static_cast<UN>(size);
    } else if constexpr (std::is_unsigned_v<UI> && std::is_signed_v<UN>) {
        if (size < 0) {
            return false;
        }
        return static_cast<UI>(index) < static_cast<std::make_unsigned_t<UN>>(size);
    } else {
        return index < size;
    }
}

template <class A, class B>
constexpr void check_sign() {
    using UA = std::remove_cvref_t<A>;
    using UB = std::remove_cvref_t<B>;
    if constexpr (std::is_integral_v<UA> && std::is_integral_v<UB>) {
        if constexpr (!std::is_same_v<UA, bool> && !std::is_same_v<UB, bool>) {
            static_assert(
                std::is_signed_v<UA> == std::is_signed_v<UB>,
                "Epitaxy: this comparison mixes signed and unsigned integers, which is unsafe. "
                "Convert one operand explicitly, or use an unchecked_* comparison.");
        }
    }
}

template <class T>
inline constexpr bool dependent_false_v = false;

}  // namespace detail

// Checked subscript access.  Bounds are checked when the container exposes a
// size and random-access iterators; other subscripts pass through unchanged.
template <class Container, class Index>
constexpr decltype(auto) subscript(Container&& container, Index&& index) {
    if constexpr (requires { std::size(container); } && requires { std::begin(container); }) {
        if constexpr (std::random_access_iterator<decltype(std::begin(container))>) {
            auto const size = std::size(container);
            if (!detail::index_in_range(index, size)) {
                bounds_safety.report_violation("subscript index is out of bounds");
                std::abort();  // never continue into an out-of-bounds access
            }
        }
    }
    return std::forward<Container>(container)[std::forward<Index>(index)];
}

template <class Container, class Index>
constexpr decltype(auto) unchecked_subscript(Container&& container, Index&& index) {
    return std::forward<Container>(container)[std::forward<Index>(index)];
}

// Checked dereference: raw pointers are null checked.
template <class Ptr>
constexpr decltype(auto) deref(Ptr&& pointer) {
    if constexpr (std::is_pointer_v<std::remove_cvref_t<Ptr>>) {
        if (pointer == nullptr) {
            null_safety.report_violation("null pointer dereference");
            std::abort();  // never continue into a null dereference
        }
    }
    return *std::forward<Ptr>(pointer);
}

template <class Ptr>
constexpr decltype(auto) unchecked_dereference(Ptr&& pointer) {
    return *std::forward<Ptr>(pointer);
}

template <class Ptr>
constexpr auto address(Ptr&& object) noexcept {
    return std::addressof(object);
}

// Checked division and remainder: integer division by zero is a contract
// violation (and a compile-time error in constant evaluation).
template <class A, class B>
constexpr auto div(A&& a, B&& b) {
    using UA = std::remove_cvref_t<A>;
    using UB = std::remove_cvref_t<B>;
    if constexpr (std::is_integral_v<UA> && std::is_integral_v<UB>) {
        if (b == 0) {
            type_safety.report_violation("integer division by zero");
            std::abort();
        }
        if constexpr (std::is_signed_v<UA> && std::is_signed_v<UB>) {
            if (a == (std::numeric_limits<UA>::min)() && b == -1) {
                type_safety.report_violation("integer division overflow");
                std::abort();
            }
        }
    }
    return std::forward<A>(a) / std::forward<B>(b);
}

template <class A, class B>
constexpr auto mod(A&& a, B&& b) {
    using UA = std::remove_cvref_t<A>;
    using UB = std::remove_cvref_t<B>;
    if constexpr (std::is_integral_v<UA> && std::is_integral_v<UB>) {
        if (b == 0) {
            type_safety.report_violation("integer remainder by zero");
            std::abort();
        }
        if constexpr (std::is_signed_v<UA> && std::is_signed_v<UB>) {
            if (a == (std::numeric_limits<UA>::min)() && b == -1) {
                type_safety.report_violation("integer remainder overflow");
                std::abort();
            }
        }
    }
    return std::forward<A>(a) % std::forward<B>(b);
}

template <class A, class B>
constexpr auto unchecked_div(A&& a, B&& b) {
    return std::forward<A>(a) / std::forward<B>(b);
}

template <class A, class B>
constexpr auto unchecked_mod(A&& a, B&& b) {
    return std::forward<A>(a) % std::forward<B>(b);
}

// Comparisons: mixed-sign integer comparisons are rejected at compile time.
template <class A, class B>
constexpr auto cmp_eq(A&& a, B&& b) {
    detail::check_sign<A, B>();
    return std::forward<A>(a) == std::forward<B>(b);
}

template <class A, class B>
constexpr auto cmp_ne(A&& a, B&& b) {
    detail::check_sign<A, B>();
    return std::forward<A>(a) != std::forward<B>(b);
}

template <class A, class B>
constexpr auto cmp_lt(A&& a, B&& b) {
    detail::check_sign<A, B>();
    return std::forward<A>(a) < std::forward<B>(b);
}

template <class A, class B>
constexpr auto cmp_le(A&& a, B&& b) {
    detail::check_sign<A, B>();
    return std::forward<A>(a) <= std::forward<B>(b);
}

template <class A, class B>
constexpr auto cmp_gt(A&& a, B&& b) {
    detail::check_sign<A, B>();
    return std::forward<A>(a) > std::forward<B>(b);
}

template <class A, class B>
constexpr auto cmp_ge(A&& a, B&& b) {
    detail::check_sign<A, B>();
    return std::forward<A>(a) >= std::forward<B>(b);
}

template <class A, class B>
constexpr auto unchecked_cmp_less(A&& a, B&& b) {
    return std::forward<A>(a) < std::forward<B>(b);
}

template <class A, class B>
constexpr auto unchecked_cmp_equal(A&& a, B&& b) {
    return std::forward<A>(a) == std::forward<B>(b);
}

// Explicitly type-unsafe casts: always written explicitly in Epitaxy.
template <class To, class From>
constexpr To unchecked_narrow(From&& from) {
    return static_cast<To>(std::forward<From>(from));
}

template <class To, class From>
constexpr To unchecked_cast(From&& from) {
    using UF = std::remove_cvref_t<From>;
    if constexpr (std::is_pointer_v<To> && std::is_pointer_v<UF> &&
                  !std::is_convertible_v<UF, To>) {
        return reinterpret_cast<To>(from);
    } else {
        return static_cast<To>(std::forward<From>(from));
    }
}

// ---------------------------------------------------------------------------
// is / as queries
// ---------------------------------------------------------------------------

namespace detail {

template <class T>
struct is_variant : std::false_type {};
template <class... Ts>
struct is_variant<std::variant<Ts...>> : std::true_type {};

template <class T>
struct is_optional : std::false_type {};
template <class T>
struct is_optional<std::optional<T>> : std::true_type {};

template <class T>
struct is_any : std::false_type {};
template <>
struct is_any<std::any> : std::true_type {};

}  // namespace detail

// Static type query, plus dynamic queries for variant/optional/any.
template <class T, class X>
constexpr bool is(X&& value) {
    using U = std::remove_cvref_t<X>;
    if constexpr (detail::is_variant<U>::value) {
        return std::holds_alternative<T>(value);
    } else if constexpr (detail::is_optional<U>::value) {
        if constexpr (std::is_same_v<T, typename U::value_type>) {
            return value.has_value();
        } else {
            return false;
        }
    } else if constexpr (detail::is_any<U>::value) {
        return value.type() == typeid(T);
    } else {
        return std::is_same_v<U, T>;
    }
}

// Value query (equality with a value), with support for any/variant.
template <class X, class V>
constexpr bool is_value(X&& value, V&& candidate) {
    using U = std::remove_cvref_t<X>;
    using VV = std::remove_cvref_t<V>;
    if constexpr (detail::is_any<U>::value) {
        auto* stored = std::any_cast<VV>(&value);
        return stored != nullptr && *stored == candidate;
    } else if constexpr (detail::is_variant<U>::value) {
        return std::visit(
            [&](auto const& alternative) -> bool {
                if constexpr (std::equality_comparable_with<decltype(alternative), VV>) {
                    return alternative == candidate;
                } else {
                    return false;
                }
            },
            value);
    } else if constexpr (std::equality_comparable_with<U, VV>) {
        return value == candidate;
    } else {
        return false;
    }
}

// Dynamic type query for polymorphic pointers: `p is *D`.
template <class D, class P>
constexpr bool is_polymorphic(P&& pointer) {
    using UP = std::remove_cvref_t<P>;
    if constexpr (std::is_pointer_v<UP> || requires { pointer.get(); }) {
        auto raw = [&]() {
            if constexpr (std::is_pointer_v<UP>) {
                return pointer;
            } else {
                return pointer.get();
            }
        }();
        using Raw = std::remove_pointer_t<decltype(raw)>;
        if constexpr (std::is_polymorphic_v<std::remove_cv_t<Raw>>) {
            return dynamic_cast<D*>(raw) != nullptr;
        } else {
            return false;
        }
    } else {
        return false;
    }
}

// Safe cast: conversions use list-initialisation, so narrowing conversions are
// rejected at compile time by C++ itself.
template <class T, class X>
decltype(auto) as(X&& value) {
    using U = std::remove_cvref_t<X>;
    if constexpr (detail::is_variant<U>::value) {
        return std::get<T>(std::forward<X>(value));
    } else if constexpr (detail::is_optional<U>::value) {
        return T{*std::forward<X>(value)};
    } else if constexpr (detail::is_any<U>::value) {
        return std::any_cast<T>(std::forward<X>(value));
    } else if constexpr (std::is_pointer_v<T> && std::is_pointer_v<U>) {
        return dynamic_cast<T>(value);
    } else {
        return T{std::forward<X>(value)};
    }
}

// ---------------------------------------------------------------------------
// Checked arithmetic (strict safety mode)
// ---------------------------------------------------------------------------
//
// In strict safety mode Epitaxy lowers +, -, *, +=, -=, *=, ++, -- and shifts
// to these helpers.  An overflow is reported through the type_safety contract
// group and then aborts: unlike ordinary contracts it cannot be deactivated,
// so strict-mode code never depends on a custom handler returning.

[[noreturn]] inline void arithmetic_overflow() noexcept {
    type_safety.report_violation("integer arithmetic overflow");
    std::abort();
}

template <class A, class B>
constexpr decltype(auto) checked_add(A&& a, B&& b) {
    using UA = std::remove_cvref_t<A>;
    using UB = std::remove_cvref_t<B>;
    if constexpr (std::is_integral_v<UA> && std::is_integral_v<UB> &&
                  !std::is_same_v<UA, bool> && !std::is_same_v<UB, bool>) {
        UA av = a;
        UB bv = b;
        using R = decltype(av + bv);
        R result{};
        if (__builtin_add_overflow(av, bv, &result)) {
            arithmetic_overflow();
        }
        return result;
    } else {
        return std::forward<A>(a) + std::forward<B>(b);
    }
}

template <class A, class B>
constexpr decltype(auto) checked_sub(A&& a, B&& b) {
    using UA = std::remove_cvref_t<A>;
    using UB = std::remove_cvref_t<B>;
    if constexpr (std::is_integral_v<UA> && std::is_integral_v<UB> &&
                  !std::is_same_v<UA, bool> && !std::is_same_v<UB, bool>) {
        UA av = a;
        UB bv = b;
        using R = decltype(av - bv);
        R result{};
        if (__builtin_sub_overflow(av, bv, &result)) {
            arithmetic_overflow();
        }
        return result;
    } else {
        return std::forward<A>(a) - std::forward<B>(b);
    }
}

template <class A, class B>
constexpr decltype(auto) checked_mul(A&& a, B&& b) {
    using UA = std::remove_cvref_t<A>;
    using UB = std::remove_cvref_t<B>;
    if constexpr (std::is_integral_v<UA> && std::is_integral_v<UB> &&
                  !std::is_same_v<UA, bool> && !std::is_same_v<UB, bool>) {
        UA av = a;
        UB bv = b;
        using R = decltype(av * bv);
        R result{};
        if (__builtin_mul_overflow(av, bv, &result)) {
            arithmetic_overflow();
        }
        return result;
    } else {
        return std::forward<A>(a) * std::forward<B>(b);
    }
}

template <class A>
constexpr decltype(auto) checked_neg(A&& a) {
    using UA = std::remove_cvref_t<A>;
    if constexpr (std::is_integral_v<UA> && !std::is_same_v<UA, bool>) {
        UA av = a;
        UA result{};
        if (__builtin_sub_overflow(UA{}, av, &result)) {
            arithmetic_overflow();
        }
        return result;
    } else {
        return -std::forward<A>(a);
    }
}

template <class A, class B>
constexpr A& checked_add_assign(A& lhs, B&& rhs) {
    if constexpr (std::is_integral_v<A> && std::is_integral_v<std::remove_cvref_t<B>> &&
                  !std::is_same_v<A, bool> && !std::is_same_v<std::remove_cvref_t<B>, bool>) {
        if (__builtin_add_overflow(lhs, rhs, &lhs)) {
            arithmetic_overflow();
        }
    } else {
        lhs += std::forward<B>(rhs);
    }
    return lhs;
}

template <class A, class B>
constexpr A& checked_sub_assign(A& lhs, B&& rhs) {
    if constexpr (std::is_integral_v<A> && std::is_integral_v<std::remove_cvref_t<B>> &&
                  !std::is_same_v<A, bool> && !std::is_same_v<std::remove_cvref_t<B>, bool>) {
        if (__builtin_sub_overflow(lhs, rhs, &lhs)) {
            arithmetic_overflow();
        }
    } else {
        lhs -= std::forward<B>(rhs);
    }
    return lhs;
}

template <class A, class B>
constexpr A& checked_mul_assign(A& lhs, B&& rhs) {
    if constexpr (std::is_integral_v<A> && std::is_integral_v<std::remove_cvref_t<B>> &&
                  !std::is_same_v<A, bool> && !std::is_same_v<std::remove_cvref_t<B>, bool>) {
        if (__builtin_mul_overflow(lhs, rhs, &lhs)) {
            arithmetic_overflow();
        }
    } else {
        lhs *= std::forward<B>(rhs);
    }
    return lhs;
}

template <class A, class B>
constexpr A& checked_div_assign(A& lhs, B&& rhs) {
    lhs = static_cast<A>(div(lhs, std::forward<B>(rhs)));
    return lhs;
}

template <class A, class B>
constexpr A& checked_mod_assign(A& lhs, B&& rhs) {
    lhs = static_cast<A>(mod(lhs, std::forward<B>(rhs)));
    return lhs;
}

template <class A>
constexpr A& checked_inc(A& value) {
    if constexpr (std::is_integral_v<A> && !std::is_same_v<A, bool>) {
        if (__builtin_add_overflow(value, A{1}, &value)) {
            arithmetic_overflow();
        }
        return value;
    } else {
        return ++value;
    }
}

template <class A>
constexpr A& checked_dec(A& value) {
    if constexpr (std::is_integral_v<A> && !std::is_same_v<A, bool>) {
        if (__builtin_sub_overflow(value, A{1}, &value)) {
            arithmetic_overflow();
        }
        return value;
    } else {
        return --value;
    }
}

template <class A, class B>
constexpr decltype(auto) checked_shl(A&& a, B&& b) {
    using UA = std::remove_cvref_t<A>;
    using UB = std::remove_cvref_t<B>;
    if constexpr (std::is_integral_v<UA> && std::is_integral_v<UB>) {
        UB shift = b;
        bool bad = false;
        if constexpr (std::is_signed_v<UB>) {
            bad = shift < 0;
        }
        if (bad || static_cast<unsigned long long>(shift) >= sizeof(UA) * 8) {
            type_safety.report_violation("shift count is out of range");
            std::abort();
        }
        return static_cast<UA>(a << shift);
    } else {
        return std::forward<A>(a) << std::forward<B>(b);
    }
}

template <class A, class B>
constexpr decltype(auto) checked_shr(A&& a, B&& b) {
    using UA = std::remove_cvref_t<A>;
    using UB = std::remove_cvref_t<B>;
    if constexpr (std::is_integral_v<UA> && std::is_integral_v<UB>) {
        UB shift = b;
        bool bad = false;
        if constexpr (std::is_signed_v<UB>) {
            bad = shift < 0;
        }
        if (bad || static_cast<unsigned long long>(shift) >= sizeof(UA) * 8) {
            type_safety.report_violation("shift count is out of range");
            std::abort();
        }
        return static_cast<UA>(a >> shift);
    } else {
        return std::forward<A>(a) >> std::forward<B>(b);
    }
}

// ---------------------------------------------------------------------------
// Ranges: a..<b and a..=b
// ---------------------------------------------------------------------------
//
// The range operators work for any type that supports ++ and ==: they start at
// the first value and increment until they reach the last value (which `..=`
// includes).  For `..=`, the last value must be valid to increment once.

template <class T>
class iota_range {
public:
    class iterator {
    public:
        constexpr explicit iterator(T value) : value_(value) {}
        constexpr T operator*() const { return value_; }
        constexpr iterator& operator++() {
            ++value_;
            return *this;
        }
        constexpr bool operator==(iterator const& other) const { return value_ == other.value_; }
        constexpr bool operator!=(iterator const& other) const { return !(*this == other); }

    private:
        T value_;
    };

    constexpr iota_range(T first, T end) : first_(first), end_(end) {}

    constexpr iterator begin() const { return iterator{first_}; }
    constexpr iterator end() const { return iterator{end_}; }

    constexpr std::size_t size() const {
        std::size_t count = 0;
        for (auto it = first_; !(it == end_); ++it) {
            ++count;
        }
        return count;
    }

    constexpr bool contains(T const& value) const
        requires std::totally_ordered<T>
    {
        return !(value < first_) && (value < end_);
    }

    constexpr T sum() const
        requires requires(T a, T b) { a + b; }
    {
        T total{};
        for (auto value : *this) {
            total = total + value;
        }
        return total;
    }

private:
    T first_{};
    T end_{};
};

template <class A, class B>
constexpr auto range(A&& first, B&& last) {
    if constexpr (std::integral<std::remove_cvref_t<A>> && std::integral<std::remove_cvref_t<B>>) {
        using Common = std::common_type_t<std::remove_cvref_t<A>, std::remove_cvref_t<B>>;
        return iota_range<Common>{static_cast<Common>(first), static_cast<Common>(last)};
    } else {
        static_assert(std::same_as<std::remove_cvref_t<A>, std::remove_cvref_t<B>>,
                      "Epitaxy: the two endpoints of a ..< range must have the same type");
        return iota_range<std::remove_cvref_t<A>>{first, last};
    }
}

template <class A, class B>
constexpr auto closed_range(A&& first, B&& last) {
    if constexpr (std::integral<std::remove_cvref_t<A>> && std::integral<std::remove_cvref_t<B>>) {
        using Common = std::common_type_t<std::remove_cvref_t<A>, std::remove_cvref_t<B>>;
        return iota_range<Common>{static_cast<Common>(first), static_cast<Common>(last + 1)};
    } else {
        static_assert(std::same_as<std::remove_cvref_t<A>, std::remove_cvref_t<B>>,
                      "Epitaxy: the two endpoints of a ..= range must have the same type");
        auto one_past = last;
        ++one_past;
        return iota_range<std::remove_cvref_t<A>>{first, one_past};
    }
}

// ---------------------------------------------------------------------------
// main's args
// ---------------------------------------------------------------------------

class args_t {
public:
    class iterator {
    public:
        constexpr iterator(char** argv, int index) : argv_(argv), index_(index) {}
        constexpr std::string_view operator*() const { return std::string_view{argv_[index_]}; }
        constexpr iterator& operator++() {
            ++index_;
            return *this;
        }
        constexpr bool operator==(iterator const& other) const { return index_ == other.index_; }
        constexpr bool operator!=(iterator const& other) const { return !(*this == other); }

    private:
        char** argv_;
        int index_;
    };

    args_t() = default;
    args_t(int argc, char** argv) : argc(argc), argv(argv) {}

    int argc = 0;
    char** argv = nullptr;

    constexpr iterator begin() const { return iterator{argv, 1}; }
    constexpr iterator end() const { return iterator{argv, argc}; }

    constexpr std::string_view operator[](std::size_t index) const {
        return std::string_view{argv[index + 1]};
    }

    constexpr std::size_t size() const {
        return argc > 0 ? static_cast<std::size_t>(argc - 1) : 0;
    }
    constexpr std::ptrdiff_t ssize() const { return static_cast<std::ptrdiff_t>(size()); }
    constexpr bool empty() const { return size() == 0; }
};

[[noreturn]] inline void unreachable_() {
    std::abort();
}

// ---------------------------------------------------------------------------
// Threads
// ---------------------------------------------------------------------------
//
// Epitaxy exposes a small owned thread handle.  ``epi::spawn(f, args...)``
// starts a thread and returns an ``epi::thread``; dropping a joinable thread is
// a contract violation (Rust panics in the same situation), so either join or
// detach explicitly.
//
// The ownership checker treats ``epi::spawn`` specially: every argument that is
// moved into the new thread must be ``Send``.  This is the checked threading
// API; direct use of ``std::thread``/``std::async`` is outside the analysis.

class thread {
public:
    thread() noexcept = default;

    explicit thread(std::thread impl) noexcept : impl_(std::move(impl)) {}

    thread(thread&& other) noexcept : impl_(std::move(other.impl_)) {}

    thread& operator=(thread&& other) noexcept {
        if (impl_.joinable()) {
            type_safety.report_violation("thread assigned over while still joinable");
            std::terminate();
        }
        impl_ = std::move(other.impl_);
        return *this;
    }

    thread(thread const&) = delete;
    thread& operator=(thread const&) = delete;

    ~thread() {
        if (impl_.joinable()) {
            type_safety.report_violation(
                "thread dropped without join or detach");
            std::terminate();
        }
    }

    bool joinable() const noexcept { return impl_.joinable(); }

    void join() {
        if (!impl_.joinable()) {
            type_safety.report_violation("join called on a non-joinable thread");
            std::terminate();
        }
        impl_.join();
    }

    void detach() {
        if (!impl_.joinable()) {
            type_safety.report_violation("detach called on a non-joinable thread");
            std::terminate();
        }
        impl_.detach();
    }

    static unsigned hardware_concurrency() noexcept {
        return std::thread::hardware_concurrency();
    }

private:
    std::thread impl_{};
};

template <class F, class... Args>
thread spawn(F&& f, Args&&... args) {
    return thread(
        std::thread(std::forward<F>(f), std::forward<Args>(args)...));
}

// ---------------------------------------------------------------------------
// Safe-only output helpers
// ---------------------------------------------------------------------------
//
// In --safe-only mode the only I/O surface is the compiler runtime:
// ``print(...)`` / ``println(...)`` stream their arguments to std::cout.
// Everything else that is not Epitaxy source is rejected by the safe-only
// validator.

template <class... Args>
void print(Args&&... args) {
    (std::cout << ... << std::forward<Args>(args));
}

template <class... Args>
void println(Args&&... args) {
    print(std::forward<Args>(args)...);
    std::cout << '\n';
}

}  // namespace epi

// Prelude names are available unqualified in Epitaxy source.
using epi::i8;
using epi::i16;
using epi::i32;
using epi::i64;
using epi::longdouble;
using epi::longlong;
using epi::u8;
using epi::u16;
using epi::u32;
using epi::u64;
using epi::uint;
using epi::ulong;
using epi::ulonglong;
using epi::ushort;

// Explicitly unchecked operations use ordinary function-call syntax in Epitaxy.
using epi::unchecked_cast;
using epi::unchecked_cmp_equal;
using epi::unchecked_cmp_less;
using epi::unchecked_div;
using epi::unchecked_dereference;
using epi::unchecked_mod;
using epi::unchecked_narrow;
using epi::unchecked_subscript;
using epi::print;
using epi::println;
using epi::checked_add;
using epi::checked_add_assign;
using epi::checked_dec;
using epi::checked_div_assign;
using epi::checked_mod_assign;
using epi::checked_inc;
using epi::checked_mul;
using epi::checked_mul_assign;
using epi::checked_neg;
using epi::checked_shl;
using epi::checked_shr;
using epi::checked_sub;
using epi::checked_sub_assign;

#endif  // EPITAXY_SUPPORT_HPP_INCLUDED
