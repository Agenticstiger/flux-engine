#!/usr/bin/env python3

# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The proof-material guard: find the proof-system backends this repository does not ship.

The engine ships the ``ProofSystem`` port and the one no-op implementation of it,
``NoProofSystem``, and nothing that proves anything. Proof-system backends are out of scope
for this repository (see CONTRIBUTING.md). The guard looks for them by the names of the seam,
by language and toolchain, and by dependency, so a file that moves is still found.

    python scripts/check_proof_material.py                  # the files git knows about
    python scripts/check_proof_material.py --walk --root D  # every file under directory D

It runs as ``make proof-guard``, as the ``Proof material`` job of the ``CI gate`` on every
pull request, and, imported by ``scripts/export_public_snapshot.py``, on every snapshot before
it is published. Exit codes: 0 nothing found; 1 something found; 2 the check could not run.

The guard is strict and it fails closed: see the comment above ``PROOF_PORT_BASES``. It reads
Python and pyproject.toml only (not requirements files, setup.cfg, notebooks or scripts without
a .py suffix). It goes by names, so a class that implements the port with no base, no telltale
name and no prove() or attest() method, and proof code in a language or under names it does
not know, are not found by it.

It is a tripwire, not a lock. A pull request runs its own copy of the workflow, the Makefile and
this script, so a maintainer's review of a change to any of them is still what protects the line.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The open-core line: proof-system backends are not part of this repository. This guard
#: FAILS a pull request that adds one and the export of a snapshot that holds one; the way
#: past it is to leave the material out, not to wave a warning through. It goes by the names
#: of the seam (the ProofSystem and FidelityProver ports, the one allowed NoProofSystem), by
#: language and toolchain (Cargo, Rust, Noir, zkVM guests) and by dependency, so a file that
#: moves is still found. A path is used only where the thing is a path: a spikes/ directory
#: (prototype work for a backend) and ADR 0007 (the decision record for one).
#:
#: It is strict and it fails closed. A test double that implements the port is reported like
#: any other implementation, a class that defines attest() or prove() is reported whatever it
#: is for (attest() is the prover port's method), the allowed NoProofSystem must stay a bare
#: name (the first real edit to it fails the check, so someone looks), a directory called
#: spikes/ is reported at any depth, and a
#: Python or pyproject.toml file it cannot read or parse is a finding too, because a file it
#: did not look at is not a file that is clean. (Run it with a Python at least as new as the
#: one the repository supports: a file that uses newer syntax than the interpreter knows is
#: reported as unread.)
PROOF_PORT_BASES: Tuple[str, ...] = ("ProofSystem", "FidelityProver")
PROOF_PORT_METHODS: Tuple[str, ...] = ("attest", "prove")
ALLOWED_PROOF_SYSTEM = "NoProofSystem"
ALLOWED_DECORATORS: Tuple[str, ...] = ("dataclass", "final")
ENUM_BASES: Tuple[str, ...] = ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag")
MOCK_NAMES: Tuple[str, ...] = (
    "Mock",
    "MagicMock",
    "AsyncMock",
    "NonCallableMock",
    "SimpleNamespace",
)
PROOF_DIR_NAMES: Tuple[str, ...] = ("spikes",)
PROOF_ADR_PATTERN = "docs/adr/0007*"
PROOF_FILE_NAMES: Tuple[str, ...] = (
    "cargo.toml",
    "cargo.lock",
    "rust-toolchain",
    "rust-toolchain.toml",
    "nargo.toml",
    "prover.toml",
    "verifier.toml",
)
PROOF_SUFFIXES: Tuple[str, ...] = (
    ".rs",
    ".nr",
    ".circom",
    ".cairo",
    ".zok",
    ".r1cs",
    ".zkey",
    ".vkey",
    ".elf",
)
#: Backend names in modules, packages and distributions, matched on the words of a name
#: (``Sp1Host`` and ``sp1-host`` are two words). A distinctive name counts at the start or end
#: of a word (``pyrisc0``, ``risc0host``) and across two words (``risc-zero``); a short or
#: ordinary one only as a whole word (``sp1-sdk`` yes, ``sp100`` and ``plonked`` no).
PROOF_BACKEND_STEMS: Tuple[str, ...] = (
    "risc0",
    "risczero",
    "nargo",
    "zkvm",
    "halo2",
    "groth16",
    "plonky",
)
PROOF_BACKEND_WORDS: Tuple[str, ...] = ("sp1", "noir", "plonk")
PROOF_BACKEND_PAIRS: Tuple[str, ...] = ("risczero",)  # risc-zero, risc_zero, RiscZero
#: Words that make an entry-point group one for proofs or provers: ``flux.proof_systems`` and
#: ``flux_engine.ProofSystems`` yes, ``markdown.proofreaders`` and ``flux.approvers`` no.
PROOF_ENTRY_WORDS: Tuple[str, ...] = (
    "proof",
    "proofs",
    "prover",
    "provers",
    "proofsystem",
    "proofsystems",
)


@dataclass(frozen=True)
class ProofFinding:
    kind: str
    path: str
    line: int
    detail: str


def _tokens(name: str) -> List[str]:
    """The lower-case words of a module or distribution name: ``Sp1Host`` and ``sp1-host``
    both give ``['sp1', 'host']``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [word for word in re.split(r"[^a-z0-9]+", spaced.lower()) if word]


def _is_backend_name(name: str) -> bool:
    words = _tokens(name)
    across = [first + second for first, second in zip(words, words[1:], strict=False)]
    return (
        any(
            word.startswith(stem) or word.endswith(stem)
            for word in words
            for stem in PROOF_BACKEND_STEMS
        )
        or any(pair in PROOF_BACKEND_PAIRS for pair in across)
        or any(word in PROOF_BACKEND_WORDS for word in words)
    )


def _name_of(node: Optional[ast.AST]) -> str:
    """The last identifier of a name: ``a.b.ProofSystem`` gives ``ProofSystem``,
    ``Generic[X]`` gives ``Generic`` and ``Foo()`` gives ``Foo``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _name_of(node.value)
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    return ""


def _constructed(value: Optional[ast.AST]) -> str:
    """The class a ``proof_system`` value builds: ``Foo()``, ``field(default_factory=Foo)`` and
    ``field(default=Foo())`` all give ``Foo``. Anything that is not a class (a lower-case
    factory function, a lambda, a plain name) gives an empty string."""
    if not isinstance(value, ast.Call):
        return ""
    built = _name_of(value.func)
    if built in ("field", "Field"):
        built = ""
        for keyword in value.keywords:
            if keyword.arg in ("default", "default_factory"):
                built = _name_of(keyword.value)
    return built if built[:1].isupper() else ""


def _port_aliases(tree: ast.AST) -> Dict[str, str]:
    """Other names for the port classes and for Protocol in this file: ``import ProofSystem as
    PS``, ``PS = ProofSystem``, ``PS: type = ProofSystem``, ``PS, Q = ProofSystem, 1`` and ``Q = PS``,
    whatever order they come in."""
    tracked = {*PROOF_PORT_BASES, ALLOWED_PROOF_SYSTEM, "Protocol"}
    alias: Dict[str, str] = {}
    nodes = list(ast.walk(tree))
    changed = True
    while changed:
        changed = False
        for node in nodes:
            found: List[Tuple[str, str]] = []
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for item in node.names:
                    original = item.name.split(".")[-1]
                    if item.asname and original in tracked:
                        found.append((item.asname, original))
            elif isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                source = _name_of(node.value)
                if source in tracked or source in alias:
                    original = alias.get(source, source)
                    found += [(_name_of(t), original) for t in node.targets if _name_of(t)]
            elif isinstance(node, ast.AnnAssign) and isinstance(
                node.value, (ast.Name, ast.Attribute)
            ):
                source = _name_of(node.value)
                if (source in tracked or source in alias) and _name_of(node.target):
                    found.append((_name_of(node.target), alias.get(source, source)))
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
                for target in node.targets:
                    if isinstance(target, ast.Tuple) and len(target.elts) == len(node.value.elts):
                        for bound, value in zip(target.elts, node.value.elts, strict=True):
                            source = _name_of(value)
                            if (source in tracked or source in alias) and _name_of(bound):
                                found.append((_name_of(bound), alias.get(source, source)))
            for name, original in found:
                if name not in alias:  # the first binding wins, so this ends
                    alias[name] = original
                    changed = True
    return alias


def _base_names(node: ast.expr) -> Set[str]:
    """The names a base-class expression can stand for: ``ProofSystem if c else object`` and
    ``a or ProofSystem`` give both branches, anything else gives its last identifier."""
    if isinstance(node, ast.IfExp):
        return _base_names(node.body) | _base_names(node.orelse)
    if isinstance(node, ast.BoolOp):
        return {name for value in node.values for name in _base_names(value)}
    name = _name_of(node)
    return {name} if name else set()


def _names_a_backend(value: Optional[ast.expr]) -> bool:
    """A string literal that is, or contains, a backend's name: ``name = "risc0"``."""
    return (
        isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and _is_backend_name(value.value)
    )


def _is_plain_allowed_class(node: ast.ClassDef) -> bool:
    """The one allowed ProofSystem is a name and nothing else: no base, no metaclass, no
    decorator but @dataclass, and a body of a docstring and literal fields, none of which
    names a backend."""
    if node.bases or node.keywords:
        return False
    if any(_name_of(decorator) not in ALLOWED_DECORATORS for decorator in node.decorator_list):
        return False
    for item in node.body:
        if isinstance(item, ast.Pass):
            continue
        if isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant):
            continue  # the docstring
        if isinstance(item, ast.AnnAssign) and (
            item.value is None or isinstance(item.value, ast.Constant)
        ):
            if _names_a_backend(item.value):
                return False
            continue
        if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant):
            if _names_a_backend(item.value):
                return False
            continue
        return False
    return True


def _python_proof_findings(relative: str, raw: bytes) -> List[ProofFinding]:
    """Raises if the file does not parse; the caller turns that into a finding. Parsed from
    bytes so that a byte-order mark and a coding line are handled the way Python does."""
    tree = ast.parse(raw)
    alias = _port_aliases(tree)
    ports = {*PROOF_PORT_BASES, ALLOWED_PROOF_SYSTEM}
    found: List[ProofFinding] = []

    def add(kind: str, line: int, detail: str) -> None:
        found.append(ProofFinding(kind, relative, line, detail))

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = {alias.get(name, name) for base in node.bases for name in _base_names(base)}
            if "Protocol" in bases:
                continue  # a port's own definition
            methods = [
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            port_methods = [method for method in methods if method in PROOF_PORT_METHODS]
            value_type = any(
                base.endswith(("Error", "Exception", "Warning")) or base in ENUM_BASES
                for base in bases
            )
            if node.name == ALLOWED_PROOF_SYSTEM:
                if _is_plain_allowed_class(node):
                    continue  # the one backend that is allowed: a name and nothing else
                add(
                    "ProofSystem implementation",
                    node.lineno,
                    f"class {node.name} is more than a name",
                )
            elif bases & ports:
                add("ProofSystem implementation", node.lineno, f"class {node.name}")
            elif value_type:
                continue  # an exception or an enum that happens to be named for the port
            elif node.name.endswith(("ProofSystem", "Prover")) and not node.name.startswith("Test"):
                add("ProofSystem implementation", node.lineno, f"class {node.name}")
            elif port_methods:
                detail = f"class {node.name} defines {port_methods[0]}()"
                add("ProofSystem implementation", node.lineno, detail)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == ALLOWED_PROOF_SYSTEM:
                add("ProofSystem implementation", node.lineno, f"def {node.name}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.keyword)):
            if isinstance(node, ast.Assign):
                targets = [_name_of(target) for target in node.targets]
            elif isinstance(node, ast.AnnAssign):
                targets = [_name_of(node.target)]
            else:
                targets = [node.arg or ""]
            if (
                ALLOWED_PROOF_SYSTEM in targets
                and isinstance(node, (ast.Assign, ast.AnnAssign))
                and node.value is not None
            ):
                add("ProofSystem implementation", node.lineno, f"{ALLOWED_PROOF_SYSTEM} rebound")
            built = _constructed(node.value)
            built = alias.get(built, built)
            if (
                "proof_system" in targets
                and built
                and built != ALLOWED_PROOF_SYSTEM
                and built not in MOCK_NAMES
            ):
                add("ProofSystem implementation", node.lineno, f"proof_system = {built}(...)")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for item in node.names:
                if item.asname == ALLOWED_PROOF_SYSTEM and item.name != ALLOWED_PROOF_SYSTEM:
                    add("ProofSystem implementation", node.lineno, f"{item.name} as {item.asname}")
            modules = [item.name for item in node.names]
            if isinstance(node, ast.ImportFrom):
                modules.insert(0, node.module or "")
            hit = next(
                (m for m in modules if any(_is_backend_name(part) for part in m.split("."))), None
            )
            if hit:
                add("proof backend import", node.lineno, hit)
        elif isinstance(node, ast.Call):
            called = _name_of(node.func)
            if called in ("type", "new_class") and len(node.args) >= 2:
                names = {
                    alias.get(_name_of(part), _name_of(part))
                    for part in ast.walk(node.args[1])
                    if isinstance(part, (ast.Name, ast.Attribute))
                }
                if names & ports:
                    detail = f"{called}(...) builds a class on the port"
                    add("ProofSystem implementation", node.lineno, detail)
            elif called in ("import_module", "__import__") and node.args:
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and any(_is_backend_name(part) for part in first.value.split("."))
                ):
                    add("proof backend import", node.lineno, first.value)
    return found


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _pyproject_proof_findings(relative: str, raw: bytes) -> List[ProofFinding]:
    """Raises if the file is not valid TOML; the caller turns that into a finding. Tables of
    an unexpected type are skipped, not crashed on."""
    data = tomllib.loads(raw.decode("utf-8-sig"))
    project = _as_dict(data.get("project"))
    requirements: List[Any] = list(_as_list(project.get("dependencies")))
    for group in _as_dict(project.get("optional-dependencies")).values():
        requirements += _as_list(group)
    for group in _as_dict(data.get("dependency-groups")).values():
        requirements += _as_list(group)
    tool_uv = _as_dict(_as_dict(data.get("tool")).get("uv"))
    requirements += _as_list(tool_uv.get("dev-dependencies"))
    requirements += list(_as_dict(tool_uv.get("sources")))  # the names it maps to a source
    requirements += _as_list(_as_dict(data.get("build-system")).get("requires"))
    tool = _as_dict(data.get("tool"))
    poetry = _as_dict(tool.get("poetry"))
    for table in (poetry, *_as_dict(poetry.get("group")).values()):
        for key in ("dependencies", "dev-dependencies"):
            requirements += list(_as_dict(_as_dict(table).get(key)))
    for group in _as_dict(_as_dict(tool.get("pdm")).get("dev-dependencies")).values():
        requirements += _as_list(group)
    for environment in _as_dict(_as_dict(tool.get("hatch")).get("envs")).values():
        requirements += _as_list(_as_dict(environment).get("dependencies"))
    found: List[ProofFinding] = []
    for requirement in requirements:
        name = re.match(r"[A-Za-z0-9_.-]+", requirement) if isinstance(requirement, str) else None
        if name and _is_backend_name(name.group(0)):
            found.append(ProofFinding("proof backend dependency", relative, 0, name.group(0)))
    for group_name, group in _as_dict(project.get("entry-points")).items():
        if any(word in PROOF_ENTRY_WORDS for word in _tokens(group_name)):
            found.append(ProofFinding("proof backend entry point", relative, 0, group_name))
            continue
        for entry, target in _as_dict(group).items():
            parts = re.split(r"[.:\[\],\s]+", target) if isinstance(target, str) else []
            if _is_backend_name(str(entry)) or any(_is_backend_name(part) for part in parts):
                found.append(
                    ProofFinding("proof backend entry point", relative, 0, f"{group_name}: {entry}")
                )
    return found


def _entries(root: Path, paths: Optional[Sequence[str]]) -> List[Path]:
    """The files and directories to look at, each directory before what is inside it."""
    if paths is None:
        return sorted(root.rglob("*"))
    chosen: Set[Path] = set()
    for relative in paths:
        pure = PurePosixPath(relative)
        chosen.add(root.joinpath(*pure.parts))
        chosen.update(root.joinpath(*parent.parts) for parent in pure.parents if parent.parts)
    return sorted(chosen)


def check_proof_material(root: Path, paths: Optional[Sequence[str]] = None) -> List[ProofFinding]:
    """Everything in ``root`` that is proof material. Empty means there is none.

    Without ``paths`` every file and directory under ``root`` is looked at, which is right for a
    directory that was just exported. With ``paths`` (relative and slash-separated, as
    ``git ls-files`` gives them) only those files and the directories that hold them are, so a
    working copy's ``.venv`` or build output is never read."""
    found: List[ProofFinding] = []
    adr_seen: Set[str] = set()
    flagged: List[str] = []  # directories already reported; what is inside is not counted again
    for path in _entries(root, paths):
        relative = path.relative_to(root).as_posix()
        if any(relative.startswith(parent + "/") for parent in flagged):
            continue
        parts = PurePosixPath(relative).parts
        name = parts[-1].lower()
        is_dir = path.is_dir() and not path.is_symlink()
        if name in PROOF_DIR_NAMES and (is_dir or path.is_symlink()):
            flagged.append(relative)
            found.append(ProofFinding("proof spike directory", relative, 0, f"{parts[-1]}/"))
            continue
        if is_dir and _is_backend_name(parts[-1]):
            flagged.append(relative)
            found.append(ProofFinding("proof backend module", relative, 0, f"{parts[-1]}/"))
            continue
        head = "/".join(part.lower() for part in parts[:3])
        if (
            len(parts) >= 3
            and fnmatch.fnmatchcase(head, PROOF_ADR_PATTERN)
            and head not in adr_seen
        ):
            adr_seen.add(head)
            found.append(ProofFinding("proof-spike ADR", "/".join(parts[:3]), 0, PROOF_ADR_PATTERN))
        if is_dir:
            continue
        suffix = PurePosixPath(name).suffix
        is_python = suffix in (".py", ".pyi")
        if is_python and _is_backend_name(parts[-1][: -len(suffix)]):
            found.append(ProofFinding("proof backend module", relative, 0, parts[-1]))
        if name in PROOF_FILE_NAMES or suffix in PROOF_SUFFIXES:
            found.append(ProofFinding("Cargo, Rust, Noir or zkVM source", relative, 0, parts[-1]))
        if path.is_symlink() or not path.is_file():
            continue
        if is_python or name == "pyproject.toml":
            scan = _python_proof_findings if is_python else _pyproject_proof_findings
            try:
                found += scan(relative, path.read_bytes())
            except Exception as exc:  # fail closed: a file that was not checked is not clean
                version = f"{sys.version_info[0]}.{sys.version_info[1]}"
                detail = f"{type(exc).__name__} under Python {version}"
                found.append(ProofFinding("could not be checked", relative, 0, detail))
    return found


def _known_files(root: Path) -> List[str]:
    """The files git knows under ``root``: the tracked ones, plus new ones that are not ignored.
    That is what a ``git add -A`` would put in a commit, so ``.venv`` and build output are not
    in it. A tracked file that was deleted from the working copy is left out."""
    done = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode(errors="replace").strip() or "git ls-files failed")
    names = {name.decode("utf-8", "surrogateescape") for name in done.stdout.split(b"\0") if name}
    return sorted(name for name in names if os.path.lexists(root.joinpath(*name.split("/"))))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if the repository holds proof-system backend material."
    )
    parser.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="the directory to check (default: this one)"
    )
    parser.add_argument(
        "--walk",
        action="store_true",
        help="look at every file under --root, not only the files git knows (for an export)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"check_proof_material: {args.root} is not a directory", file=sys.stderr)
        return 2
    paths: Optional[List[str]] = None
    if not args.walk:
        try:
            paths = _known_files(root)
        except (OSError, RuntimeError) as exc:
            print(
                f"check_proof_material: could not list the files git knows ({exc}); "
                "pass --walk to look at every file under the directory",
                file=sys.stderr,
            )
            return 2
        if not paths:  # an ignored directory, or an empty one: nothing was looked at
            print(
                f"check_proof_material: git knows no files under {args.root}; "
                "pass --walk to look at every file there",
                file=sys.stderr,
            )
            return 2
    found = check_proof_material(root, paths)
    if not found:
        print(f"no proof material in {'every file' if paths is None else f'{len(paths)} files'}")
        return 0
    print(f"proof material found ({len(found)}):")
    for item in found:
        where = f"{item.path}:{item.line}" if item.line else item.path
        print(f"  {item.kind}: {where}  {item.detail}")
    print(
        "\nThis repository ships the ProofSystem port and the no-op NoProofSystem, nothing that "
        "proves anything. Proof-system backends are out of scope here: see CONTRIBUTING.md."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
