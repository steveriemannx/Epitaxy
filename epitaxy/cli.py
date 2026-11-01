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

from . import __version__, ast
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


def _project_command(argv: list[str]) -> int:
    from . import project
    from .diag import CompileError

    command = argv[0]
    rest = argv[1:]
    if command == "new":
        parser = argparse.ArgumentParser(
            prog="epitaxy new", description="create a new Epitaxy project"
        )
        parser.add_argument("name", help="project (directory) name")
        parser.add_argument(
            "--path", default=".", help="parent directory (default: current)"
        )
        args = parser.parse_args(rest)
        try:
            root = project.new_project(args.name, Path(args.path))
        except project.ProjectError as exc:
            print(f"epitaxy: error: {exc}", file=sys.stderr)
            return 1
        print(f"created {root}")
        print("  Epitaxy.toml")
        print("  src/main.ep")
        print("  .gitignore")
        print()
        print("next:")
        print(f"  cd {args.name}")
        print("  epitaxy run")
        return 0

    parser = argparse.ArgumentParser(
        prog=f"epitaxy {command}",
        description="build, run, test or check an Epitaxy project",
    )
    parser.add_argument("--cc", default=None, help="C++ compiler used for the module build")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep intermediate build files")
    parser.add_argument("--sanitize", action="store_true",
                        help="compile with ASan/UBSan")
    parser.add_argument("--safe-only", dest="safe_only", action="store_true")
    parser.add_argument("-strict-safety", dest="strict_safety", action="store_true")
    parser.add_argument("--release", action="store_true", help="accepted for compatibility")
    if command == "run":
        if "--" in rest:
            index = rest.index("--")
            program_args = rest[index + 1 :]
            rest = rest[:index]
        else:
            program_args = []
    else:
        program_args = []
    args = parser.parse_args(rest)

    if args.sanitize:
        os.environ["CXXFLAGS"] = (
            os.environ.get("CXXFLAGS", "")
            + " -fsanitize=address,undefined -fno-sanitize-recover=all -g"
        ).strip()
    manifest = project.find_manifest()
    if manifest is None:
        print(
            "epitaxy: error: no Epitaxy.toml found in this directory or its parents",
            file=sys.stderr,
        )
        return 2
    root = manifest.parent
    try:
        if command == "build":
            output = project.build_project(root, args.cc, args.quiet, args.keep)
            if not args.quiet:
                print(f"epitaxy: built {output}", file=sys.stderr)
            return 0
        if command == "run":
            return project.run_project(
                root, args.cc, args.quiet, args.keep, program_args
            )
        if command == "test":
            return project.test_project(root, args.cc, args.quiet, args.keep)
        if command == "check":
            return project.check_project(root)
    except project.ProjectError as exc:
        print(f"epitaxy: error: {exc}", file=sys.stderr)
        return 1
    except CompileError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    return 2


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epitaxy",
        description="Compile Epitaxy (.ep) source files to C++20.",
        epilog=(
            "Epitaxy is MIT licensed.  It follows the Cpp2 syntax requirements "
            "and contains no code from cppfront.  Project commands: "
            "epitaxy new <name> | build | run | test | check."
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
        "--sanitize",
        dest="sanitize",
        action="store_true",
        help="compile with -fsanitize=address,undefined when using --run/-c",
    )
    parser.add_argument(
        "--cpp-modules",
        dest="cpp_modules",
        action="store_true",
        help="prototype C++20 modules build: every input with 'module name;' is "
        "compiled as a C++ module, the single input without it is the entry "
        "point (requires GCC or upstream Clang; Apple Clang lacks C++20 "
        "modules)",
    )
    parser.add_argument(
        "--safe-only",
        dest="safe_only",
        action="store_true",
        help="restrict the program to Epitaxy and the compiler runtime: no "
        "preprocessor, cpp1, unsafe, raw pointers, external C++ names or "
        "thread API; implies -strict-safety",
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


PROJECT_COMMANDS = ("new", "build", "run", "test", "check")


def main(argv: Optional[list[str]] = None) -> int:
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    if raw and raw[0] in PROJECT_COMMANDS:
        return _project_command(raw)
    program_args: list[str] = []
    if "--" in raw:
        index = raw.index("--")
        raw, program_args = raw[:index], raw[index + 1 :]
    args = build_arg_parser().parse_args(raw)
    if (args.strict_safety or args.safe_only) and not all(
        [
            args.subscript_checks,
            args.null_checks,
            args.div_checks,
            args.sign_checks,
            args.ownership_checks,
        ]
    ):
        print(
            "epitaxy: error: strict/safe-only mode cannot be combined with -no-* checks",
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
        strict_safety=args.strict_safety or args.safe_only,
        safe_only=args.safe_only,
    )

    if args.cpp_modules:
        if args.emit_cpp or args.print_cpp:
            print(
                "epitaxy: error: --cpp-modules does not support --emit-cpp/--print-cpp",
                file=sys.stderr,
            )
            return 2
        return _build_cpp_modules(args, program_args)

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


def _probe_module_toolchain(cxx: str):
    """Return 'gcc', 'clang' or None for a compiler's C++20 module support."""
    workdir = Path(tempfile.mkdtemp(prefix="epitaxy-module-probe-"))
    try:
        source = workdir / "probe.cppm"
        source.write_text(
            "export module epi_probe;\n"
            "export int epi_probe_fn() { return 0; }\n",
            encoding="utf-8",
        )
        pcm = workdir / "probe.pcm"
        clang = subprocess.run(
            [cxx, "-std=c++20", "-x", "c++-module", "--precompile",
             str(source), "-o", str(pcm)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if clang.returncode == 0 and pcm.exists():
            return "clang"
        obj = workdir / "probe.o"
        gcc = subprocess.run(
            [cxx, "-std=c++20", "-fmodules-ts", "-x", "c++", "-c",
             str(source), "-o", str(obj)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(workdir),
        )
        if gcc.returncode == 0 and obj.exists():
            return "gcc"
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _module_toolchain(args):
    if args.cc:
        candidates = [args.cc]
    else:
        candidates = []
        for candidate in (
            os.environ.get("CXX"),
            "g++-16",
            "g++",
            "/opt/homebrew/opt/llvm/bin/clang++",
            "/usr/local/opt/llvm/bin/clang++",
            "clang++",
        ):
            if not candidate:
                continue
            resolved = shutil.which(candidate) or candidate
            if resolved and resolved not in candidates:
                candidates.append(resolved)
    for cxx in candidates:
        if not (Path(cxx).exists() or shutil.which(cxx)):
            continue
        kind = _probe_module_toolchain(cxx)
        if kind is not None:
            return cxx, kind
    print(
        "epitaxy: error: no C++20 module support found. Pass --cc with a "
        "compiler that supports modules (GCC with -fmodules-ts, or upstream "
        "Clang). Apple Clang does not support C++20 modules.",
        file=sys.stderr,
    )
    return None, None


def _module_options(args) -> Options:
    return Options(
        subscript_checks=args.subscript_checks,
        null_checks=args.null_checks,
        div_checks=args.div_checks,
        sign_checks=args.sign_checks,
        ownership_checks=args.ownership_checks,
        preload_std=args.preload_std,
        strict_safety=args.strict_safety or args.safe_only,
        safe_only=args.safe_only,
    )


def _build_cpp_modules(args, program_args: Optional[list[str]] = None) -> int:
    from .compiler import transpile
    from .diag import CompileError
    from .parser import parse

    cxx, toolchain = _module_toolchain(args)
    if cxx is None:
        return 2
    files = [Path(f) for f in args.files]
    if not files:
        print("epitaxy: error: --cpp-modules needs input .ep files", file=sys.stderr)
        return 2

    entry = None
    module_files: dict[str, tuple[Path, list[str], object]] = {}
    for path in files:
        if not path.exists():
            print(f"epitaxy: error: no such file: {path}", file=sys.stderr)
            return 2
        source = path.read_text(encoding="utf-8")
        try:
            tu = parse(source, str(path))
        except CompileError as exc:
            print(exc.render(), file=sys.stderr)
            return 1
        name = None
        imports = []
        for item in tu.items:
            if isinstance(item, ast.ModuleDecl):
                if name is not None:
                    print(
                        f"epitaxy: error: {path}: more than one 'module' declaration",
                        file=sys.stderr,
                    )
                    return 1
                name = item.name
            elif isinstance(item, ast.ImportDecl):
                imports.append(item.name)
        if name is None:
            if entry is not None:
                print(
                    "epitaxy: error: --cpp-modules needs exactly one entry file "
                    "(an input without a 'module name;' declaration)",
                    file=sys.stderr,
                )
                return 1
            entry = (path, imports, tu)
        else:
            if name in module_files:
                print(
                    f"epitaxy: error: duplicate module '{name}' in "
                    f"{module_files[name][0]} and {path}",
                    file=sys.stderr,
                )
                return 1
            module_files[name] = (path, imports, tu)
    if entry is None:
        print(
            "epitaxy: error: --cpp-modules needs exactly one entry file without "
            "a 'module name;' declaration",
            file=sys.stderr,
        )
        return 1

    # Module files cannot define main.
    def defines_main(items: list) -> bool:
        for item in items:
            if isinstance(item, ast.FunctionDecl) and item.name == "main":
                return True
            if isinstance(item, ast.NamespaceDecl) and item.kind == "=":
                if defines_main(item.members):
                    return True
        return False

    for name, (path, _imports, tu) in module_files.items():
        if defines_main(tu.items):
            print(
                f"epitaxy: error: module '{name}' ({path}) must not define main; "
                "put main in the entry file",
                file=sys.stderr,
            )
            return 1

    # Resolve imports and compute a dependency order.
    state: dict[str, int] = {}
    order: list[str] = []

    def visit(name: str, stack: list) -> bool:
        if state.get(name) == 2:
            return True
        if state.get(name) == 1:
            cycle = " -> ".join(stack + [name])
            print(
                f"epitaxy: error: import cycle: {cycle}",
                file=sys.stderr,
            )
            return False
        state[name] = 1
        stack.append(name)
        for dep in module_files[name][1]:
            if dep not in module_files:
                print(
                    f"epitaxy: error: module '{name}' imports unknown module "
                    f"'{dep}'",
                    file=sys.stderr,
                )
                return False
            if not visit(dep, stack):
                return False
        stack.pop()
        state[name] = 2
        order.append(name)
        return True

    for name in module_files:
        if not visit(name, []):
            return 1
    for dep in entry[1]:
        if dep not in module_files:
            print(
                f"epitaxy: error: entry file imports unknown module '{dep}'",
                file=sys.stderr,
            )
            return 1

    workdir = Path(tempfile.mkdtemp(prefix="epitaxy-modules-"))
    cxxflags = shlex.split(os.environ.get("CXXFLAGS", ""))
    base_flags = [
        *cxxflags,
        "-std=c++20",
        "-Wall",
        "-Wextra",
        "-Wno-unused-parameter",
        "-Wno-unused-label",
        "-I",
        str(SUPPORT_DIR),
    ]
    module_flags = ["-fmodules-ts"] if toolchain == "gcc" else []
    if toolchain == "clang":
        module_flags = ["-Wno-unused-command-line-argument"]

    def run(cmd: list[str]) -> bool:
        if not args.quiet:
            print("epitaxy: running " + " ".join(cmd), file=sys.stderr)
        result = subprocess.run(cmd, cwd=str(workdir))
        return result.returncode == 0

    try:
        objects: list[str] = []
        pcm_paths: dict[str, Path] = {}
        for name in order:
            path, imports, _tu = module_files[name]
            options = _module_options(args)
            options.cpp_module = name
            options.cpp_module_imports = imports
            cpp = transpile(path.read_text(encoding="utf-8"), str(path), options)
            cppm = workdir / f"{name}.cppm"
            cppm.write_text(cpp, encoding="utf-8")
            obj = workdir / f"{name}.o"
            if toolchain == "clang":
                pcm_flags = [
                    f"-fmodule-file=epi_{dep}={pcm_paths[dep]}" for dep in imports
                ]
                pcm = workdir / f"{name}.pcm"
                precompile = [
                    cxx, *base_flags, "-x", "c++-module", "--precompile",
                    str(cppm), "-o", str(pcm), *pcm_flags,
                ]
                if not run(precompile):
                    print(
                        f"epitaxy: error: precompiling module '{name}' failed",
                        file=sys.stderr,
                    )
                    return 1
                pcm_paths[name] = pcm
                command = [cxx, *base_flags, "-c", str(pcm), "-o", str(obj), *pcm_flags]
            else:
                command = [
                    cxx, *base_flags, *module_flags, "-x", "c++", "-c",
                    str(cppm), "-o", str(obj),
                ]
            if not run(command):
                print(
                    f"epitaxy: error: compiling module '{name}' failed",
                    file=sys.stderr,
                )
                return 1
            objects.append(str(obj))

        entry_path, entry_imports, _tu = entry
        options = _module_options(args)
        options.cpp_module_entry = True
        options.cpp_module_imports = entry_imports
        cpp = transpile(entry_path.read_text(encoding="utf-8"), str(entry_path), options)
        entry_cpp = workdir / "main.cpp"
        entry_cpp.write_text(cpp, encoding="utf-8")
        entry_obj = workdir / "main.o"
        if toolchain == "clang":
            entry_pcm_flags = [
                f"-fmodule-file=epi_{dep}={pcm_paths[dep]}" for dep in entry_imports
            ]
            command = [
                cxx, *base_flags, "-c", str(entry_cpp), "-o", str(entry_obj),
                *entry_pcm_flags,
            ]
        else:
            command = [
                cxx, *base_flags, *module_flags, "-c", str(entry_cpp),
                "-o", str(entry_obj),
            ]
        if not run(command):
            print("epitaxy: error: compiling the entry file failed", file=sys.stderr)
            return 1

        exe = workdir / "program"
        command = [
            cxx, *cxxflags, "-std=c++20", *module_flags,
            str(entry_obj), *objects, "-o", str(exe),
        ]
        if not run(command):
            print("epitaxy: error: linking the module program failed", file=sys.stderr)
            return 1

        if args.output:
            output_path = Path(args.output)
            shutil.copy2(exe, output_path)
            if not args.quiet:
                print(f"epitaxy: executable written to {output_path}", file=sys.stderr)
        if args.run:
            program = subprocess.run([str(exe)] + list(program_args or []))
            if program.returncode != 0:
                return program.returncode
        elif not args.quiet:
            print(f"epitaxy: built executable at {exe}", file=sys.stderr)
        if args.keep:
            print(f"epitaxy: module build files kept in {workdir}", file=sys.stderr)
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


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
    ]
    if getattr(args, "sanitize", False):
        command += [
            "-fsanitize=address,undefined",
            "-fno-sanitize-recover=all",
            "-g",
        ]
    command += [
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
