"""Cargo-like Epitaxy projects.

A project is a directory containing ``Epitaxy.toml`` and a ``src/`` tree:

    myapp/
      Epitaxy.toml
      src/main.ep        # entry point (defines main)
      src/greeting.ep    # `module greeting;` ...
      tests/smoke.ep     # each test file is a program that must exit 0

``epitaxy build`` compiles the entry and all modules into ``target/<name>``;
``epitaxy run`` builds and runs it; ``epitaxy test`` builds and runs every
``tests/*.ep`` file; ``epitaxy check`` parses and lowers every source file.

Path dependencies are supported in the manifest:

    [dependencies]
    util = "../util"                 # shorthand
    util = { path = "../util" }      # inline table

A dependency contributes its own ``src/`` modules; module names share one flat
namespace for now (import the declared ``module`` name).
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from .diag import CompileError


# ---------------------------------------------------------------------------
# Minimal TOML subset
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    in_string = False
    escaped = False
    for index, char in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "#":
                return line[:index]
    return line


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    current = []
    for char in text:
        if in_string:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            current.append(char)
        elif char in "{[":
            depth += 1
            current.append(char)
        elif char in "}]":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current).strip())
    return parts


def _parse_value(text: str):
    text = text.strip()
    if not text:
        return ""
    if text.startswith('"') and text.endswith('"'):
        body = text[1:-1]
        return body.replace('\\"', '"').replace("\\\\", "\\")
    if text in ("true", "false"):
        return text == "true"
    if text.startswith("{") and text.endswith("}"):
        table = {}
        for item in _split_top_level(text[1:-1]):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            table[key.strip()] = _parse_value(value)
        return table
    if text.startswith("[") and text.endswith("]"):
        return [_parse_value(item) for item in _split_top_level(text[1:-1])]
    try:
        return int(text)
    except ValueError:
        return text


def parse_manifest_text(text: str) -> dict:
    data: dict = {}
    section: Optional[str] = None
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        table = data.setdefault(section or "", {})
        table[key.strip()] = _parse_value(value)
    return data


# ---------------------------------------------------------------------------
# Manifest / package loading
# ---------------------------------------------------------------------------


@dataclass
class Manifest:
    root: Path
    name: str
    version: str = "0.1.0"
    entry: str = "src/main.ep"
    dependencies: dict[str, Path] = field(default_factory=dict)


@dataclass
class Package:
    root: Path
    manifest: Manifest
    dependencies: list["Package"] = field(default_factory=list)


class ProjectError(Exception):
    pass


def find_manifest(start: Optional[Path] = None) -> Optional[Path]:
    path = (start or Path.cwd()).resolve()
    if path.is_file():
        path = path.parent
    for candidate in [path, *path.parents]:
        manifest = candidate / "Epitaxy.toml"
        if manifest.is_file():
            return manifest
    return None


def load_manifest(root: Path) -> Manifest:
    manifest_path = root / "Epitaxy.toml"
    if not manifest_path.is_file():
        raise ProjectError(f"no Epitaxy.toml found in {root}")
    data = parse_manifest_text(manifest_path.read_text(encoding="utf-8"))
    package = data.get("package", {})
    name = package.get("name")
    if not isinstance(name, str) or not name:
        raise ProjectError(f"{manifest_path}: [package] name is required")
    dependencies: dict[str, Path] = {}
    for dep_name, spec in data.get("dependencies", {}).items():
        if isinstance(spec, dict):
            raw_path = spec.get("path")
        else:
            raw_path = spec
        if not isinstance(raw_path, str) or not raw_path:
            raise ProjectError(
                f"{manifest_path}: dependency '{dep_name}' needs a path"
            )
        dep_path = Path(raw_path)
        if not dep_path.is_absolute():
            dep_path = (root / dep_path).resolve()
        dependencies[dep_name] = dep_path
    return Manifest(
        root=root,
        name=name,
        version=str(package.get("version", "0.1.0")),
        entry=str(package.get("entry", "src/main.ep")),
        dependencies=dependencies,
    )


def load_package(root: Path, seen: Optional[set[Path]] = None) -> Package:
    root = root.resolve()
    seen = set() if seen is None else seen
    if root in seen:
        raise ProjectError(f"dependency cycle through {root}")
    seen = seen | {root}
    manifest = load_manifest(root)
    package = Package(root=root, manifest=manifest)
    for dep_name, dep_path in manifest.dependencies.items():
        if not (dep_path / "Epitaxy.toml").is_file():
            raise ProjectError(
                f"{root / 'Epitaxy.toml'}: dependency '{dep_name}' at {dep_path} "
                "has no Epitaxy.toml"
            )
        package.dependencies.append(load_package(dep_path, seen))
    return package


def iter_packages(package: Package):
    yield package
    for dependency in package.dependencies:
        yield from iter_packages(dependency)


def entry_path(project_root: Path) -> Path:
    manifest = load_manifest(project_root)
    entry = (project_root / manifest.entry).resolve()
    if not entry.is_file():
        raise ProjectError(
            f"{manifest.root / 'Epitaxy.toml'}: entry file '{manifest.entry}' "
            "does not exist"
        )
    return entry


def collect_sources(project_root: Path):
    """Return (entry_file, module_files, packages)."""
    root_package = load_package(project_root)
    entry = entry_path(project_root)
    module_files: list[Path] = []
    for package in iter_packages(root_package):
        src = package.root / "src"
        if not src.is_dir():
            continue
        for path in sorted(src.rglob("*.ep")):
            resolved = path.resolve()
            if resolved == entry:
                continue
            module_files.append(resolved)
    return entry, module_files, list(iter_packages(root_package))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _build_args(cc: Optional[str], quiet: bool, keep: bool, files: list[Path],
                output: Optional[Path], run: bool = False):
    return SimpleNamespace(
        cc=cc,
        quiet=quiet,
        keep=keep,
        output=str(output) if output is not None else None,
        run=run,
        files=[str(path) for path in files],
        emit_cpp=False,
        print_cpp=False,
        subscript_checks=True,
        null_checks=True,
        div_checks=True,
        sign_checks=True,
        ownership_checks=True,
        preload_std=True,
        strict_safety=False,
        safe_only=False,
    )


def build_project(project_root: Path, cc: Optional[str], quiet: bool = False,
                  keep: bool = False) -> Path:
    from .cli import _build_cpp_modules

    entry, modules, _packages = collect_sources(project_root)
    manifest = load_manifest(project_root)
    target = project_root / "target"
    target.mkdir(parents=True, exist_ok=True)
    output = target / manifest.name
    if not quiet:
        print(
            f"epitaxy: building {manifest.name} v{manifest.version} "
            f"({1 + len(modules)} source files)",
            file=sys.stderr,
        )
    code = _build_cpp_modules(
        _build_args(cc, quiet, keep, [entry, *modules], output, run=False)
    )
    if code != 0:
        raise ProjectError(f"build failed with exit code {code}")
    return output


def run_project(project_root: Path, cc: Optional[str], quiet: bool = False,
                keep: bool = False, program_args: Optional[list[str]] = None) -> int:
    from .cli import _build_cpp_modules

    entry, modules, _packages = collect_sources(project_root)
    manifest = load_manifest(project_root)
    target = project_root / "target"
    target.mkdir(parents=True, exist_ok=True)
    output = target / manifest.name
    return _build_cpp_modules(
        _build_args(cc, quiet, keep, [entry, *modules], output, run=True),
        program_args,
    )


def test_project(project_root: Path, cc: Optional[str], quiet: bool = False,
                 keep: bool = False) -> int:
    from .cli import _build_cpp_modules

    entry, modules, _packages = collect_sources(project_root)
    tests_dir = project_root / "tests"
    test_files = sorted(tests_dir.rglob("*.ep")) if tests_dir.is_dir() else []
    if not test_files:
        print("epitaxy: no tests found (expected tests/*.ep)", file=sys.stderr)
        return 0
    target = project_root / "target"
    target.mkdir(parents=True, exist_ok=True)
    failed = 0
    for test in test_files:
        name = test.relative_to(tests_dir).with_suffix("").as_posix().replace("/", "::")
        if not quiet:
            print(f"epitaxy: test {name}", file=sys.stderr)
        code = _build_cpp_modules(
            _build_args(
                cc, quiet, keep, [test.resolve(), *modules],
                target / f"test-{test.stem}", run=True,
            )
        )
        if code == 0:
            print(f"test {name}: ok")
        else:
            print(f"test {name}: FAILED (exit {code})")
            failed += 1
    if failed:
        print(f"{failed} test(s) failed", file=sys.stderr)
        return 1
    print(f"all {len(test_files)} tests passed")
    return 0


def check_project(project_root: Path) -> int:
    from .codegen import Options
    from .compiler import transpile
    from .diag import CompileError
    from .parser import parse

    entry, modules, _packages = collect_sources(project_root)
    files = [*modules, entry]
    errors = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        try:
            tu = parse(source, str(path))
        except CompileError as exc:
            print(exc.render(), file=sys.stderr)
            errors += 1
            continue
        module_name = None
        imports: list[str] = []
        for item in tu.items:
            if item.__class__.__name__ == "ModuleDecl":
                module_name = item.name
            elif item.__class__.__name__ == "ImportDecl":
                imports.append(item.name)
        options = Options()
        if module_name is not None:
            options.cpp_module = module_name
        else:
            options.cpp_module_entry = True
        options.cpp_module_imports = imports
        try:
            transpile(source, str(path), options)
        except CompileError as exc:
            print(exc.render(), file=sys.stderr)
            errors += 1
    print(f"checked {len(files)} file(s)")
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# `epitaxy new`
# ---------------------------------------------------------------------------

MAIN_TEMPLATE = """main: () = {{
    epi::println("Hello from {name}!");
}}
"""


def new_project(name: str, parent: Optional[Path] = None) -> Path:
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
        raise ProjectError(
            f"invalid project name '{name}'; use letters, digits, '_' and '-'"
        )
    root = ((parent or Path.cwd()) / name).resolve()
    if root.exists():
        raise ProjectError(f"destination '{root}' already exists")
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Epitaxy.toml").write_text(
        "[package]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        "\n"
        "[dependencies]\n",
        encoding="utf-8",
    )
    (root / "src" / "main.ep").write_text(
        MAIN_TEMPLATE.format(name=name), encoding="utf-8"
    )
    (root / ".gitignore").write_text("/target/\n", encoding="utf-8")
    return root
