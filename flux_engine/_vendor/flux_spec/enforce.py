#!/usr/bin/env python3
"""FLUX reference policy gate (RFC-04).

The normative enforcement decision contract, implemented. A conformant gate
answers one question — *may this AI call proceed under this agentPolicy?* —
and answers it identically everywhere:

    decide(policy, request, skill=None) -> decision

Checks run in a fixed, normative order and the first failure wins:

  0. INVALID_POLICY          the policy itself is off-spec (fail closed)
  1. INVALID_REQUEST         the request is malformed
  2. MODEL_NOT_ALLOWED       model outside agentPolicy.allowedModels
  3. USE_CASE_DENIED         use case listed in deniedUseCases
  4. USE_CASE_NOT_ALLOWED    allowedUseCases declared and use case absent
  5. PURPOSE_REQUIRED        purposeLimitation on (default true) and no purpose
  6. TOKEN_BUDGET_EXCEEDED   tokens above the effective budget

Three properties make this contract portable rather than merely implementable:

**Numbers are values, not Python types.** JSON Schema `"type": "integer"`
matches `200000.0` as readily as `200000`, so a gate that tests
`isinstance(x, int)` silently stops enforcing budgets on a schema-valid
policy. Every integer here is read through `as_int()`, which accepts either
spelling and rejects booleans.

**Strings are compared after normalisation.** `useCase` is caller-supplied,
so a deny-list matched by raw equality is advisory: `Advertising`,
`advertising ` and a Cyrillic homoglyph all slip past. Identifiers are
NFC-normalised and stripped before comparison (case-sensitively — see
`normalize()`).

**Digests are canonical.** `policyDigest` is the SHA-256 of the policy
canonicalised per RFC 8785 (JCS), so a Python gate and a JavaScript gate
agree byte-for-byte. It digests the *document's* policy, never a synthesised
one; when a skill narrows the budget the decision records `skillRef` and
`effectiveBudget` separately, so an auditor can join a decision back to the
document that authorised it and still see what actually applied.

Prior art: the evaluation model is deliberately the one adopters already know
from Cedar (docs.cedarpolicy.com) and Open Policy Agent — **default deny**,
and **forbid overrides permit** (here: deniedUseCases beats allowedUseCases).
We diverge from Cedar's `{decision, diagnostics:{reason, errors}}` shape in
one respect: Cedar's `reason` lists the policy ids that matched, which is
right for a policy *language* with arbitrary rules, whereas FLUX has a fixed
check contract. A closed `reasonCode` enum is what makes conformance testable
— two gates must agree not just on the verdict but on *why*.

Usage:
    python3 scripts/enforce.py                      # run the conformance vectors
    python3 scripts/enforce.py --vectors FILE       # run a specific vector file

Exit code 0 = every vector matched. Vendors demonstrate RFC-04 conformance by
running their own gate against tests/enforcement-vectors.json — which is
itself guarded by a negative test (tests/test_regression.py) proving that a
gate with known defects fails the suite.

Requires: pip install rfc8785 (pure Python, no dependencies — the Trail of
Bits implementation of RFC 8785).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path

try:
    import rfc8785
except ImportError:  # pragma: no cover
    sys.exit("enforce.py requires: pip install rfc8785")

# VENDOR EDIT (see VENDOR-NOTES.md): one level shallower (see validate.py),
# and the vectors ship under vectors/ in the vendored layout, not tests/.
REPO = Path(__file__).resolve().parent
DEFAULT_VECTORS = REPO / "vectors" / "enforcement-vectors.json"

# JSON numbers are IEEE-754 doubles once they cross a language boundary, so
# integers above 2^53-1 cannot be exchanged (or canonicalised per RFC 8785)
# without silent precision loss. Values beyond it are rejected rather than
# rounded — a budget nobody can agree on is worse than no budget.
MAX_SAFE_INTEGER = 9007199254740991

REASONS = {
    "ALLOWED": "the call satisfies every constraint of the policy",
    "INVALID_POLICY": "agentPolicy is off-spec: {detail}",
    "INVALID_REQUEST": "request is malformed: {detail}",
    "MODEL_NOT_ALLOWED": "model {model!r} is not in agentPolicy.allowedModels",
    "USE_CASE_DENIED": "use case {useCase!r} is listed in agentPolicy.deniedUseCases",
    "USE_CASE_NOT_ALLOWED": "use case {useCase!r} is not in agentPolicy.allowedUseCases",
    "PURPOSE_REQUIRED": "agentPolicy.purposeLimitation requires a purpose on every call",
    "TOKEN_BUDGET_EXCEEDED": "requested {tokens} tokens, effective budget is {budget}",
}


def as_int(value):
    """The JSON notion of an integer, not Python's.

    JSON Schema `"type": "integer"` accepts any number with a zero fractional
    part, so `200000.0` is a valid tokenBudget and a JS gate will parse it to
    a float. Booleans are rejected outright — `isinstance(True, int)` is True
    in Python, which would otherwise read `tokenBudget: true` as a budget of 1.
    Returns None when the value is not an integer.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def normalize(value: str) -> str:
    """Normalise an identifier before comparison.

    NFC (so a decomposed 'café' matches a composed one) plus surrounding
    whitespace stripped (so 'advertising ' cannot slip past a deny-list).
    Matching is case-SENSITIVE: folding case would make `Model-A` and
    `model-a` the same identifier, which is wrong for model names that really
    do differ. Policies should not declare identifiers differing only by case.
    """
    return unicodedata.normalize("NFC", value).strip()


def canonical_bytes(value) -> bytes:
    """RFC 8785 (JCS) canonical serialisation — the digest's input."""
    return rfc8785.dumps(value)


def policy_digest(policy) -> str:
    """SHA-256 over the JCS canonicalisation of the policy.

    Canonical means cross-implementation: JCS fixes key order, number
    spelling (200000.0 and 200000 serialise identically) and string escaping
    (raw UTF-8, not \\uXXXX), so a Python gate and a JavaScript gate produce
    the same digest for the same policy.
    """
    return "sha256:" + hashlib.sha256(canonical_bytes(policy)).hexdigest()


def _string_list(value):
    return isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value)


def policy_problem(policy) -> str | None:
    """Why this policy is off-spec, or None. Mirrors $defs/agentPolicy.

    A gate MUST reject an off-spec policy rather than enforce a degraded
    reading of it: a missing or non-integer tokenBudget must never mean
    "no budget".
    """
    if not isinstance(policy, dict):
        return "policy must be an object"
    if not _string_list(policy.get("allowedModels")):
        return "allowedModels must be a non-empty array of non-empty strings"
    budget = as_int(policy.get("tokenBudget"))
    if budget is None or budget < 1:
        return "tokenBudget must be an integer of at least 1"
    if budget > MAX_SAFE_INTEGER:
        return f"tokenBudget exceeds the JSON-safe integer domain ({MAX_SAFE_INTEGER})"
    for field in ("allowedUseCases", "deniedUseCases"):
        if field in policy and not _string_list(policy[field]):
            return f"{field}, when present, must be a non-empty array of non-empty strings"
    if "purposeLimitation" in policy and not isinstance(policy["purposeLimitation"], bool):
        return "purposeLimitation must be a boolean"
    return None


def request_problem(request) -> str | None:
    if not isinstance(request, dict):
        return "request must be an object"
    for field in ("model", "useCase"):
        value = request.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"{field} must be a non-empty string"
    tokens = as_int(request.get("tokens"))
    if tokens is None:
        return "tokens must be an integer"
    if tokens < 0:
        return "tokens must not be negative"
    if tokens > MAX_SAFE_INTEGER:
        return f"tokens exceeds the JSON-safe integer domain ({MAX_SAFE_INTEGER})"
    if "purpose" in request and not isinstance(request["purpose"], str):
        return "purpose, when present, must be a string"
    return None


def effective_budget(policy, skill=None) -> int:
    """The budget that actually applies to this call.

    A skill NARROWS: the effective budget is min(skill, policy). It can never
    widen one — the document validator rejects a skill budget above its
    policy's, and this is the belt to that braces.
    """
    budget = as_int(policy.get("tokenBudget"))
    skill_budget = as_int((skill or {}).get("tokenBudget"))
    if skill_budget is None:
        return budget
    return min(skill_budget, budget)


def effective_policy(policy: dict, skill: dict | None = None) -> dict:
    """Back-compat helper: the policy with its budget narrowed by `skill`.

    Prefer passing `skill` to decide() directly — that keeps policyDigest
    bound to the document's own policy instead of a synthesised one.
    """
    if not skill:
        return policy
    narrowed = dict(policy)
    budget = effective_budget(policy, skill)
    if budget is not None:
        narrowed["tokenBudget"] = budget
    return narrowed


def _decision(allow, code, policy, request, skill=None, budget=None, **fmt):
    fields = {**(request if isinstance(request, dict) else {}), "budget": budget, **fmt}
    decision = {
        "allow": allow,
        "reasonCode": code,
        "reason": REASONS[code].format(**fields),
        "policyDigest": policy_digest(policy) if isinstance(policy, dict) else policy_digest({}),
        "request": request if isinstance(request, dict) else {},
    }
    if budget is not None:
        decision["effectiveBudget"] = budget
    if skill and isinstance(skill.get("name"), str):
        decision["skillRef"] = skill["name"]
    return decision


def decide(policy: dict, request: dict, skill: dict | None = None) -> dict:
    """Evaluate one call against one agentPolicy. Pure and deterministic.

    `policy` is the document's agentPolicy — pass `skill` rather than a
    pre-narrowed policy, so the decision's digest identifies the authorising
    document and `effectiveBudget`/`skillRef` record what actually applied.
    """
    problem = policy_problem(policy)
    if problem:
        return _decision(False, "INVALID_POLICY", policy, request, skill, detail=problem)

    problem = request_problem(request)
    if problem:
        return _decision(False, "INVALID_REQUEST", policy, request, skill, detail=problem)

    budget = effective_budget(policy, skill)
    model = normalize(request["model"])
    use_case = normalize(request["useCase"])

    if model not in {normalize(m) for m in policy["allowedModels"]}:
        return _decision(False, "MODEL_NOT_ALLOWED", policy, request, skill, budget=budget)

    if use_case in {normalize(u) for u in policy.get("deniedUseCases") or []}:
        return _decision(False, "USE_CASE_DENIED", policy, request, skill, budget=budget)

    if "allowedUseCases" in policy and use_case not in {normalize(u) for u in policy["allowedUseCases"]}:
        return _decision(False, "USE_CASE_NOT_ALLOWED", policy, request, skill, budget=budget)

    if policy.get("purposeLimitation", True) and not (request.get("purpose") or "").strip():
        return _decision(False, "PURPOSE_REQUIRED", policy, request, skill, budget=budget)

    if as_int(request["tokens"]) > budget:
        return _decision(False, "TOKEN_BUDGET_EXCEEDED", policy, request, skill, budget=budget)

    return _decision(True, "ALLOWED", policy, request, skill, budget=budget)


def run_vectors(path: Path, gate=decide, label: str = "reference gate") -> tuple[int, list[str]]:
    """Run a vector file against `gate`. Returns (failures, messages).

    Every vector asserts allow, reasonCode, and — where the vector pins one —
    policyDigest, so a gate cannot pass by guessing verdicts while computing
    digests wrongly (or not at all).
    """
    vectors = json.loads(path.read_text())
    messages = []
    for v in vectors["vectors"]:
        decision = gate(v["policy"], v["request"], v.get("skill"))
        expect = v["expect"]
        if decision.get("allow") != expect["allow"] or decision.get("reasonCode") != expect["reasonCode"]:
            messages.append(
                f"{v['name']}: expected {expect['reasonCode']} (allow={expect['allow']}), "
                f"got {decision.get('reasonCode')} (allow={decision.get('allow')})"
            )
            continue
        if "policyDigest" in expect and decision.get("policyDigest") != expect["policyDigest"]:
            messages.append(f"{v['name']}: policyDigest mismatch — expected {expect['policyDigest']}, got {decision.get('policyDigest')}")
        if "effectiveBudget" in expect and decision.get("effectiveBudget") != expect["effectiveBudget"]:
            messages.append(f"{v['name']}: effectiveBudget mismatch — expected {expect['effectiveBudget']}, got {decision.get('effectiveBudget')}")
    return len(messages), messages


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FLUX reference policy gate (RFC-04)")
    ap.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    args = ap.parse_args(argv)
    total = len(json.loads(args.vectors.read_text())["vectors"])
    failures, messages = run_vectors(args.vectors)
    if failures:
        print(f"FAIL — {failures}/{total} enforcement vectors failed")
        for m in messages:
            print(" ", m)
        return 1
    print(f"ok — {total} enforcement vectors green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
