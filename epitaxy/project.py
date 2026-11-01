"""Cargo-like Epitaxy projects: manifests, workspaces, path/registry deps,
lockfiles, library targets and incremental builds.

Layout (same as Cargo, with .ep files)::

    myapp/
      Epitaxy.toml
      Epitaxy.lock           # generated
      src/main.ep            # entry point (bin target)
      src/greeting.ep        # `module greeting;`
      tests/smoke.ep         # each test file is a program that must exit 0
      target/                # build output + incremental cache

Manifest::

    [package]
    name = "myapp"
    version = "0.1.0"
    entry = "src/main.ep"          # optional (default)
    # [lib] path = "src/lib.ep"    # optional library target

    [dependencies]
    util = "../util"                         # path dependency
    util = { path = "../util" }              # or inline table
    greeter = { version = "0.2", registry = "local" }

Workspace::

    [workspace]
    members = ["app", "util"]

Dependencies and module names
-----------------------------
A dependency's ``src/**/*.ep`` modules are compiled together with the
dependent package and live in that package's module namespace: a module
``module math;`` inside package ``util`` becomes the C++ module
``epi_util_math``. Imports inside a package may be unqualified (same package,
``import math;``) or package-qualified for a direct dependency
(``import util::math;``). The declared C++ declarations still live in the
global C++ namespace, so cross-package *symbol* collisions are reported by the
C++ compiler rather than resolved automatically.

Registries: a registry is a directory containing ``<name>-<version>/``
package directories with ``Epitaxy.toml``. Search path comes from
``EPITAXY_REGISTRY`` (``os.pathsep`` separated), else ``$EPITAXY_HOME/registry``,
else ``~/.epitaxy/registry``. Version requirements support exact versions,
``^``, ``~`` and ``*``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from . import ast
from .diag import CompileError


# ---------------------------------------------------------------------------
# Minimal TOML subset (Python 3.10 has no tomllib)
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
    current: list[str] = []
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
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
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
        data.setdefault(section or "", {})[key.strip()] = _parse_value(value)
    return data


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def _parse_version(text: str) -> Optional[tuple[int, int, int]]:
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text).strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2) or 0), int(match.group(3) or 0))


def version_satisfies(version: str, requirement: str) -> bool:
    have = _parse_version(version)
    if have is None:
        return False
    req = (requirement or "*").strip()
    if req in ("*", ""):
        return True
    if req.startswith("^"):
        want = _parse_version(req[1:])
        return want is not None and have >= want and have[0] == want[0]
    if req.startswith("~"):
        want = _parse_version(req[1:])
        return want is not None and have >= want and have[:2] == want[:2]
    want = _parse_version(req)
    # Cargo-style: a bare version means caret-compatible.
    return want is not None and have >= want and have[0] == want[0]


def _version_key(version: str) -> tuple[int, int, int]:
    return _parse_version(version) or (0, 0, 0)


# ---------------------------------------------------------------------------
# Manifest / dependency model
# ---------------------------------------------------------------------------


class ProjectError(Exception):
    pass


@dataclass
class Dependency:
    name: str
    path: Optional[Path] = None
    version: Optional[str] = None
    registry: Optional[str] = None


@dataclass
class Manifest:
    root: Path
    name: Optional[str]
    version: str = "0.1.0"
    entry: str = "src/main.ep"
    lib: Optional[str] = None
    dependencies: dict[str, Dependency] = field(default_factory=dict)
    workspace_members: list[str] = field(default_factory=list)
    is_workspace: bool = False


@dataclass
class Package:
    root: Path
    manifest: Manifest
    source: str = "path"
    dependencies: dict[str, "Package"] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.manifest.name or self.root.name

    @property
    def version(self) -> str:
        return self.manifest.version

    def module_ident(self) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", self.name)


@dataclass
class Workspace:
    root: Path
    packages: dict[str, Package]
    default_package: Optional[str] = None


@dataclass
class LockEntry:
    name: str
    version: str
    source: str
    dependencies: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


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
    root = root.resolve()
    manifest_path = root / "Epitaxy.toml"
    if not manifest_path.is_file():
        raise ProjectError(f"no Epitaxy.toml found in {root}")
    data = parse_manifest_text(manifest_path.read_text(encoding="utf-8"))
    package = data.get("package", {})
    name = package.get("name")
    if name is not None and not isinstance(name, str):
        raise ProjectError(f"{manifest_path}: [package] name must be a string")
    dependencies: dict[str, Dependency] = {}
    for dep_name, spec in data.get("dependencies", {}).items():
        if isinstance(spec, dict):
            path_value = spec.get("path")
            version = spec.get("version")
            registry = spec.get("registry")
        else:
            path_value, version, registry = spec, None, None
        if path_value is not None:
            dependency = Dependency(name=dep_name, path=Path(str(path_value)))
            if not dependency.path.is_absolute():
                dependency.path = (root / dependency.path).resolve()
            dependencies[dep_name] = dependency
        else:
            dependencies[dep_name] = Dependency(
                name=dep_name,
                version=str(version) if version is not None else "*",
                registry=str(registry) if registry is not None else None,
            )
    lib = data.get("lib", {}).get("path") if isinstance(data.get("lib"), dict) else None
    workspace = data.get("workspace", {})
    members = workspace.get("members", []) if isinstance(workspace, dict) else []
    return Manifest(
        root=root,
        name=name if isinstance(name, str) and name else None,
        version=str(package.get("version", "0.1.0")),
        entry=str(package.get("entry", "src/main.ep")),
        lib=str(lib) if lib else None,
        dependencies=dependencies,
        workspace_members=[str(member) for member in members],
        is_workspace=bool(members),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def registry_dirs() -> list[Path]:
    override = os.environ.get("EPITAXY_REGISTRY")
    if override:
        return [Path(item).expanduser() for item in override.split(os.pathsep) if item]
    home = os.environ.get("EPITAXY_HOME")
    if home:
        return [Path(home).expanduser() / "registry"]
    return [Path.home() / ".epitaxy" / "registry"]


def registry_candidates(name: str, requirement: str) -> list[Path]:
    candidates: list[Path] = []
    for registry in registry_dirs():
        if not registry.is_dir():
            continue
        for child in sorted(registry.iterdir()):
            if not child.is_dir() or not (child / "Epitaxy.toml").is_file():
                continue
            try:
                manifest = load_manifest(child)
            except ProjectError:
                continue
            if manifest.name != name:
                continue
            if version_satisfies(manifest.version, requirement):
                candidates.append(child)
    candidates.sort(key=lambda path: _version_key(load_manifest(path).version), reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Lockfile
# ---------------------------------------------------------------------------


LOCK_NAME = "Epitaxy.lock"


def read_lock(root: Path) -> dict[str, dict]:
    path = root / LOCK_NAME
    if not path.is_file():
        return {}
    data = parse_manifest_text(path.read_text(encoding="utf-8"))
    entries = {}
    for section, values in data.items():
        if not section.startswith("package."):
            continue
        name = section[len("package.") :]
        entries[name] = {
            "version": str(values.get("version", "0.0.0")),
            "source": str(values.get("source", "")),
            "dependencies": values.get("dependencies", []),
        }
    return entries


def write_lock(root: Path, entries: dict[str, LockEntry]) -> None:
    lines = ["# Generated by Epitaxy. Do not edit.", "version = 1", ""]
    for name in sorted(entries):
        entry = entries[name]
        lines.append(f"[package.{name}]")
        lines.append(f'version = "{entry.version}"')
        lines.append(f'source = "{entry.source}"')
        if entry.dependencies:
            deps = ", ".join(f'"{dep}"' for dep in sorted(entry.dependencies))
            lines.append(f"dependencies = [{deps}]")
        lines.append("")
    (root / LOCK_NAME).write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Workspace/package resolution
# ---------------------------------------------------------------------------


class Resolver:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root
        self.by_root: dict[Path, Package] = {}
        self.by_name: dict[str, Package] = {}
        self.lock = read_lock(workspace_root)
        self.lock_entries: dict[str, LockEntry] = {}

    def _source_for(self, package: Package) -> str:
        if package.source.startswith("registry"):
            return package.source
        try:
            relative = package.root.relative_to(self.workspace_root)
            return f"path+{relative.as_posix()}"
        except ValueError:
            return f"path+{package.root}"

    def load_package(self, root: Path, source: str = "path") -> Package:
        root = root.resolve()
        if root in self.by_root:
            return self.by_root[root]
        manifest = load_manifest(root)
        if not manifest.name:
            raise ProjectError(f"{root / 'Epitaxy.toml'}: [package] name is required")
        existing = self.by_name.get(manifest.name)
        if existing is not None:
            if existing.root != root:
                raise ProjectError(
                    f"duplicate package '{manifest.name}' at {existing.root} and "
                    f"{root}; workspaces/registries must use unique package names"
                )
        package = Package(root=root, manifest=manifest, source=source)
        self.by_root[root] = package
        self.by_name[manifest.name] = package
        for dep_name, dependency in manifest.dependencies.items():
            if dependency.path is not None:
                dep_root = dependency.path
                if not (dep_root / "Epitaxy.toml").is_file():
                    raise ProjectError(
                        f"{root / 'Epitaxy.toml'}: dependency '{dep_name}' at "
                        f"{dep_root} has no Epitaxy.toml"
                    )
                dep_package = self.load_package(dep_root, "path")
            else:
                dep_package = self._load_registry_dependency(
                    dependency, f"{root / 'Epitaxy.toml'}"
                )
            package.dependencies[dep_name] = dep_package
            if dep_package.name != dep_name:
                # Allow renaming: the key is the import name for the dependency.
                pass
        self.lock_entries[package.name] = LockEntry(
            name=package.name,
            version=package.version,
            source=self._source_for(package),
            dependencies=[dep.name for dep in package.dependencies.values()],
        )
        return package

    def _load_registry_dependency(self, dependency: Dependency, context: str) -> Package:
        requirement = dependency.version or "*"
        locked = self.lock.get(dependency.name)
        if (
            locked is not None
            and version_satisfies(locked["version"], requirement)
        ):
            for registry in registry_dirs():
                candidate = registry / f"{dependency.name}-{locked['version']}"
                if (candidate / "Epitaxy.toml").is_file():
                    return self.load_package(candidate, f"registry+{locked['version']}")
        candidates = registry_candidates(dependency.name, requirement)
        if not candidates:
            searched = ", ".join(str(path) for path in registry_dirs())
            raise ProjectError(
                f"{context}: no registry package '{dependency.name}' satisfying "
                f"'{requirement}' (searched: {searched}). Set EPITAXY_REGISTRY to "
                "a registry directory."
            )
        selected = candidates[0]
        manifest = load_manifest(selected)
        return self.load_package(selected, f"registry+{manifest.version}")


def load_workspace(manifest_path: Optional[Path] = None) -> Workspace:
    manifest_path = manifest_path or find_manifest()
    if manifest_path is None:
        raise ProjectError("no Epitaxy.toml found in this directory or its parents")
    root = manifest_path.parent
    manifest = load_manifest(root)
    if not manifest.is_workspace:
        if not manifest.name:
            raise ProjectError(f"{manifest_path}: [package] name is required")
        resolver = Resolver(root)
        package = resolver.load_package(root, "path")
        return Workspace(root=root, packages={package.name: package})
    resolver = Resolver(root)
    packages: dict[str, Package] = {}
    for member in manifest.workspace_members:
        member_root = (root / member).resolve()
        if not (member_root / "Epitaxy.toml").is_file():
            raise ProjectError(
                f"{manifest_path}: workspace member '{member}' has no Epitaxy.toml"
            )
        member_manifest = load_manifest(member_root)
        if not member_manifest.name:
            raise ProjectError(
                f"{member_root / 'Epitaxy.toml'}: [package] name is required"
            )
        package = resolver.load_package(member_root, "path")
        packages[package.name] = package
    if manifest.name:
        package = resolver.load_package(root, "path")
        packages[package.name] = package
    return Workspace(root=root, packages=packages)


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------


@dataclass
class PackageSources:
    package: Package
    entry: Optional[Path]
    modules: dict[str, Path]  # module name -> file
    module_imports: dict[str, list[str]]  # module name -> raw imports


def package_sources(package: Package, require_target: bool = True) -> PackageSources:
    src = package.root / "src"
    if not src.is_dir():
        raise ProjectError(f"{package.root / 'Epitaxy.toml'}: src/ directory is missing")
    entry_candidate = (package.root / package.manifest.entry).resolve()
    entry = entry_candidate if entry_candidate.is_file() else None
    modules: dict[str, Path] = {}
    imports: dict[str, list[str]] = {}
    from .parser import parse

    for path in sorted(src.rglob("*.ep")):
        resolved = path.resolve()
        if entry is not None and resolved == entry:
            continue
        try:
            tu = parse(resolved.read_text(encoding="utf-8"), str(resolved))
        except CompileError as exc:
            raise ProjectError(exc.render())
        module_name = None
        raw_imports: list[str] = []
        for item in tu.items:
            if isinstance(item, ast.ModuleDecl):
                if module_name is not None:
                    raise ProjectError(f"{resolved}: more than one module declaration")
                module_name = item.name
            elif isinstance(item, ast.ImportDecl):
                raw_imports.append(item.name)
        if module_name is None:
            raise ProjectError(
                f"{resolved}: every non-entry source file must declare 'module name;'"
            )
        if module_name in modules:
            raise ProjectError(
                f"{package.root}: duplicate module '{module_name}' in "
                f"{modules[module_name]} and {resolved}"
            )
        modules[module_name] = resolved
        imports[module_name] = raw_imports
    if entry is None and package.manifest.lib is None and require_target:
        raise ProjectError(
            f"{package.root / 'Epitaxy.toml'}: no entry file '{package.manifest.entry}' "
            "and no [lib] target"
        )
    return PackageSources(package=package, entry=entry, modules=modules, module_imports=imports)


def module_qualified_name(package: Package, module: str) -> str:
    return f"{package.module_ident()}_{module}"


def resolve_import(import_name: str, package: Package, sources: dict[str, PackageSources]) -> str:
    if "::" in import_name:
        dep_name, module_name = import_name.split("::", 1)
        dependency = package.dependencies.get(dep_name)
        if dependency is None:
            raise ProjectError(
                f"{package.root}: import '{import_name}' names package '{dep_name}' "
                f"which is not a direct dependency of '{package.name}'"
            )
        dep_sources = sources.get(dependency.name)
        if dep_sources is None or module_name not in dep_sources.modules:
            raise ProjectError(
                f"{package.root}: package '{dep_name}' has no module '{module_name}'"
            )
        return module_qualified_name(dependency, module_name)
    if import_name not in package_sources_cache[package.name].modules:
        raise ProjectError(
            f"{package.root}: unknown module '{import_name}' (same-package import)"
        )
    return module_qualified_name(package, import_name)


# Filled by build/check entry points; simple module-level cache keeps the
# import resolver signature small.
package_sources_cache: dict[str, PackageSources] = {}

def _populate_sources(package: Package) -> None:
    global package_sources_cache
    package_sources_cache = {}
    seen: set[str] = set()

    def visit(current: Package, is_target: bool) -> None:
        if current.name in seen:
            return
        seen.add(current.name)
        package_sources_cache[current.name] = package_sources(
            current, require_target=is_target
        )
        for dependency in current.dependencies.values():
            visit(dependency, False)

    visit(package, True)


def ensure_lock(workspace: Workspace) -> None:
    write_lock(workspace.root, _lock_entries(workspace))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


@dataclass
class BuildContext:
    workspace: Workspace
    target: Path
    cache: Path
    cxx: str
    toolchain: str
    cxxflags: list[str]
    quiet: bool
    keep: bool

    def base_flags(self) -> list[str]:
        return [
            *self.cxxflags,
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Wno-unused-parameter",
            "-Wno-unused-label",
            "-I",
            str(Path(__file__).resolve().parent / "support"),
        ]

    def module_flags(self) -> list[str]:
        if self.toolchain == "gcc":
            return ["-fmodules-ts"]
        return ["-Wno-unused-command-line-argument"]


def _toolchain(cc: Optional[str]) -> tuple[str, str]:
    from .cli import _module_toolchain

    cxx, kind = _module_toolchain(SimpleNamespace(cc=cc))
    if cxx is None:
        raise ProjectError(
            "no C++ compiler with C++20 module support was found; pass --cc "
            "(GCC or upstream Clang)"
        )
    return cxx, kind


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _run(context: BuildContext, command: list[str], cwd: Path) -> bool:
    if not context.quiet:
        print("epitaxy: running " + " ".join(command), file=sys.stderr)
    return subprocess.run(command, cwd=str(cwd)).returncode == 0


def _compile_module(context: BuildContext, name: str, cpp: str,
                    dep_hashes: list[str]) -> tuple[Path, str, bool]:
    from .compiler import transpile

    key = _hash(
        cpp
        + "|"
        + context.cxx
        + "|"
        + " ".join(context.base_flags())
        + "|"
        + "|".join(sorted(dep_hashes))
    )
    obj = context.cache / f"{name}.o"
    pcm = context.cache / f"{name}.pcm"
    stamp = context.cache / f"{name}.hash"
    if (
        stamp.is_file()
        and obj.is_file()
        and stamp.read_text(encoding="utf-8").strip() == key
        and (context.toolchain == "gcc" or pcm.is_file())
    ):
        return obj, key, True
    cppm = context.cache / f"{name}.cppm"
    cppm.write_text(cpp, encoding="utf-8")
    if context.toolchain == "clang":
        command = [
            context.cxx, *context.base_flags(), "-x", "c++-module",
            "--precompile", str(cppm), "-o", str(pcm),
        ]
        if not _run(context, command, context.cache):
            raise ProjectError(f"precompiling module '{name}' failed")
        command = [
            context.cxx, *context.base_flags(), "-c", str(pcm), "-o", str(obj),
        ]
    else:
        command = [
            context.cxx, *context.base_flags(), *context.module_flags(),
            "-x", "c++", "-c", str(cppm), "-o", str(obj),
        ]
    if not _run(context, command, context.cache):
        raise ProjectError(f"compiling module '{name}' failed")
    stamp.write_text(key, encoding="utf-8")
    return obj, key, False


def _compile_entry(context: BuildContext, cpp: str, name: str,
                   dep_hashes: list[str]) -> tuple[Path, str, bool]:
    key = _hash(
        cpp
        + "|entry|"
        + context.cxx
        + "|"
        + " ".join(context.base_flags())
        + "|"
        + "|".join(sorted(dep_hashes))
    )
    obj = context.cache / f"{name}.o"
    stamp = context.cache / f"{name}.hash"
    if stamp.is_file() and obj.is_file() and stamp.read_text(encoding="utf-8").strip() == key:
        return obj, key, True
    cpp_path = context.cache / f"{name}.cpp"
    cpp_path.write_text(cpp, encoding="utf-8")
    command = [context.cxx, *context.base_flags(), *context.module_flags(),
               "-c", str(cpp_path), "-o", str(obj)]
    if not _run(context, command, context.cache):
        raise ProjectError("compiling the entry file failed")
    stamp.write_text(key, encoding="utf-8")
    return obj, key, False


def _generate_module_cpp(path: Path, module_name: str, imports: list[str]) -> str:
    from .codegen import Options
    from .compiler import transpile

    options = Options()
    options.cpp_module = module_name
    options.cpp_module_imports = imports
    return transpile(path.read_text(encoding="utf-8"), str(path), options)


def _generate_entry_cpp(path: Path, imports: list[str]) -> str:
    from .codegen import Options
    from .compiler import transpile

    options = Options()
    options.cpp_module_entry = True
    options.cpp_module_imports = imports
    return transpile(path.read_text(encoding="utf-8"), str(path), options)


def _scan_imports(path: Path) -> list[str]:
    from . import ast
    from .parser import parse

    tu = parse(path.read_text(encoding="utf-8"), str(path))
    return [item.name for item in tu.items if isinstance(item, ast.ImportDecl)]


def build_package(context: BuildContext, package: Package,
                  want_binary: bool = True) -> Optional[Path]:
    """Compile a package (and its dependency modules). Returns the bin/lib path."""
    global package_sources_cache
    _populate_sources(package)
    all_packages: dict[str, Package] = {}

    def visit(current: Package) -> None:
        if current.name in all_packages:
            return
        all_packages[current.name] = current
        for dependency in current.dependencies.values():
            visit(dependency)

    visit(package)

    # Compile all module sources in dependency closure.
    modules: dict[str, tuple[Package, Path, list[str]]] = {}
    for current in all_packages.values():
        sources = package_sources_cache[current.name]
        for module_name, path in sources.modules.items():
            qualified = module_qualified_name(current, module_name)
            raw_imports = sources.module_imports[module_name]
            resolved = [
                resolve_import(import_name, current, package_sources_cache)
                for import_name in raw_imports
            ]
            modules[qualified] = (current, path, resolved)

    hashes: dict[str, str] = {}
    objects: dict[str, Path] = {}
    remaining = dict(modules)
    compiled = 0
    cached = 0
    while remaining:
        progress = False
        for qualified in list(remaining):
            current, path, imports = remaining[qualified]
            if any(dep not in hashes for dep in imports):
                continue
            cpp = _generate_module_cpp(path, qualified, imports)
            dep_hashes = [hashes[dep] for dep in imports]
            obj, module_hash, was_cached = _compile_module(
                context, qualified, cpp, dep_hashes
            )
            hashes[qualified] = module_hash
            objects[qualified] = obj
            compiled += 1 if not was_cached else 0
            cached += 1 if was_cached else 0
            del remaining[qualified]
            progress = True
        if not progress:
            raise ProjectError(
                "module import cycle or unknown module among: "
                + ", ".join(sorted(remaining))
            )

    sources = package_sources_cache[package.name]
    artifact: Optional[Path] = None
    if sources.entry is not None and want_binary:
        entry_imports = [
            resolve_import(import_name, package, package_sources_cache)
            for import_name in _scan_imports(sources.entry)
        ]
        cpp = _generate_entry_cpp(sources.entry, entry_imports)
        entry_obj, entry_hash, entry_cached = _compile_entry(
            context, cpp, f"entry_{package.module_ident()}", [hashes[i] for i in entry_imports]
        )
        if not entry_cached:
            compiled += 1
        else:
            cached += 1
        binary = context.target / package.name
        link_needed = (not binary.is_file()) or (not entry_cached) or any(
            not path.is_file() for path in objects.values()
        )
        if link_needed:
            objects_list = [str(objects[name]) for name in sorted(objects)]
            command = [
                context.cxx, *context.cxxflags, "-std=c++20",
                *context.module_flags(), str(entry_obj), *objects_list,
                "-o", str(binary),
            ]
            if not _run(context, command, context.target):
                raise ProjectError(f"linking '{package.name}' failed")
        artifact = binary
    elif package.manifest.lib is not None or sources.entry is None:
        lib_name = package.module_ident()
        archive = context.target / f"lib{lib_name}.a"
        own_objects = [
            objects[module_qualified_name(package, module_name)]
            for module_name in sources.modules
        ]
        ar = os.environ.get("AR") or shutil.which("ar") or "ar"
        if not archive.is_file() or compiled or cached == 0:
            command = [ar, "rcs", str(archive), *[str(path) for path in own_objects]]
            if not _run(context, command, context.target):
                raise ProjectError(f"creating library '{archive.name}' failed")
        artifact = archive
    if not context.quiet:
        print(
            f"epitaxy: {package.name}: {compiled} compiled, {cached} cached",
            file=sys.stderr,
        )
    return artifact


# ---------------------------------------------------------------------------
# Helpers used by commands
# ---------------------------------------------------------------------------


def prepare_context(workspace: Workspace, cc: Optional[str], quiet: bool,
                    keep: bool) -> BuildContext:
    cxx, toolchain = _toolchain(cc)
    target = workspace.root / "target"
    target.mkdir(parents=True, exist_ok=True)
    cache = target / "epitaxy"
    cache.mkdir(parents=True, exist_ok=True)
    return BuildContext(
        workspace=workspace,
        target=target,
        cache=cache,
        cxx=cxx,
        toolchain=toolchain,
        cxxflags=shlex.split(os.environ.get("CXXFLAGS", "")),
        quiet=quiet,
        keep=keep,
    )


def _selected_packages(workspace: Workspace, package_name: Optional[str]) -> list[Package]:
    if package_name is None:
        return list(workspace.packages.values())
    if package_name not in workspace.packages:
        raise ProjectError(
            f"unknown package '{package_name}' (workspace has: "
            + ", ".join(sorted(workspace.packages))
            + ")"
        )
    return [workspace.packages[package_name]]


def build_workspace(workspace: Workspace, cc: Optional[str], quiet: bool = False,
                    keep: bool = False, package_name: Optional[str] = None) -> int:
    ensure_lock(workspace)
    context = prepare_context(workspace, cc, quiet, keep)
    for package in _selected_packages(workspace, package_name):
        build_package(context, package)
    write_lock(workspace.root, _lock_entries(workspace))
    return 0


def _lock_entries(workspace: Workspace) -> dict[str, LockEntry]:
    entries: dict[str, LockEntry] = {}

    def visit(package: Package) -> None:
        if package.name in entries:
            return
        entries[package.name] = LockEntry(
            name=package.name,
            version=package.version,
            source=package.source
            if package.source.startswith("registry")
            else f"path+{package.root}",
            dependencies=[dep.name for dep in package.dependencies.values()],
        )
        for dependency in package.dependencies.values():
            visit(dependency)

    for package in workspace.packages.values():
        visit(package)
    return entries


def run_workspace(workspace: Workspace, cc: Optional[str], quiet: bool = False,
                  keep: bool = False, package_name: Optional[str] = None,
                  program_args: Optional[list[str]] = None) -> int:
    ensure_lock(workspace)
    context = prepare_context(workspace, cc, quiet, keep)
    packages = _selected_packages(workspace, package_name)
    if package_name is None:
        bins = [
            package for package in packages
            if package_sources(package).entry is not None
        ]
        if not bins:
            raise ProjectError(
                "no binary package to run; use `epitaxy build` for libraries"
            )
        if len(bins) != 1:
            raise ProjectError(
                "`epitaxy run` needs -p <package> when the workspace has "
                f"{len(bins)} binary packages (bins: "
                + ", ".join(package.name for package in bins)
                + ")"
            )
        packages = bins
    package = packages[0]
    _populate_sources(package)
    sources = package_sources_cache[package.name]
    if sources.entry is None:
        raise ProjectError(f"package '{package.name}' has no binary entry (library)")
    build_package(context, package)
    binary = context.target / package.name
    program = subprocess.run([str(binary)] + list(program_args or []))
    return program.returncode


def test_workspace(workspace: Workspace, cc: Optional[str], quiet: bool = False,
                   keep: bool = False, package_name: Optional[str] = None) -> int:
    ensure_lock(workspace)
    context = prepare_context(workspace, cc, quiet, keep)
    failures = 0
    total = 0
    for package in _selected_packages(workspace, package_name):
        tests_dir = package.root / "tests"
        test_files = sorted(tests_dir.rglob("*.ep")) if tests_dir.is_dir() else []
        if not test_files:
            continue
        build_package(context, package)  # make sure modules are built
        _populate_sources(package)
        sources = package_sources_cache[package.name]
        for test in test_files:
            total += 1
            name = f"{package.name}::{test.relative_to(tests_dir).with_suffix('').as_posix()}"
            if not quiet:
                print(f"epitaxy: test {name}", file=sys.stderr)
            try:
                test_imports = [
                    resolve_import(import_name, package, package_sources_cache)
                    for import_name in _scan_imports(test)
                ]
                cpp = _generate_entry_cpp(test, test_imports)
                entry_obj, _hash_value, _cached = _compile_entry(
                    context, cpp, f"test_{package.module_ident()}_{test.stem}", []
                )
                modules = [
                    str(context.cache / f"{module_qualified_name(package if current is package else current, module_name)}.o")
                    for current, current_sources in [
                        (pkg, package_sources_cache[pkg.name])
                        for pkg in _dependency_closure(package)
                    ]
                    for module_name in current_sources.modules
                ]
                binary = context.target / f"test-{package.module_ident()}-{test.stem}"
                command = [
                    context.cxx, *context.cxxflags, "-std=c++20",
                    *context.module_flags(), str(entry_obj), *modules,
                    "-o", str(binary),
                ]
                if not _run(context, command, context.target):
                    raise ProjectError("linking test failed")
                program = subprocess.run([str(binary)])
                if program.returncode != 0:
                    print(f"test {name}: FAILED (exit {program.returncode})")
                    failures += 1
                else:
                    print(f"test {name}: ok")
            except ProjectError as exc:
                print(f"test {name}: FAILED ({exc})")
                failures += 1
    if total == 0:
        print("epitaxy: no tests found (expected tests/*.ep)")
        return 0
    if failures:
        print(f"{failures} test(s) failed", file=sys.stderr)
        return 1
    print(f"all {total} tests passed")
    return 0


def _dependency_closure(package: Package) -> list[Package]:
    seen: dict[str, Package] = {}

    def visit(current: Package) -> None:
        if current.name in seen:
            return
        seen[current.name] = current
        for dependency in current.dependencies.values():
            visit(dependency)

    visit(package)
    return list(seen.values())


def check_workspace(workspace: Workspace, package_name: Optional[str] = None) -> int:
    from .codegen import Options
    from .compiler import transpile

    packages = _selected_packages(workspace, package_name)
    global package_sources_cache
    for package in packages:
        _populate_sources(package)
    errors = 0
    total = 0
    for package in packages:
        sources = package_sources_cache[package.name]
        for module_name, path in sources.modules.items():
            total += 1
            imports = [
                resolve_import(import_name, package, package_sources_cache)
                for import_name in sources.module_imports[module_name]
            ]
            options = Options()
            options.cpp_module = module_qualified_name(package, module_name)
            options.cpp_module_imports = imports
            try:
                transpile(path.read_text(encoding="utf-8"), str(path), options)
            except CompileError as exc:
                print(exc.render(), file=sys.stderr)
                errors += 1
        if sources.entry is not None:
            total += 1
            imports = [
                resolve_import(import_name, package, package_sources_cache)
                for import_name in _scan_imports(sources.entry)
            ]
            options = Options()
            options.cpp_module_entry = True
            options.cpp_module_imports = imports
            try:
                transpile(
                    sources.entry.read_text(encoding="utf-8"),
                    str(sources.entry),
                    options,
                )
            except CompileError as exc:
                print(exc.render(), file=sys.stderr)
                errors += 1
    print(f"checked {total} file(s)")
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# `epitaxy new`
# ---------------------------------------------------------------------------

MAIN_TEMPLATE = """main: () = {{
    epi::println("Hello from {name}!");
}}
"""

LIB_TEMPLATE = """module {module};

pub: namespace = {{
    answer: () -> i32 = 42;
}}
"""


def new_project(name: str, parent: Optional[Path] = None,
                library: bool = False) -> Path:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
        raise ProjectError(
            f"invalid project name '{name}'; use letters, digits, '_' and '-'"
        )
    root = ((parent or Path.cwd()) / name).resolve()
    if root.exists():
        raise ProjectError(f"destination '{root}' already exists")
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    lines = [
        "[package]",
        f'name = "{name}"',
        'version = "0.1.0"',
    ]
    if library:
        module_name = re.sub(r"[^A-Za-z0-9_]", "_", name)
        lines.append("[lib]")
        lines.append('path = "src/lib.ep"')
        lines.append("")
        lines.append("[dependencies]")
        (root / "Epitaxy.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (root / "src" / "lib.ep").write_text(
            LIB_TEMPLATE.format(module=module_name), encoding="utf-8"
        )
    else:
        lines.append("")
        lines.append("[dependencies]")
        (root / "Epitaxy.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (root / "src" / "main.ep").write_text(
            MAIN_TEMPLATE.format(name=name), encoding="utf-8"
        )
    (root / ".gitignore").write_text("/target/\n", encoding="utf-8")
    return root
