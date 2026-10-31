#!/usr/bin/env python3
"""Test runner for the Epitaxy compiler.

Test case layout
----------------

``tests/cases/<name>.ep``
    A complete program that must compile, run, and print exactly the contents
    of ``<name>.expected`` to standard output (exit code 0 unless ``<name>.exit``
    exists, which holds the expected exit code).

    Optional companion files:

    * ``<name>.args``   -- command line arguments for the program (shlex split)
    * ``<name>.flags``  -- compiler flags, e.g. ``-no-comparison-checks``
    * ``<name>.stdin``  -- standard input for the program

``tests/errors/<name>.ep``
    A program that must be rejected.  ``<name>.error`` holds a substring of the
    expected diagnostic.  If the diagnostic comes from the C++ compiler instead
    of the Epitaxy compiler, add ``<name>.stage`` containing ``cpp``.

Usage::

    python3 tests/run_tests.py                 # run everything
    python3 tests/run_tests.py -f inspect      # only matching tests
    python3 tests/run_tests.py -v              # show compiler output
    python3 tests/run_tests.py --keep          # keep temporary build dirs
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from epitaxy.codegen import CodeGen, Options  # noqa: E402
from epitaxy.diag import CompileError  # noqa: E402
from epitaxy.parser import parse  # noqa: E402

CASES_DIR = ROOT / "tests" / "cases"
ERRORS_DIR = ROOT / "tests" / "errors"
EXAMPLES_DIR = ROOT / "examples"
SUPPORT_DIR = ROOT / "epitaxy" / "support"

CXX_FLAGS = ["-std=c++20", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Wno-unused-label"]

RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"


def find_cxx() -> str:
    for candidate in (os.environ.get("CXX"), "clang++", "g++", "c++"):
        if candidate and shutil.which(candidate):
            return candidate
    raise SystemExit("error: no C++ compiler found; set CXX")


def options_from_flags(flags: list[str]) -> Options:
    opts = Options()
    for flag in flags:
        if flag == "-no-subscript-checks":
            opts.subscript_checks = False
        elif flag == "-no-null-checks":
            opts.null_checks = False
        elif flag == "-no-div-zero-checks":
            opts.div_checks = False
        elif flag == "-no-comparison-checks":
            opts.sign_checks = False
        else:
            raise SystemExit(f"error: unknown compiler flag in .flags file: {flag}")
    return opts


def transpile(path: Path, options: Options):
    source = path.read_text(encoding="utf-8")
    tu = parse(source, str(path))
    return CodeGen(options, str(path)).generate(tu)


def compile_cpp(cpp: str, name: str, cxx: str, workdir: Path, keep: bool):
    cpp_path = workdir / f"{name}.cpp"
    exe_path = workdir / name
    cpp_path.write_text(cpp, encoding="utf-8")
    extra = shlex.split(os.environ.get("CXXFLAGS", ""))
    command = [cxx, *CXX_FLAGS, *extra, "-I", str(SUPPORT_DIR), str(cpp_path), "-o", str(exe_path)]
    result = subprocess.run(command, capture_output=True, text=True)
    return result, exe_path, cpp_path


def read_optional(path: Path, default: str = "") -> str:
    return path.read_text(encoding="utf-8") if path.exists() else default


def run_program_case(path: Path, cxx: str, keep: bool, verbose: bool) -> tuple[bool, str]:
    name = path.stem
    flags = shlex.split(read_optional(path.with_suffix(".flags")))
    args = shlex.split(read_optional(path.with_suffix(".args")))
    stdin_data = read_optional(path.with_suffix(".stdin"))
    expected_stdout = read_optional(path.with_suffix(".expected"))
    expected_exit = int(read_optional(path.with_suffix(".exit"), "0").strip() or "0")

    try:
        cpp = transpile(path, options_from_flags(flags))
    except CompileError as exc:
        return False, f"Epitaxy rejected the program:\n{exc.render()}"

    workdir = Path(tempfile.mkdtemp(prefix=f"epitaxy-test-{name}-"))
    try:
        result, exe_path, cpp_path = compile_cpp(cpp, name, cxx, workdir, keep)
        if result.returncode != 0:
            return False, "C++ compilation failed:\n" + result.stderr
        if verbose and result.stderr.strip():
            print(f"    warnings:\n{result.stderr}", file=sys.stderr)
        try:
            program = subprocess.run(
                [str(exe_path), *args],
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return False, "program timed out"
        ok = True
        details = []
        if program.stdout != expected_stdout:
            ok = False
            details.append(
                "stdout mismatch\n"
                f"--- expected ---\n{expected_stdout}--- actual ---\n{program.stdout}"
            )
        if expected_exit != program.returncode:
            ok = False
            details.append(f"exit code: expected {expected_exit}, got {program.returncode}")
        if not ok:
            details.append(f"(generated C++ kept at {cpp_path})" if keep else "")
        return ok, "\n".join(details)
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"    kept: {workdir}", file=sys.stderr)


def run_error_case(path: Path, cxx: str, keep: bool, verbose: bool) -> tuple[bool, str]:
    name = path.stem
    expected = read_optional(path.with_suffix(".error")).strip()
    stage = read_optional(path.with_suffix(".stage")).strip() or "epitaxy"
    flags = shlex.split(read_optional(path.with_suffix(".flags")))

    if stage == "epitaxy":
        try:
            transpile(path, options_from_flags(flags))
        except CompileError as exc:
            rendered = exc.render()
            if expected and expected not in rendered:
                return False, f"error message mismatch\nexpected substring: {expected!r}\nactual: {rendered}"
            return True, ""
        return False, "the Epitaxy compiler accepted an invalid program"

    # C++-stage diagnostics (e.g. default-on mixed-sign comparison checks).
    try:
        cpp = transpile(path, options_from_flags(flags))
    except CompileError as exc:
        return False, f"unexpected Epitaxy error: {exc.render()}"
    workdir = Path(tempfile.mkdtemp(prefix=f"epitaxy-test-{name}-"))
    try:
        result, _exe, cpp_path = compile_cpp(cpp, name, cxx, workdir, keep)
        if result.returncode == 0:
            return False, "the C++ compiler accepted an invalid program"
        if expected and expected not in result.stderr:
            return False, (
                f"error message mismatch\nexpected substring: {expected!r}\n"
                f"actual:\n{result.stderr}"
            )
        return True, ""
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"    kept: {workdir}", file=sys.stderr)


def run_example(path: Path, cxx: str, keep: bool, verbose: bool) -> tuple[bool, str]:
    try:
        cpp = transpile(path, Options())
    except CompileError as exc:
        return False, f"Epitaxy rejected the example:\n{exc.render()}"
    workdir = Path(tempfile.mkdtemp(prefix=f"epitaxy-example-{path.stem}-"))
    try:
        result, exe_path, _ = compile_cpp(cpp, path.stem, cxx, workdir, keep)
        if result.returncode != 0:
            return False, "C++ compilation failed:\n" + result.stderr
        program = subprocess.run([str(exe_path)], capture_output=True, text=True, timeout=60)
        if program.returncode != 0:
            return False, f"example exited with {program.returncode}"
        if verbose:
            print(f"    example output:\n{program.stdout}", file=sys.stderr)
        return True, ""
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Epitaxy test suite")
    parser.add_argument("-f", "--filter", default="", help="only run tests whose name contains this")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep generated C++ and executables")
    parser.add_argument("--no-examples", action="store_true")
    args = parser.parse_args(argv)

    global failures
    cxx = find_cxx()
    failures = 0
    total = 0

    def selected(path: Path) -> bool:
        return args.filter in path.stem

    case_files = sorted(p for p in CASES_DIR.glob("*.ep") if selected(p))
    error_files = sorted(p for p in ERRORS_DIR.glob("*.ep") if selected(p))
    example_files = [] if args.no_examples else sorted(EXAMPLES_DIR.glob("*.ep"))

    for path in case_files:
        total += 1
        ok, details = run_program_case(path, cxx, args.keep, args.verbose)
        _report("case", path.name, ok, details, args.verbose)

    for path in error_files:
        total += 1
        ok, details = run_error_case(path, cxx, args.keep, args.verbose)
        _report("error", path.name, ok, details, args.verbose)

    if not args.filter:
        for path in example_files:
            total += 1
            ok, details = run_example(path, cxx, args.keep, args.verbose)
            _report("example", path.name, ok, details, args.verbose)

    print()
    if failures:
        print(f"{RED}{failures} of {total} tests failed{RESET}")
        return 1
    print(f"{GREEN}all {total} tests passed{RESET}")
    return 0


def _report(kind: str, name: str, ok: bool, details: str, verbose: bool) -> None:
    global failures
    if ok:
        if verbose:
            print(f"{GREEN}PASS{RESET} {kind:7} {name}")
    else:
        failures += 1
        print(f"{RED}FAIL{RESET} {kind:7} {name}")
        for line in details.splitlines():
            print("     " + line)


failures = 0


if __name__ == "__main__":
    raise SystemExit(main())
