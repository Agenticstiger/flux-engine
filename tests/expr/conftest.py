# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the ``flux-expr/1`` suite.

The published vectors (``tests/vectors/expr-vectors.json``) are the single
source of the scope models used across these tests: a model is described once,
in the file that ships upstream, and the loader here is the reference
implementation of reading it back. Nothing in this module hard-codes a count
or a level list taken from the telco example -- those come from the bundle.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

import pytest

from flux_engine.expr.types import Scope, ScopeModel, Type, TypeKind, enum_of

REPO_ROOT = Path(__file__).resolve().parents[2]
VECTORS_PATH = REPO_ROOT / "tests" / "vectors" / "expr-vectors.json"

#: Where the FLUX spec repo's shipped telco bundle lives when it is checked
#: out beside this repo. Tests that need the real documents skip without it;
#: everything else runs from the vectors file.
_EXAMPLE_CANDIDATES = (
    REPO_ROOT.parent / "FLUX" / "examples" / "telco-payment-recovery",
    REPO_ROOT / "examples" / "telco-payment-recovery",
)


def telco_example_dir() -> Optional[Path]:
    """Return the shipped telco bundle directory, or ``None`` if not present."""
    for candidate in _EXAMPLE_CANDIDATES:
        if (candidate / "world.flux.yml").is_file():
            return candidate
    return None


def type_from_json(blob: Mapping[str, Any]) -> Type:
    """Rebuild a :class:`Type` from its ``attrmodel/1`` JSON form."""
    kind = TypeKind(blob["type"])
    if kind is TypeKind.ENUM:
        return enum_of(blob["levels"])
    return Type(kind)


def scope_model_from_json(blob: Mapping[str, Any]) -> ScopeModel:
    """Rebuild a :class:`ScopeModel` from a vectors-file model fragment."""
    return ScopeModel(
        Scope(blob["scope"]),
        {name: type_from_json(declared) for name, declared in blob["paths"].items()},
        dict(blob.get("aliases", {})),
    )


def decode_value(value: Any, declared: Type) -> Any:
    """Decode a vector row value from its JSON encoding into a view value.

    ``number`` arrives as a decimal string (never a JSON number -- KERNEL-SPEC
    §2.1 keeps ``float`` out of every document), ``set<string>`` as an array,
    everything else as itself.
    """
    if value is None:
        return None
    if declared.kind is TypeKind.NUMBER:
        return Decimal(value)
    if declared.kind is TypeKind.SET_STRING:
        return frozenset(value)
    return value


def load_vectors() -> Dict[str, Any]:
    """Load and return the published vectors document."""
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


_DOCUMENT = load_vectors()
_MODELS: Dict[str, ScopeModel] = {
    name: scope_model_from_json(blob) for name, blob in _DOCUMENT["models"].items()
}


def vector_cases() -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(id, vector)`` for every published vector, for parametrisation."""
    for vector in _DOCUMENT["vectors"]:
        yield vector["id"], vector


@pytest.fixture(scope="session")
def vectors() -> Dict[str, Any]:
    """The published vectors document."""
    return _DOCUMENT


@pytest.fixture(scope="session")
def models() -> Mapping[str, ScopeModel]:
    """The vectors file's model fragments, rebuilt as scope models."""
    return _MODELS


@pytest.fixture()
def tiny(models: Mapping[str, ScopeModel]) -> ScopeModel:
    """The small hand-built subject model the operator vectors use."""
    return models["tiny"]


@pytest.fixture()
def telco_subject(models: Mapping[str, ScopeModel]) -> ScopeModel:
    """The telco ``subject`` scope, as published in the vectors file."""
    return models["telco-subject"]


@pytest.fixture()
def telco_event(models: Mapping[str, ScopeModel]) -> ScopeModel:
    """The telco ``event`` scope for ``sig-payment-aborted``."""
    return models["telco-event-payment-aborted"]


@pytest.fixture()
def telco_observed(models: Mapping[str, ScopeModel]) -> ScopeModel:
    """The telco ``observed`` scope."""
    return models["telco-observed"]


def model_for(vector: Mapping[str, Any]) -> ScopeModel:
    """Return the scope model a vector is checked against."""
    return _MODELS[vector["model_fragment"]]


def ids_of(prefix: str) -> List[str]:
    """Vector ids beginning with *prefix* -- used to assert coverage."""
    return [vector["id"] for vector in _DOCUMENT["vectors"] if vector["id"].startswith(prefix)]
