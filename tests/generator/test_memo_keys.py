# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A memo kept per process under a plan's digest is also keyed by what the run sets.

A plan's digest leaves out the fields a run sets and a build reads (what an ``end`` share is a
share of, the period range a projection is cut at, the periodic rows' budget). A memo keyed on the
digest alone serves one run's layout to another run of the same digest.
:func:`~flux_engine.generator.relations.run_set_key` holds those fields in one place; this scan
fails for a key under ``flux_engine/`` that holds a plan's digest and not that helper.

What it reads, in every module under ``flux_engine/``:

* a ``<plan>.digest().value`` anywhere must sit in a tuple that also calls ``run_set_key``, unless
  the use is listed in :data:`RECORDS` as a record and no memo key;
* a ``<plan>.digest()`` with no ``.value``, and a call of a helper of ``core/digest.py``
  (``digest_of_document`` and the others), must do so wherever it is a key: in a tuple, a subscript,
  a dict key, the argument of ``get`` / ``pop`` / ``setdefault``, a tuple a ``*key*`` function
  returns, or held in a variable that is;
* the first argument of ``run_set_key`` must be the plan whose digest sits in the same tuple (the
  receiver of the ``.digest()``), unless :data:`PAIRED_DIFFERENTLY` says why not;
* a function under ``lru_cache`` / ``cache`` may take no plan (a parameter named ``plan``, or one
  annotated ``GeneratorPlan`` or ``HistoryPick``), unless :data:`LRU_PLAN_PARAMETERS` says why not.

The rules run on source text, so each is shown below to catch a mutant of the real source.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set, Tuple

import pytest

pytestmark = pytest.mark.unit

ENGINE = Path(__file__).resolve().parents[2] / "flux_engine"

Site = Tuple[str, str]

#: Uses of a digest that are no memo key, as ``(path under flux_engine, function)`` with the reason.
RECORDS: Dict[Site, str] = {
    (
        "generator/rows.py",
        "as_document",
    ): "the plan's own document: a record of the digest, no memo",
    ("generator/projections.py", "as_document"): "the projection member of a plan's document",
    ("stages/plan.py", "_fidelity_summary"): "plan.json's record of the generator digest, no memo",
}

#: Keys whose ``run_set_key`` is made over another plan than the one whose digest sits beside it.
PAIRED_DIFFERENTLY: Dict[Site, str] = {}

#: Functions under ``lru_cache`` that take a plan, with the reason a plan is no part of their key.
LRU_PLAN_PARAMETERS: Dict[Site, str] = {}

#: The memos that must be keyed by it, as ``(path, function)``: the scan below finds each of them.
MEMO_SITES = {
    ("generator/events.py", "layout_of"),
    ("generator/parent_bounds.py", "deferred_bound"),
    ("generator/periods.py", "layout_of"),
    ("generator/projections.py", "_whole"),
    ("generator/projections.py", "sources_of"),
    ("generator/relations.py", "_layout"),
    ("generator/relations.py", "_history_of"),
    ("generator/relations.py", "_thresholds"),
    ("generator/rows.py", "_whole_rows"),
    ("generator/versionview.py", "view_of"),
}

#: A hash object's ``.digest()`` is bytes: no plan's.
_HASHES = {"sha1", "sha256", "sha512", "md5", "blake2b", "blake2s", "new"}
_NOT_HELPERS = {"require_same_domain", "require_domain", "digests_match"}
_CACHES = {"lru_cache", "cache"}
_PLAN_TYPES = ("GeneratorPlan", "HistoryPick")
_KEY_BUILDERS = ("key", "identity", "held")
_LOOKUPS = {"get", "pop", "setdefault", "__getitem__", "__setitem__", "__contains__"}


def _helpers_of(source: str) -> Set[str]:
    """The public functions of ``core/digest.py`` other than its checks: what makes a digest."""
    return {
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_")
        and node.name not in _NOT_HELPERS
    }


HELPERS = _helpers_of((ENGINE / "core" / "digest.py").read_text("utf-8"))


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


@dataclass
class Use:
    path: str
    function: str
    kind: str  # "value" (``.digest().value``), "object" (``.digest()``) or "helper"
    keyed: bool


@dataclass
class Scan:
    uses: List[Use] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def keyed_sites(self) -> Set[Site]:
        return {(use.path, use.function) for use in self.uses if use.keyed}


def _run_set_calls(node: ast.AST) -> List[ast.Call]:
    return [
        inner
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and _name(inner.func) == "run_set_key"
    ]


def _is_digest_call(node: ast.AST) -> bool:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "digest"
        and not node.args
        and not node.keywords
    ):
        return False
    receiver = node.func.value
    return not (isinstance(receiver, ast.Call) and _name(receiver.func) in _HASHES)


def _scan_module(path: str, source: str, found: Scan) -> None:
    tree = ast.parse(source)
    parents: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def function_of(node: ast.AST) -> str:
        up = parents.get(node)
        while up is not None:
            if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return up.name
            up = parents.get(up)
        return ""

    def key_of(node: ast.AST) -> Optional[ast.Tuple]:
        """The innermost tuple around *node* that calls ``run_set_key``."""
        up = parents.get(node)
        while up is not None:
            if isinstance(up, ast.Tuple) and _run_set_calls(up):
                return up
            up = parents.get(up)
        return None

    def in_key(node: ast.AST) -> bool:
        """Whether *node* is used as, or in, a key: it goes no further than the call that holds it."""
        child, up = node, parents.get(node)
        while up is not None:
            if isinstance(up, ast.Tuple):
                # A tuple returned is a record, unless the function that returns it builds a key.
                return not isinstance(parents.get(up), ast.Return) or any(
                    word in function_of(up) for word in _KEY_BUILDERS
                )
            if isinstance(up, ast.Subscript) and child is up.slice:
                return True
            if isinstance(up, ast.Dict) and child in up.keys:
                return True
            if isinstance(up, ast.Compare) and any(
                isinstance(op, (ast.In, ast.NotIn)) for op in up.ops
            ):
                return True
            if isinstance(up, ast.Call):
                return _name(up.func) in _LOOKUPS and child in up.args
            if isinstance(up, (ast.stmt, ast.Lambda)):
                return False
            child, up = up, parents.get(up)
        return False

    def assigned_to(node: ast.AST) -> List[str]:
        """The names *node*'s expression is assigned to, when the statement is a plain assignment."""
        up = parents.get(node)
        while up is not None and not isinstance(up, (ast.stmt, ast.Lambda)):
            up = parents.get(up)
        if isinstance(up, ast.Assign):
            return [target.id for target in up.targets if isinstance(target, ast.Name)]
        if isinstance(up, ast.AnnAssign) and isinstance(up.target, ast.Name):
            return [up.target.id]
        return []

    receivers: Dict[ast.Tuple, Set[str]] = {}
    held: Dict[Tuple[str, str], Set[str]] = {}
    for node in ast.walk(tree):
        up = parents.get(node)
        wrapped = isinstance(up, ast.Attribute) and up.attr in {"value", "hex"}
        top: Optional[ast.AST] = None
        receiver: Optional[str] = None
        kind = ""
        if _is_digest_call(node):
            assert isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            receiver = ast.dump(node.func.value)
            if wrapped:
                if isinstance(up, ast.Attribute) and up.attr == "value":
                    top, kind = up, "value"
            else:
                top, kind = node, "object"
        elif isinstance(node, ast.Call) and _name(node.func) in HELPERS and not wrapped:
            top, kind = node, "helper"
        if top is None:
            continue
        function = function_of(top)
        key = key_of(top)
        found.uses.append(Use(path, function, kind, key is not None))
        if key is not None and receiver is not None:
            receivers.setdefault(key, set()).add(receiver)
        site = (path, function)
        if site in RECORDS:
            continue
        where = f"{path}:{top.lineno} in {function or '<module>'}()"
        if kind == "value" and key is None:
            found.problems.append(
                f"{where}: a digest().value outside a key that holds run_set_key(...); "
                "add it, or list the use in RECORDS with a reason"
            )
        elif kind != "value" and key is None and in_key(top):
            what = "a digest() with no .value" if kind == "object" else "a digest helper's result"
            found.problems.append(f"{where}: a memo key holds {what} and no run_set_key(...)")
        elif kind != "value":
            held.setdefault(site, set()).update(assigned_to(top))
    for node in ast.walk(tree):
        site = (path, function_of(node))
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in held.get(site, set())
            and in_key(node)
            and key_of(node) is None
        ):
            found.problems.append(
                f"{path}:{node.lineno} in {site[1] or '<module>'}(): "
                f"{node.id!r} holds a digest and is a memo key with no run_set_key(...)"
            )
    for call in _run_set_calls(tree):
        site = (path, function_of(call))
        key = key_of(call)
        if site in PAIRED_DIFFERENTLY or not call.args:
            continue
        if key is None or ast.dump(call.args[0]) not in receivers.get(key, set()):
            found.problems.append(
                f"{path}:{call.lineno} in {site[1] or '<module>'}(): "
                f"run_set_key({ast.unparse(call.args[0])}) is not made over the plan whose digest "
                "sits in the same key; pair them, or list the key in PAIRED_DIFFERENTLY"
            )
    _scan_caches(path, tree, found)


def _cached(function: ast.FunctionDef) -> bool:
    return any(
        _name(decorator.func if isinstance(decorator, ast.Call) else decorator) in _CACHES
        for decorator in function.decorator_list
    )


def _plan_parameters(function: ast.FunctionDef) -> List[str]:
    names = []
    for argument in (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    ):
        annotation = "" if argument.annotation is None else ast.unparse(argument.annotation)
        if (
            argument.arg == "plan"
            or argument.arg.endswith("_plan")
            or any(kind in annotation for kind in _PLAN_TYPES)
        ):
            names.append(argument.arg)
    return names


def _scan_caches(path: str, tree: ast.Module, found: Scan) -> None:
    """A cache over a plan is keyed by the plan's fields, not by its digest: see :data:`LRU_PLAN_PARAMETERS`."""
    defined = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    cached = [node for node in defined.values() if _cached(node)]
    for node in ast.walk(tree):
        # ``cache(self._build)`` wraps a function by name, as a decorator would.
        if (
            isinstance(node, ast.Call)
            and _name(node.func) in _CACHES
            and node.args
            and _name(node.args[0]) in defined
        ):
            cached.append(defined[_name(node.args[0])])
    for function in cached:
        names = _plan_parameters(function)
        if names and (path, function.name) not in LRU_PLAN_PARAMETERS:
            found.problems.append(
                f"{path}:{function.lineno} {function.name}() is cached and takes a plan "
                f"({', '.join(names)}); key it by the plan's digest and run_set_key(...), or list "
                "it in LRU_PLAN_PARAMETERS with a reason"
            )


def scan(sources: Mapping[str, str]) -> Scan:
    found = Scan()
    for path, source in sources.items():
        _scan_module(path, source, found)
    return found


SOURCES = {
    path.relative_to(ENGINE).as_posix(): path.read_text("utf-8")
    for path in sorted(ENGINE.rglob("*.py"))
}
REAL = scan(SOURCES)


def test_every_digest_keyed_memo_holds_what_the_run_sets() -> None:
    assert not REAL.problems, "\n".join(REAL.problems)


def test_the_memo_sites_are_the_ones_the_scan_finds() -> None:
    assert REAL.keyed_sites() == MEMO_SITES


def test_the_records_are_still_uses_of_a_digest() -> None:
    uses = {(use.path, use.function) for use in REAL.uses}
    assert set(RECORDS) <= uses


def test_the_allowlists_name_something_that_is_there() -> None:
    assert not set(PAIRED_DIFFERENTLY) - REAL.keyed_sites()
    for path, name in LRU_PLAN_PARAMETERS:
        assert f"def {name}(" in SOURCES[path]


# -- each rule catches a mutant of the real source ----------------------------------------------

VIEW = "generator/versionview.py"
VIEW_KEY = "    held = (root.digest, root.seed_tree_version, history.digest().value, run_set_key(history))\n"


def _mutant(path: str, old: str, new: str) -> Scan:
    """The scan of the engine with *old* in *path* replaced by *new* (in memory: no file changes)."""
    assert SOURCES[path].count(old) == 1, (path, old)
    return scan({**SOURCES, path: SOURCES[path].replace(old, new)})


def _caught(found: Scan, path: str, text: str) -> None:
    assert any(path in problem and text in problem for problem in found.problems), found.problems


def test_a_key_that_drops_the_helper_is_caught() -> None:
    found = _mutant(
        VIEW, "history.digest().value, run_set_key(history))", "history.digest().value)"
    )
    _caught(found, VIEW, "outside a key that holds run_set_key")


def test_a_digest_moved_into_a_variable_is_caught() -> None:
    found = _mutant(
        VIEW,
        VIEW_KEY,
        "    fingerprint = history.digest().value\n"
        "    held = (root.digest, root.seed_tree_version, fingerprint, run_set_key(history))\n",
    )
    _caught(found, VIEW, "outside a key that holds run_set_key")


def test_a_key_by_a_digest_with_no_value_is_caught() -> None:
    found = _mutant(VIEW, "history.digest().value, run_set_key(history))", "history.digest())")
    _caught(found, VIEW, "digest() with no .value")


def test_a_digest_object_held_in_a_variable_is_caught() -> None:
    found = _mutant(
        VIEW,
        VIEW_KEY,
        "    fingerprint = history.digest()\n"
        "    held = (root.digest, root.seed_tree_version, fingerprint)\n",
    )
    _caught(found, VIEW, "'fingerprint' holds a digest")


def test_a_key_by_a_digest_helper_is_caught() -> None:
    found = _mutant(
        VIEW,
        "history.digest().value, run_set_key(history))",
        "digest_of_document(history.as_document(), subject='history'))",
    )
    _caught(found, VIEW, "digest helper's result")


def test_a_run_set_key_over_another_plan_than_the_digest_is_caught() -> None:
    found = _mutant(
        VIEW,
        "history.digest().value, run_set_key(history))",
        "history.digest().value, run_set_key(chain))",
    )
    _caught(found, VIEW, "run_set_key(chain) is not made over the plan")


def test_a_run_set_key_over_the_projection_for_the_history_is_caught() -> None:
    found = _mutant(
        "generator/events.py",
        "run_set_key(projection.history, projection)",
        "run_set_key(projection, projection)",
    )
    _caught(found, "generator/events.py", "run_set_key(projection) is not made over the plan")


def test_a_digest_keyed_memo_outside_the_generator_is_caught() -> None:
    source = (
        "_KEPT = {}\n\n\n"
        "def layout(plan):\n"
        "    held = (plan.digest().value, 3)\n"
        "    return _KEPT.setdefault(held, 1)\n"
    )
    _caught(scan({**SOURCES, "stages/new_memo.py": source}), "stages/new_memo.py", "outside a key")


def test_a_cache_over_a_plan_is_caught() -> None:
    for source in (
        "@lru_cache(maxsize=4)\ndef built(plan, root):\n    return 1\n",
        "@functools.cache\ndef built(held: 'GeneratorPlan'):\n    return 1\n",
        "@lru_cache\ndef built(pick: HistoryPick):\n    return 1\n",
        "def built(plan):\n    return 1\n\n\nmade = functools.cache(built)\n",
    ):
        _caught(scan({**SOURCES, "stages/cached.py": source}), "stages/cached.py", "takes a plan")


def test_what_is_no_memo_key_is_not_caught() -> None:
    source = (
        "@lru_cache\ndef built(rows: int, names: tuple):\n    return 1\n\n\n"
        "def same(left, right):\n    return left.digest() == right.digest()\n\n\n"
        "def hashed(data):\n    return sha256(data).digest()\n"
    )
    assert scan({**SOURCES, "stages/records.py": source}).problems == REAL.problems


def test_a_key_builder_that_returns_a_digest_tuple_is_caught() -> None:
    source = "def held_key(plan):\n    return (plan.digest(), 3)\n"
    _caught(scan({**SOURCES, "stages/keys.py": source}), "stages/keys.py", "no .value")
