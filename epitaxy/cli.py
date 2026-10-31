"""Command line interface for the Epitaxy compiler.

Usage examples::

    epitaxy hello.ep                 # produce hello.cpp
    epitaxy --run hello.ep           # transpile, compile and run
    epitaxy --emit-cpp hello.ep      # print the generated C++20
    epitaxy -o build/hello.cpp hi.ep

The generated C++ includes ``epi_support.hpp``; when Epitaxy compiles the
result itself it adds its bundled support directory to the include path.
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
from typing import Optional

from . import __version__
from .codegen import Options
from .compiler import transpile
from .diag import CompileError

SUPPORT_DIR = Path(__file__).resolve().parent / "support"
SUPPORT_HEADER = SUPPORT_DIR / "epi_support.hpp"


def find_cxx() -> str:
    for candidate in (os.environ.get("CXX"), "clang++", "g++", "c++"):
        if candidate and shutil.which(candidate):
            return candidate
    return ""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epitaxy",
        description="Compile Epitaxy (.ep) source files to C++20.",
        epilog=(
            "Epitaxy is MIT licensed.  It follows the Cpp2 syntax requirements "
            "and contains no code from cppfront."
        ),
    )
    parser.add_argument("files", nargs="+", metavar="FILE.ep", help="Epitaxy source file(s)")
    parser.add_argument("-o", "--output", metavar="FILE.cpp", help="output C++ file")
    parser.add_argument(
        "--emit-cpp",
        action="store_true",
        help="write the generated C++ to standard output instead of a file",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="transpile, compile with a C++20 compiler and run the program",
    )
    parser.add_argument("-c", "--compile", action="store_true", help="only compile (to an executable)")
    parser.add_argument("--cc", metavar="CXX", help="C++ compiler to use for --run/-c (default: $CXX)")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep intermediate C++ files and compiled executables",
    )
    parser.add_argument(
        "--print-cpp",
        action="store_true",
        help="print the generated C++ to standard error as well",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress informational messages")
    parser.add_argument(
        "-no-subscript-checks",
        dest="subscript_checks",
        action="store_false",
        help="disable default-on subscript bounds checking",
    )
    parser.add_argument(
        "-no-null-checks",
        dest="null_checks",
        action="store_false",
        help="disable default-on null dereference checking",
    )
    parser.add_argument(
        "-no-div-zero-checks",
        dest="div_checks",
        action="store_false",
        help="disable default-on integer division-by-zero checking",
    )
    parser.add_argument(
        "-no-comparison-checks",
        dest="sign_checks",
        action="store_false",
        help="disable default-on mixed-sign comparison checking",
    )
    parser.add_argument(
        "-no-ownership-checks",
        dest="ownership_checks",
        action="store_false",
        help="disable default-on ownership/borrow checking",
    )
    parser.add_argument(
        "-no-prelude",
        dest="preload_std",
        action="store_false",
        help="do not pre-include the C++ standard-library headers",
    )
    parser.add_argument(
        "-strict-safety",
        dest="strict_safety",
        action="store_true",
        help="enforce the strict safety guarantee (all checks on, checked "
        "arithmetic, no raw C++ outside unsafe)",
    )
    parser.add_argument("--version", action="version", version=f"Epitaxy {__version__} (MIT)")
    return parser


def transpile_file(path: Path, options: Options) -> str:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompileError(f"cannot read source file: {exc}") from exc
    return transpile(source, str(path), options)


def main(argv: Optional[list[str]] = None) -> int:
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    program_args: list[str] = []
    if "--" in raw:
        index = raw.index("--")
        raw, program_args = raw[:index], raw[index + 1 :]
    args = build_arg_parser().parse_args(raw)
    if args.strict_safety and not all(
        [
            args.subscript_checks,
            args.null_checks,
            args.div_checks,
            args.sign_checks,
            args.ownership_checks,
        ]
    ):
        print(
            "epitaxy: error: -strict-safety cannot be combined with -no-* checks",
            file=sys.stderr,
        )
        return 2

    options = Options(
        subscript_checks=args.subscript_checks,
        null_checks=args.null_checks,
        div_checks=args.div_checks,
        sign_checks=args.sign_checks,
        ownership_checks=args.ownership_checks,
        preload_std=args.preload_std,
        strict_safety=args.strict_safety,
    )

    files = [Path(f) for f in args.files]
    if len(files) > 1 and (args.run or args.compile or args.output):
        print("epitaxy: error: -o/--run/-c accept a single input file", file=sys.stderr)
        return 2
    if len(files) > 1 and args.emit_cpp:
        print("epitaxy: error: --emit-cpp accepts a single input file", file=sys.stderr)
        return 2

    exit_code = 0
    for path in files:
        if not path.exists():
            print(f"epitaxy: error: no such file: {path}", file=sys.stderr)
            return 2
        try:
            cpp = transpile_file(path, options)
        except CompileError as exc:
            print(exc.render(), file=sys.stderr)
            return 1

        if args.print_cpp:
            print(cpp, file=sys.stderr)

        if args.emit_cpp:
            sys.stdout.write(cpp)
            continue

        # When we compile/run the program ourselves the C++ file is an
        # intermediate; do not litter the source tree unless asked to.
        want_file = (not (args.run or args.compile)) or args.output is not None
        if want_file:
            output_path = _output_path(path, args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(cpp, encoding="utf-8")
            if not args.quiet:
                print(f"epitaxy: generated {output_path}", file=sys.stderr)

        if args.run or args.compile:
            code = _compile_and_run(
                cpp,
                path,
                args,
                run=args.run,
                program_args=program_args,
            )
            if code != 0:
                exit_code = code
                break

    return exit_code


def _output_path(source: Path, output: Optional[str]) -> Path:
    if output:
        return Path(output)
    return source.with_suffix(".cpp")


def _compile_and_run(cpp: str, source: Path, args, run: bool, program_args: Optional[list[str]] = None) -> int:
    cxx = args.cc or find_cxx()
    if not cxx:
        print(
            "epitaxy: error: no C++ compiler found; set CXX or pass --cc",
            file=sys.stderr,
        )
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="epitaxy-"))
    cpp_path = workdir / (source.stem + ".cpp")
    exe_path = workdir / source.stem
    cpp_path.write_text(cpp, encoding="utf-8")
    command = [
        cxx,
        *shlex.split(os.environ.get("CXXFLAGS", "")),
        "-std=c++20",
        "-Wall",
        "-Wextra",
        "-Wno-unused-parameter",
        "-Wno-unused-label",
        "-I",
        str(SUPPORT_DIR),
        str(cpp_path),
        "-o",
        str(exe_path),
    ]
    if not args.quiet:
        print("epitaxy: compiling with " + " ".join(command), file=sys.stderr)
    result = subprocess.run(command)
    if result.returncode != 0:
        print("epitaxy: error: C++ compilation failed", file=sys.stderr)
        if args.keep:
            print(f"epitaxy: intermediate files kept in {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
        return 1

    if not run:
        if args.keep:
            print(f"epitaxy: executable at {exe_path}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
        return 0

    try:
        program = subprocess.run([str(exe_path)] + list(program_args or []))
    except OSError as exc:
        print(f"epitaxy: error: could not run program: {exc}", file=sys.stderr)
        shutil.rmtree(workdir, ignore_errors=True)
        return 1
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return program.returncode


if __name__ == "__main__":
    raise SystemExit(main())
