# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Architecture gates: the import-linter contracts, plus a kernel AST scan.

Two independent checks of the same "kernel packages stay deterministic"
property:

1. The import-linter contracts in pyproject.toml (run as a subprocess --
   the same `lint-imports` entry point `make ci-local` and CI use).
2. An AST scan asserting the kernel packages (core, world, runtime, expr,
   seam) never call the wall clock, a UUID generator, or the stdlib
   `random` module -- the "six deliberately broken kernels" idea
   names exactly these as the ways a kernel silently loses
   determinism (wall clock, uuid4 ids, ...). import-linter's forbidden
   contract already bans importing the `random`/`uuid`/`time`/`os`/
   `socket` *modules* from the kernel; this test is an independent,
   call-site-level check that a kernel module can't route around that by
   importing the *function* instead of the module (e.g.
   ``from random import choice``) or by reaching a banned module via an
   attribute access the import graph doesn't see as an import.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_PACKAGES = ("core", "world", "runtime", "expr", "seam")

# Dotted call paths that are never allowed in a kernel module, matched on
# the whole chain (`ast.unparse(func)`), so `datetime.datetime.now()` --
# whose func.value is an Attribute, not a Name -- is seen as well as
# `time.time()`.
_BANNED_CALLS = {
    "datetime.now",
    "datetime.utcnow",
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "datetime.date.today",
    "time.time",
    "time.monotonic",
    "time.perf_counter",
    "uuid.uuid4",
    "uuid.uuid1",
    "os.urandom",
    "secrets.token_bytes",
    "secrets.token_hex",
}
# Every attribute of these reads process entropy, mutates global RNG state
# or reads the wall clock, so any call on them is banned.
_BANNED_MODULES_ANY_ATTR = {"random", "secrets"}
# KERNEL-SPEC §4's first normative sentence: "numpy is used **only** as the
# bit source (`bit_generator.random_raw()`); no numpy distribution function
# is called on any digest path." Nothing enforced it. The rule holds today
# by construction -- `core/rng.py` keeps only `generator.bit_generator` --
# but `numpy>=1.26,<3` is unpinned and the only job that runs the
# full-pipeline golden is one ubuntu/3.12 cell, so a distribution call
# reintroduced by a later edit would move run digests on a numpy upgrade
# with no gate positioned to catch it.
#
# Matched on the *final attribute* of any call chain, because the receiver
# is a variable (`stream.normal(...)`, `self._generator.uniform(...)`) and
# no scan can know its type. That is safe here: none of these names is
# called as a method anywhere in the five kernel packages, and the two
# numpy attributes §4 does allow -- `bit_generator` and `random_raw` -- are
# not among them.
_BANNED_DISTRIBUTION_METHODS = frozenset(
    {
        "beta",
        "binomial",
        "bytes",
        "chisquare",
        "choice",
        "default_rng",
        "dirichlet",
        "exponential",
        "gamma",
        "geometric",
        "gumbel",
        "hypergeometric",
        "integers",
        "laplace",
        "logistic",
        "lognormal",
        "multinomial",
        "multivariate_normal",
        "negative_binomial",
        "noncentral_chisquare",
        "normal",
        "pareto",
        "permutation",
        "permuted",
        "poisson",
        "power",
        "randint",
        "random",
        "random_sample",
        "rayleigh",
        "shuffle",
        "standard_cauchy",
        "standard_exponential",
        "standard_gamma",
        "standard_normal",
        "standard_t",
        "triangular",
        "uniform",
        "vonmises",
        "wald",
        "weibull",
        "zipf",
    }
)
# `from <module> import <name>` binds a bare name that no attribute-chain
# match can see -- the exact route the module docstring says this scan
# exists to close. Per source module, the names whose *call* is banned;
# `"*"` bans every name the module exports. Constructors are deliberately
# absent: `from datetime import datetime, timedelta` is how the kernel
# builds exact instants from declared inputs, and `datetime.now()` on an
# imported class is still caught by the attribute-chain rule above.
_BANNED_IMPORTED_FUNCTIONS: Dict[str, frozenset] = {
    "random": frozenset({"*"}),
    "secrets": frozenset({"*"}),
    "uuid": frozenset({"uuid1", "uuid4"}),
    "time": frozenset({"time", "time_ns", "monotonic", "monotonic_ns", "perf_counter"}),
    "os": frozenset({"urandom", "getenv", "getpid"}),
    # `from numpy.random import PCG64, Generator, SeedSequence` is exactly
    # how §3 says the seed tree is built, so the constructors stay
    # importable; the distribution functions do not.
    "numpy.random": _BANNED_DISTRIBUTION_METHODS,
    "numpy": frozenset({"random"}),
}


def _lint_imports_executable() -> str:
    """Resolve the ``lint-imports`` console script next to this interpreter.

    Using ``sys.executable``'s own directory (rather than relying on
    ``PATH``) means this test finds the right ``lint-imports`` even when
    invoked as ``.venv/bin/pytest`` without the venv activated.
    """
    candidate = Path(sys.executable).with_name("lint-imports")
    return str(candidate) if candidate.exists() else "lint-imports"


@pytest.mark.unit
def test_import_linter_contracts_pass() -> None:
    result = subprocess.run(
        [_lint_imports_executable()],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class _CallVisitor(ast.NodeVisitor):
    """Flag a kernel module's non-deterministic calls, however they are spelled.

    Three spellings, because the module docstring promises to catch all
    three and the attribute-on-a-Name form catches only the first:

    * ``random.choice(...)`` -- an attribute chain rooted at a module name;
    * ``datetime.datetime.now(...)`` -- an attribute chain two deep, whose
      ``func.value`` is itself an ``Attribute`` and not a ``Name``;
    * ``choice(...)`` after ``from random import choice`` -- a bare
      ``ast.Name`` call with no module in sight, which is why the visitor
      collects the names ``ImportFrom`` binds before it looks at calls.

    :meth:`visit` must be called on a whole module; :meth:`scan` does that
    in the right order.
    """

    def __init__(self) -> None:
        self.violations: List[str] = []
        self._imported_names: Dict[str, str] = {}

    def scan(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in _BANNED_IMPORTED_FUNCTIONS:
                banned = _BANNED_IMPORTED_FUNCTIONS[node.module]
                for alias in node.names:
                    if "*" in banned or alias.name in banned:
                        self._imported_names[alias.asname or alias.name] = (
                            f"{node.module}.{alias.name}"
                        )
        self.visit(tree)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            origin = self._imported_names.get(func.id)
            if origin is not None:
                self.violations.append(f"{func.id}(...) [from {origin}]")
        elif isinstance(func, ast.Attribute):
            dotted = ast.unparse(func)
            root = dotted.split(".", 1)[0]
            if root in _BANNED_MODULES_ANY_ATTR or dotted in _BANNED_CALLS:
                self.violations.append(f"{dotted}(...)")
            elif func.attr in _BANNED_DISTRIBUTION_METHODS:
                self.violations.append(f"{dotted}(...) [numpy distribution call]")
        self.generic_visit(node)


def _iter_kernel_files():
    for package in KERNEL_PACKAGES:
        package_dir = REPO_ROOT / "flux_engine" / package
        if package_dir.is_dir():
            yield from sorted(package_dir.rglob("*.py"))


@pytest.mark.unit
def test_kernel_packages_never_call_the_wall_clock_uuid_or_random() -> None:
    violations: Dict[str, List[str]] = {}
    for file_path in _iter_kernel_files():
        tree = ast.parse(file_path.read_text(), filename=str(file_path))
        visitor = _CallVisitor()
        visitor.scan(tree)
        if visitor.violations:
            violations[str(file_path.relative_to(REPO_ROOT))] = visitor.violations
    assert not violations, f"non-deterministic calls found in kernel packages: {violations}"


#: The seven spellings the previous visitor could not see. Kept as source
#: rather than as a fixture file so the negative control is readable
#: beside the check it controls.
_NON_DETERMINISM_CONTROL = """
import datetime
import os
import secrets
import time
import uuid
from random import choice
from time import monotonic


def f():
    a = choice([1, 2])
    b = monotonic()
    c = datetime.datetime.now()
    d = datetime.datetime.utcnow()
    e = secrets.token_hex()
    g = os.urandom(32)
    h = uuid.uuid4()
    return a, b, c, d, e, g, h
"""


@pytest.mark.unit
def test_the_kernel_scan_sees_every_spelling_of_a_non_deterministic_call() -> None:
    """The negative control: the guard must go red on code that routes around it.

    A guard that cannot fire is worse than no guard, and this one could
    not: it matched only ``<Name>.<attr>(...)``, so a bare call from
    ``from random import choice`` and a two-deep chain like
    ``datetime.datetime.now()`` were both invisible. Seven spellings, all
    of which used to pass.
    """
    visitor = _CallVisitor()
    visitor.scan(ast.parse(_NON_DETERMINISM_CONTROL))
    assert len(visitor.violations) == 7, visitor.violations


#: Five spellings of KERNEL-SPEC §4's banned "numpy distribution function
#: on a digest path" -- through the module, through a variable holding a
#: Generator, and through a bare name bound by `from numpy.random import`.
_NUMPY_DISTRIBUTION_CONTROL = """
import numpy as np
from numpy.random import normal


def f(stream, generator):
    a = np.random.default_rng(7)
    b = np.random.normal(0, 1)
    c = stream.uniform(0, 1)
    d = generator.integers(0, 10)
    e = normal(0, 1)
    return a, b, c, d, e
"""


@pytest.mark.unit
def test_the_kernel_scan_sees_a_numpy_distribution_call() -> None:
    """The negative control for KERNEL-SPEC §4's first normative rule.

    Five spellings, none of which the scan could see before: the rule was
    stated in a docstring and enforced by nothing, while the clock, uuid,
    `random`, `secrets` and `os.urandom` each had a guard and a control.
    """
    visitor = _CallVisitor()
    visitor.scan(ast.parse(_NUMPY_DISTRIBUTION_CONTROL))
    assert len(visitor.violations) == 5, visitor.violations


@pytest.mark.unit
def test_the_kernel_scan_allows_the_bit_source_numpy_is_used_for() -> None:
    """The positive control: §4's *permitted* numpy use must stay green.

    A guard that also banned ``bit_generator.random_raw()`` would ban the
    one numpy call the kernel is built on, so the exemption is asserted
    rather than assumed.
    """
    source = """
from numpy.random import PCG64, Generator, SeedSequence


def build(entropy):
    generator = Generator(PCG64(SeedSequence(entropy)))
    return int(generator.bit_generator.random_raw())
"""
    visitor = _CallVisitor()
    visitor.scan(ast.parse(source))
    assert visitor.violations == []


@pytest.mark.unit
def test_the_kernel_scan_does_not_fire_on_deterministic_code() -> None:
    """The positive control: ordinary kernel-shaped code stays green."""
    source = """
from decimal import Decimal
from fractions import Fraction


def f(rows):
    total = Decimal(0)
    for row in sorted(rows):
        total += Decimal(row)
    return Fraction(total)
"""
    visitor = _CallVisitor()
    visitor.scan(ast.parse(source))
    assert visitor.violations == []


# --- hashlib containment (KERNEL-SPEC §2's heading) ------------------------

#: KERNEL-SPEC §2's heading declares ``core/canonical.py`` "the only module
#: that imports rfc8785 AND hashlib". Only the ``rfc8785`` half has an
#: import-linter contract; the ``hashlib`` half had none, and the rule was
#: already broken -- ``evidence/pack.py`` imported ``hashlib`` inside a
#: helper and computed its own sha256 over a directory tree. A second
#: hashing site in shipped code is exactly what the containment rule exists
#: to prevent: it can disagree with the canonicaliser about framing,
#: ordering and encoding with no gate noticing.
_HASHLIB_ALLOWED = {"flux_engine/core/canonical.py"}


@pytest.mark.unit
def test_only_core_canonical_imports_hashlib() -> None:
    offenders: Dict[str, List[str]] = {}
    engine_root = REPO_ROOT / "flux_engine"
    for path in sorted(engine_root.rglob("*.py")):
        # The vendored flux_spec copy is byte-for-byte upstream code and is
        # out of scope for this repo's own kernel discipline, exactly as it
        # is for the rfc8785 contract in pyproject.toml.
        if "_vendor" in path.parts or "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel in _HASHLIB_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == "hashlib" for a in node.names):
                found.append(f"line {node.lineno}: import hashlib")
            if isinstance(node, ast.ImportFrom) and node.module == "hashlib":
                found.append(f"line {node.lineno}: from hashlib import ...")
        if found:
            offenders[rel] = found
    assert not offenders, offenders


@pytest.mark.unit
def test_the_hashlib_containment_check_can_fire() -> None:
    """The negative control: the check must see an import when there is one."""
    tree = ast.parse("import hashlib\nfrom hashlib import sha256\n")
    found = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Import) and any(a.name == "hashlib" for a in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "hashlib")
    ]
    assert len(found) == 2
