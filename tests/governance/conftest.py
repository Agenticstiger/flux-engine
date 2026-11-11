# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the governance suite (RFC-04 gate, audit, sovereignty, skills).

The 34 published RFC-04 conformance vectors are loaded from wherever
:mod:`flux_engine.spec` resolves them -- the installed ``flux_spec``
distribution when present, the vendored copy otherwise (see
``flux_engine/spec/__init__.py``'s ``default_vectors_path``) -- so this
suite runs the same vectors flux_engine would ship against, not a copy
pinned inside the test tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import pytest

from flux_engine.spec import default_vectors_path

try:  # pragma: no cover - exercised by whichever branch this environment takes
    import flux_spec.enforce as _impl
except ImportError:  # pragma: no cover
    import flux_engine._vendor.flux_spec.enforce as _impl  # type: ignore[no-redef]

VECTORS_PATH: Path = default_vectors_path()

#: The six defects the FLUX spec repo's own regression suite (RFC-04's
#: publisher, ``tests/test_regression.py::_broken``) uses to prove the
#: vector file actually constrains conformance: a conformant-looking gate
#: with exactly one of these defects must fail at least one vector. This
#: module reimplements the same six defects (not a subset, not a
#: rewording) so this suite can prove the identical property against
#: flux_engine's own binding rather than only trusting the upstream
#: publisher's own regression test.
DEFECTS: tuple[str, ...] = (
    "fail_open_purpose",
    "skill_replaces_budget",
    "native_int_budget",
    "case_insensitive",
    "lenient_policy",
    "fake_digest",
)


def broken_gate(defect: str) -> Callable[..., Dict[str, Any]]:
    """A gate identical to the reference except for one named *defect*.

    Delegates to ``flux_spec.enforce.decide`` for everything the defect
    does not touch, exactly as ``flux_engine.governance.gate.decide``
    does -- the point is to corrupt one behaviour, not to write a second
    implementation of the whole contract.
    """

    def gate(
        policy: Dict[str, Any],
        request: Dict[str, Any],
        skill: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if defect == "fail_open_purpose":  # purposeLimitation defaults to false
            policy = {"purposeLimitation": False, **policy}
        elif defect == "skill_replaces_budget":  # skill widens instead of narrowing
            if skill and _impl.as_int(skill.get("tokenBudget")) is not None:
                policy = {**policy, "tokenBudget": skill["tokenBudget"]}
            skill = None
        elif defect == "native_int_budget":  # isinstance(x, int) instead of JSON integer
            if isinstance(policy.get("tokenBudget"), float):
                policy = {k: v for k, v in policy.items() if k != "tokenBudget"}
                policy["tokenBudget"] = 10**15
        elif defect == "case_insensitive":  # folds case before matching
            request = {
                **request,
                "useCase": str(request.get("useCase", "")).lower(),
                "model": str(request.get("model", "")).lower(),
            }
            policy = {
                **policy,
                "allowedModels": [m.lower() for m in policy.get("allowedModels", [])],
            }
        elif defect == "lenient_policy":  # enforces off-spec policies instead of rejecting
            problem = _impl.policy_problem(policy)
            if problem:
                policy = {
                    "allowedModels": policy.get("allowedModels") or ["*"],
                    "tokenBudget": 10**15,
                    "purposeLimitation": False,
                }

        decision = _impl.decide(policy, request, skill)
        if defect == "fake_digest":  # no real binding to the policy
            decision = {**decision, "policyDigest": "sha256:" + "0" * 64}
        return decision

    return gate


@pytest.fixture(scope="session")
def vectors_path() -> Path:
    return VECTORS_PATH


@pytest.fixture(scope="session")
def vectors() -> List[Mapping[str, Any]]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]
