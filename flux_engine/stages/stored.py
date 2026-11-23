# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a run's plugin bytes are stored is decided by the bundle, never by ``plan.json``.

ADR 0032 section 6. The state directory can cross a trust boundary (ADR 0015), and ``plan.json``
sits in it: its ``planDigest`` is an unkeyed hash and the run id derives from it, so a person who can
write the directory can rewrite its ``generators`` member, digest it again and rename the run. What
the bundle declares is different: ``apply`` and ``ship`` check the bundle's bytes against the plan's
``inputDigest``, and the declaration is among them. So the stages ask this module what the bundle
declares, and the plan's record may only agree with it. A record that is missing, or differs, is
refused (``FLX-3123``) by a message that quotes no value, so a forged record can make a command refuse
and can never make one skip a check.

Nothing here imports a plugin.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import Kernel
from flux_engine.evidence.report import StoredGenerator
from flux_engine.generators.errors import FLX_3123
from flux_engine.generators.registry import normalize_distribution
from flux_engine.seam.generators import GeneratorDeclaration
from flux_engine.seam.resolve import declared_generators, load_bundle
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages.errors import InputDigestMoved
from flux_engine.stages.plan import GENERATORS_PLAN_KEY

__all__ = [
    "REPRODUCIBILITY_KINDS",
    "declared_for_run",
    "require_record",
    "stored_generators",
]

#: The reproducibility a declaration, and so a record, may state (ADR 0032 section 6).
REPRODUCIBILITY_KINDS = ("bytes", "environment", "none")

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def declared_for_run(
    directory: Path,
    plan_document: Mapping[str, Any],
    kernel: Kernel,
    simulation_id: Optional[str],
) -> Optional[Tuple[GeneratorDeclaration, ...]]:
    """The generator plugins the bundle at *directory* declares, read from the bundle itself.

    ``()`` when it declares none. ``None`` when the bundle cannot be read, so the stage that goes on
    reports it as it always did. A bundle that declares a plugin (or a plan that records one) whose
    ``inputDigest`` is not the plan's is the moved bundle ``FLX-2101`` names: its declaration is not
    the one the plan was made from, and nothing is decided from it.
    """
    from flux_engine.runtime import resolve_cast

    try:
        loaded = load_bundle(Path(directory))
        cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
        declared = declared_generators(cast.simulation, where=cast.simulation_id)
        current = _pipeline.input_digest_of(loaded, kernel=kernel).ref.value
    except FluxError:
        return None
    planned = plan_document.get("inputDigest")
    if current != planned and (declared or GENERATORS_PLAN_KEY in plan_document):
        raise InputDigestMoved(planned=str(planned), current=current)
    return declared


def _mismatch() -> FluxError:
    return FluxError(
        FLX_3123,
        "the generator plugins this run's plan.json records are not the ones its bundle "
        "declares (the plugin, its distribution and version, the grade, the privacy account or "
        "the model digest), or it records none for a bundle that declares them, or records "
        "some for one that does not; a plan.json that was changed is not trusted, and its "
        "plugin bytes are stored, never decided from it; plan the run again",
    )


def _agrees(declaration: GeneratorDeclaration, entry: Mapping[str, Any]) -> bool:
    distribution = entry.get("distribution")
    if (
        entry.get("plugin") != declaration.plugin
        or not isinstance(distribution, str)
        or normalize_distribution(distribution) != normalize_distribution(declaration.distribution)
        or entry.get("version") != declaration.version
        or entry.get("grade") != declaration.grade.value
        or entry.get("modelDigest") != declaration.model_digest
    ):
        return False
    account = declaration.privacy.to_document() if declaration.privacy is not None else None
    return bool(entry.get("privacy") == account)


def require_record(
    declared: Sequence[GeneratorDeclaration], plan_document: Mapping[str, Any]
) -> None:
    """Refuse (``FLX-3123``) a ``plan.json`` whose ``generators`` member is not what the bundle declares.

    The same exposes, and for each the plugin, the distribution (as PEP 503 normalizes it), the
    version, the grade, the privacy account and the model digest. A plan that records nothing for a
    bundle that declares nothing agrees. The message quotes no value.
    """
    recorded = plan_document.get(GENERATORS_PLAN_KEY)
    if not declared:
        if recorded is None:
            return
        raise _mismatch()
    wanted = {item.expose_id: item for item in declared}
    if not isinstance(recorded, Mapping) or set(recorded) != set(wanted):
        raise _mismatch()
    for expose_id, declaration in wanted.items():
        entry = recorded[expose_id]
        if not isinstance(entry, Mapping) or not _agrees(declaration, entry):
            raise _mismatch()


def stored_generators(
    declared: Sequence[GeneratorDeclaration], plan_document: Mapping[str, Any]
) -> Tuple[StoredGenerator, ...]:
    """The exposes ``EVIDENCE.md`` says were stored, in expose order; empty when the bundle declares none.

    The record is the bundle's declaration (:func:`require_record` refuses a plan that disagrees)
    plus what only the plan holds: whether the install was editable, which makes the run ``none``,
    and the environment digest the engine measured. Those must be well formed: a boolean, one of
    :data:`REPRODUCIBILITY_KINDS` that is the declared one (or ``none`` for an editable install), and
    ``sha256:`` with 64 lowercase hex digits. Anything else is ``FLX-3123``.
    """
    require_record(declared, plan_document)
    if not declared:
        return ()
    recorded = plan_document[GENERATORS_PLAN_KEY]
    found: List[StoredGenerator] = []
    for declaration in sorted(declared, key=lambda item: item.expose_id):
        entry = recorded[declaration.expose_id]
        editable = entry.get("editable")
        kind = entry.get("reproducibility")
        digest = entry.get("environmentDigest")
        expected = "none" if editable is True else declaration.reproducibility
        if (
            not isinstance(editable, bool)
            or kind not in REPRODUCIBILITY_KINDS
            or kind != expected
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            raise FluxError(
                FLX_3123,
                "the reproducibility or the environment digest this run's plan.json records for "
                "a generator plugin is not well formed, or is not the one its bundle declares; "
                "a plan.json that was changed is not trusted; plan the run again",
            )
        found.append(
            StoredGenerator(
                expose_id=declaration.expose_id,
                plugin=declaration.plugin,
                distribution=declaration.distribution,
                version=declaration.version,
                editable=editable,
                reproducibility=expected,
                environment_digest=digest,
            )
        )
    return tuple(found)
