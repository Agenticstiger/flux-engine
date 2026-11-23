# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy of a test bundle with one declaration changed.

The small telco fixture declares 64 rows and two fidelity properties. Tests
that need a run large enough to time, or to start worker processes, copy it
and raise that one number; tests of how a property is checked copy it and
declare other properties; tests of what an unreadable declaration is told
copy it and write one line otherwise. This is the one place that knows how.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable, Sequence, Tuple, Union

from flux_engine.core.digest import digest_of_document
from flux_engine.seam.resolve import load_documents_exact

#: The product the starter bundle's contract declares.
_STARTER_PRODUCT = "telco.gold.payment_recovery_moment"

#: The declared row count in the fixture's simulation document.
_DECLARED_ROWS = re.compile(r"(?m)^(\s+rows:) 64\b")

#: The fixture's fidelity property list: everything between its
#: ``properties:`` line and the ``budget:`` line that follows it.
_DECLARED_PROPERTIES = re.compile(r"(?ms)^(          properties:\n).*?^(          budget:)")

#: The fixture's whole fidelity block: its ``fidelity:`` line and every line
#: indented under it, up to the ``agentPolicy:`` section that follows.
_DECLARED_FIDELITY = re.compile(r"(?ms)^(        fidelity:).*?^(agentPolicy:)")

_SIMULATION = "simulation.flux.yml"


def _rewritten(
    fixture: Path,
    target: Path,
    pattern: "re.Pattern[str]",
    replacement: Union[str, Callable[["re.Match[str]"], str]],
    what: str,
    *,
    file_name: str = _SIMULATION,
) -> Path:
    """Copy *fixture* to *target*, replace the one match of *pattern* in *file_name*, return *target*."""
    shutil.copytree(fixture, target)
    document = target / file_name
    text, count = pattern.subn(replacement, document.read_text())
    assert count == 1, f"the fixture's {what} moved"
    document.write_text(text)
    return target


def bundle_with_rows(fixture: Path, target: Path, rows: int) -> Path:
    """Copy *fixture* to *target*, declare *rows* rows, and return *target*."""
    return _rewritten(fixture, target, _DECLARED_ROWS, rf"\g<1> {rows}", "declared row count")


def bundle_with_properties(fixture: Path, target: Path, properties: Sequence[str]) -> Path:
    """Copy *fixture* to *target*, declare *properties*, and return *target*.

    Each entry is one property as a YAML flow mapping, such as
    ``{ property: support, column: market, tolerance: { type: count, max: 0 } }``;
    together they replace the fixture's whole list, in the order given.
    """
    listed = "".join(f"            - {entry}\n" for entry in properties)
    return _rewritten(
        fixture,
        target,
        _DECLARED_PROPERTIES,
        lambda found: found.group(1) + listed + found.group(2),
        "fidelity property list",
    )


def bundle_with_fidelity(fixture: Path, target: Path, value: str) -> Path:
    """Copy *fixture* to *target*, declare its fidelity block as *value*, and return *target*.

    *value* is the YAML written after ``fidelity:``, on that one line, such
    as ``oops`` or ``~``; it replaces the fixture's whole block.
    """
    return _rewritten(
        fixture,
        target,
        _DECLARED_FIDELITY,
        lambda found: f"{found.group(1)} {value}\n{found.group(2)}",
        "fidelity block",
    )


def bundle_with_text(fixture: Path, target: Path, file_name: str, old: str, new: str) -> Path:
    """Copy *fixture* to *target*, write *new* for the one *old* in *file_name*, return *target*."""
    return _rewritten(
        fixture, target, re.compile(re.escape(old)), lambda _: new, repr(old), file_name=file_name
    )


def repin_contract(contract: Path, simulation: Path, product_id: str) -> None:
    """Pin *simulation* to *contract*'s digest again, as an author does after editing the contract."""
    (document,) = [doc for doc in load_documents_exact(contract) if doc.get("id") == product_id]
    digest = digest_of_document(document, subject=f"contractDigest:{product_id}").value
    text = simulation.read_text("utf-8")
    pinned = set(re.findall(r'contractDigest: "(sha256:[0-9a-f]{64})"', text))
    assert len(pinned) == 1, "the simulation pins one contract digest"
    simulation.write_text(text.replace(pinned.pop(), digest), "utf-8")


def add_contract_columns(bundle: Path, columns: Sequence[Tuple[str, str, bool]]) -> Path:
    """Append ``(name, type, required)`` columns to the starter bundle's contract; return *bundle*.

    The simulation pins the contract's digest, so it is re-pinned the way an
    author does after editing a contract. The columns go at the end of the
    first (and only) expose's schema, so the declared column order is the
    starter's, then these in the order given.
    """
    contract = bundle / "payment-recovery.fluid.yml"
    text = contract.read_text("utf-8")
    anchor = "    binding:\n"
    assert text.count(anchor) == 1, "the starter contract changed shape"
    added = "".join(
        f"        - name: {name}\n          type: {column_type}\n"
        f"          required: {'true' if required else 'false'}\n"
        for name, column_type, required in columns
    )
    contract.write_text(text.replace(anchor, added + anchor), "utf-8")
    repin_contract(contract, bundle / _SIMULATION, _STARTER_PRODUCT)
    return bundle


def declare_in_namespace(bundle: Path, lines: Sequence[str]) -> Path:
    """Add *lines* to the starter simulation's ``com.agentics.flux/1`` extensions; return *bundle*.

    The starter declares no extensions, so the block is new and goes at the end of
    the document, as a top-level key beside ``spec``. Each line is one key of the
    namespace, written as it would be in the file: ``strictCoverage: true``.
    """
    simulation = bundle / _SIMULATION
    text = simulation.read_text("utf-8")
    assert "extensions:" not in text, "the starter simulation changed shape"
    block = "extensions:\n  com.agentics.flux/1:\n" + "".join(f"    {line}\n" for line in lines)
    simulation.write_text(text.rstrip("\n") + "\n" + block, "utf-8")
    return bundle


def add_second_expose(
    bundle: Path, expose_id: str, columns: Sequence[Tuple[str, str, bool]]
) -> Path:
    """Add a second expose to the starter contract, and a second emit for it; return *bundle*.

    Both emits point at the one product, so the pinned digest is the same for
    both and is re-pinned once. Call it after :func:`add_contract_columns`,
    which expects the starter contract's single ``binding`` block.
    """
    contract = bundle / "payment-recovery.fluid.yml"
    schema = "".join(
        f"        - name: {name}\n          type: {column_type}\n"
        f"          required: {'true' if required else 'false'}\n"
        for name, column_type, required in columns
    )
    contract.write_text(
        contract.read_text("utf-8")
        + f"  - exposeId: {expose_id}\n    kind: stream\n    contract:\n      schema:\n{schema}"
        f"    binding:\n      platform: kafka\n      format: kafka_topic\n"
        f"      location:\n        topic: billing.{expose_id}.v1\n",
        "utf-8",
    )
    (document,) = [
        doc for doc in load_documents_exact(contract) if doc.get("id") == _STARTER_PRODUCT
    ]
    digest = digest_of_document(document, subject=f"contractDigest:{_STARTER_PRODUCT}").value
    simulation = bundle / _SIMULATION
    text = simulation.read_text("utf-8")
    pinned = re.findall(r'contractDigest: "(sha256:[0-9a-f]{64})"', text)
    assert len(pinned) == 1, "the starter simulation changed shape"
    simulation.write_text(
        text.replace(pinned[0], digest)
        + f"    - productRef: {_STARTER_PRODUCT}\n      exposeId: {expose_id}\n"
        f'      fluidVersion: "^0.7.3"\n      provenance:\n        contractDigest: "{digest}"\n',
        "utf-8",
    )
    return bundle
