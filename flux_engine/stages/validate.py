# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim validate``: schema-valid, seam-resolvable, and executable in principle.

The checks, in order, none of which materialises a population or draws
a single value:

1. :func:`flux_engine.seam.resolve.load_bundle` -- the bundle is
   schema-valid FLUX/FLUID, and the validating and exact reads agree on
   which documents exist (KERNEL-SPEC §2).
2. Every ``Simulation``'s cast resolves (every ``*Ref`` in its closure
   points at a document in the bundle) and every one of its ``emits[]``
   entries resolves against a FLUID contract in the bundle
   (KERNEL-SPEC §8).
3. Every ``Simulation``'s declared ``fidelity`` is one this engine
   version can execute (:func:`flux_engine.stages.pipeline.require_supported_fidelity`).
4. Every ``Simulation``'s World names a vertical pack this engine ships
   (:func:`flux_engine.verticals.pack_of_world`, ``FLX-5042``), and the
   ``Simulation`` declares the ``timeBounds`` its clock runs over
   (:func:`flux_engine.core.clock.declared_time_bounds`, ``FLX-5023``).
   The FLUX schema leaves both optional; ``plan`` cannot run without them
   and refuses them in this order, with the same code.
5. Every emit's declared ranges and bins hold together: a bin inside its range,
   a range inside the contract's integer width (``FLX-3402``), and a DATE or
   TIMESTAMP range inside ``timeBounds`` (a warning, not a refusal).
6. Every emit's fidelity block keeps SEAM-FIDELITY-SPEC §2's rules, read
   by the parser ``verify`` compiles with
   (:func:`flux_engine.stages.pipeline.parse_declared_fidelity`), so a
   refusal here is the one ``verify`` would give. A tolerance its margin
   cannot meet is §3's, refused by ``plan``.

7. With ``profile`` (``--profile``), the profile a Simulation pins is read, and ``validate`` refuses
   what ``plan`` refuses about it with the same code: a profile that is not the one pinned or
   cannot be read (``FLX-3109``, ``FLX-3110``), an overlay that names a table or column the
   profile lacks (``FLX-3105``), and a parameter of the register that cites a table or column the
   profile does not describe (``FLX-3113``). That is all it checks about a profile. It leaves to
   ``plan`` a profile given to a Simulation that pins none, and the grade floor (``FLX-4005``).
   Without ``profile`` there is nothing to read, so none of these is found: ``validate`` says valid
   for a bundle ``plan`` refuses on one of them.

Nothing here is written to disk; ``validate`` never creates a run
directory, which is the difference between it and ``plan``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple, Union

from flux_engine.api.stages import StageResult
from flux_engine.core.clock import declared_time_bounds
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import new_plan_is_counter_based
from flux_engine.generator.rows import declared_range_findings
from flux_engine.runtime import resolve_cast
from flux_engine.seam.overlay import require_in_contract
from flux_engine.seam.profile import require_profile
from flux_engine.seam.resolve import (
    FLX_3105,
    declared_generators,
    declared_overlay,
    load_bundle,
    profile_reference,
    resolve_all_emits,
    strict_coverage_declared,
)
from flux_engine.stages._consumer_check import (
    check_consumer_sql,
    consumers_unchecked,
    unchecked_line,
)
from flux_engine.stages._plugin_exposes import plugin_exposes, refuse_plugin_combinations
from flux_engine.stages.pipeline import (
    check_conditions_declared,
    check_copies_declared,
    check_emit_conditions,
    check_emit_copula,
    check_flux_side_rule_ids,
    check_generators_declared,
    check_hidden_declared,
    check_overlay_declared,
    check_reference_declared,
    check_relations_declared,
    counted_relationships,
    parameter_register_of,
    parse_declared_fidelity,
    plan_emit_rules,
    refuse_determined_columns,
    require_supported_fidelity,
)
from flux_engine.stages.plan import check_simulation_against_profile
from flux_engine.verticals import pack_of_world

__all__ = ["validate"]


def _check_emit(
    entry: Any,
    resolved: Any,
    simulation: Mapping[str, Any],
    plugins: Any,
    *,
    simulation_id: str,
    strict: bool,
) -> List[str]:
    """Refuse what `plan` would refuse in one emit, and return the warnings it holds."""
    parse_declared_fidelity(entry)
    check_reference_declared(
        entry, simulation, where=simulation_id, counter_based=new_plan_is_counter_based()
    )
    # The contract's rules are refused here as `plan` refuses them (EXPR-SPEC-2 §12),
    # and so are the columns they determine.
    rules = plan_emit_rules(
        entry,
        strict=strict,
        where=simulation_id,
        # `validate` has no plan, so it reads the rules as a new plan's kernel does.
        counter_based=new_plan_is_counter_based(),
        counted=counted_relationships(simulation, where=simulation_id),
        emits=resolved,
        windowed=simulation.get("spec", {}).get("timeBounds") is not None,
        time_bounds=simulation.get("spec", {}).get("timeBounds"),
    )
    refuse_determined_columns(entry, rules, sim_id=simulation_id, simulation=simulation)
    # What a generator plugin's expose cannot combine with (ADR 0032 section 5).
    refuse_plugin_combinations(entry, rules, plugins, where=simulation_id)
    check_emit_conditions(entry, rules, sim_id=simulation_id, simulation=simulation)
    check_emit_copula(
        entry,
        rules,
        sim_id=simulation_id,
        simulation=simulation,
        counter_based=new_plan_is_counter_based(),
    )
    if entry.fidelity is None:
        return []
    # A bin outside its range, or a range outside its integer width, is
    # refused here; a date range outside timeBounds is a warning.
    return list(
        declared_range_findings(
            entry.compile_planned(), entry.fidelity, simulation.get("spec", {}).get("timeBounds")
        )
    )


def _check_overlay_names(resolved: Any, simulation: Mapping[str, Any], *, where: str) -> None:
    """Refuse (``FLX-3105``) an overlay that names a table or column the pinned contract lacks.

    `plan` checks the overlay against the profile it is handed; `validate` has no profile, but a
    profile describes the contract's tables and columns, so a name the contract does not have is
    refused here with the code `plan` gives it.
    """
    overlay = declared_overlay(simulation, where=where)
    if overlay is None:
        return
    contract: Dict[str, List[str]] = {}
    for entry in resolved:
        for expose in entry.contract_document.get("exposes") or ():
            schema = (expose.get("contract") or {}).get("schema") or ()
            contract[str(expose.get("exposeId"))] = [str(column.get("name")) for column in schema]
    require_in_contract(contract, overlay, code=FLX_3105)


def _validate_simulation(
    loaded: Any, simulation_id: str, profile: Union[str, Path, None], warnings: List[str]
) -> Tuple[Dict[str, Any], bool]:
    """Check one Simulation of the bundle; its summary, and whether its consumers' queries went unchecked.

    The warnings its checks give are added to *warnings*. A refusal is raised as the
    :class:`FluxError` it is.
    """
    cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
    fidelity = str(cast.simulation.get("spec", {}).get("fidelity", "deterministic"))
    require_supported_fidelity(fidelity)
    pack_of_world(cast.world)
    declared_time_bounds(cast.simulation)
    resolved = resolve_all_emits(
        loaded, simulation_id=simulation_id, counter_based=new_plan_is_counter_based()
    )
    strict = strict_coverage_declared(cast.simulation, where=simulation_id)
    check_flux_side_rule_ids(resolved, where=simulation_id)
    check_copies_declared(
        cast.simulation, where=simulation_id, counter_based=new_plan_is_counter_based()
    )
    check_generators_declared(
        cast.simulation, where=simulation_id, counter_based=new_plan_is_counter_based()
    )
    check_hidden_declared(resolved, where=simulation_id, counter_based=new_plan_is_counter_based())
    check_overlay_declared(
        cast.simulation, where=simulation_id, counter_based=new_plan_is_counter_based()
    )
    _check_overlay_names(resolved, cast.simulation, where=simulation_id)
    plugins = plugin_exposes(resolved, declared_generators(cast.simulation, where=simulation_id))
    check_conditions_declared(
        cast.simulation, where=simulation_id, counter_based=new_plan_is_counter_based()
    )
    for entry in resolved:
        warnings.extend(
            _check_emit(
                entry,
                resolved,
                cast.simulation,
                plugins,
                simulation_id=simulation_id,
                strict=strict,
            )
        )
    # The counts and weights `plan` refuses when it lays the relationships out.
    check_relations_declared(
        resolved, cast.simulation, sim_id=simulation_id, strict=strict, plugins=plugins
    )
    # Consumer queries: every view and query is checked against the exposes' schema, as
    # `plan` checks them, so a text `verify` would refuse is refused before any apply.
    check_consumer_sql(cast.simulation, resolved, where=simulation_id)
    is_unchecked = consumers_unchecked(cast.simulation, where=simulation_id)
    # Parameter register: every register entry has to name numbers of this run.
    register = parameter_register_of(loaded, cast)
    _check_pinned_profile(cast.simulation, simulation_id, register, profile)
    summary = _simulation_summary(cast, resolved, fidelity, register)
    return summary, is_unchecked


def _simulation_summary(
    cast: Any, resolved: Sequence[Any], fidelity: str, register: Any
) -> Dict[str, Any]:
    """What ``validate`` reports of a Simulation it found valid."""
    return {
        "fidelity": fidelity,
        "documents": len(cast.documents),
        "emits": [entry.expose_id for entry in resolved],
        "emitsWithFidelity": [entry.expose_id for entry in resolved if entry.fidelity is not None],
        **(
            {"parameters": {"declared": len(register.numbers), "counts": register.counts()}}
            if register is not None
            else {}
        ),
    }


def _check_pinned_profile(
    simulation: Any, simulation_id: str, register: Any, profile: Union[str, Path, None]
) -> None:
    """Read the Simulation against the profile it pins, as ``plan`` reads it; nothing without a profile."""
    if profile is None:
        return
    pin = profile_reference(simulation, where=simulation_id)
    if pin is not None:
        check_simulation_against_profile(
            simulation,
            simulation_id,
            register,
            require_profile(pin, Path(profile), where=simulation_id),
        )


def validate(
    bundle_directory: Union[str, Path],
    *,
    tolerate_additive: bool = True,
    profile: Union[str, Path, None] = None,
) -> StageResult:
    """Validate the bundle at *bundle_directory*. Never writes anything.

    *profile* is the profile document the Simulations pin, as ``plan --profile`` takes it. Given,
    every Simulation that pins a profile is read against it as ``plan`` reads it (see the module
    docstring, check 7); not given, no profile is read and the checks are the ones that need none.
    """
    directory = Path(bundle_directory)
    try:
        loaded = load_bundle(directory, tolerate_additive=tolerate_additive)
    except FluxError as error:
        return StageResult(
            stage="validate",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )

    simulations: Dict[str, Any] = {}
    problems: List[str] = []
    warnings: List[str] = []
    codes: Set[str] = set()
    unchecked: List[str] = []
    for simulation_id in sorted(loaded.simulation_ids()):
        try:
            simulations[simulation_id], is_unchecked = _validate_simulation(
                loaded, simulation_id, profile, warnings
            )
            if is_unchecked:
                unchecked.append(simulation_id)
        except FluxError as error:
            problems.append(f"{simulation_id}: {error}")
            codes.add(error.code.code)

    if problems:
        return StageResult(
            stage="validate",
            exit_code=1,
            _artifacts={"simulations": simulations, "problems": problems},
            stderr="; ".join(problems),
            # The code every problem shares, as a refusal of the whole bundle
            # reports its own: a block refused with FLX-5030 is refused with
            # the code `verify` gives it. Problems with different codes have
            # no one code to report.
            error_code=next(iter(codes)) if len(codes) == 1 else None,
        )

    if unchecked:
        warnings.append(unchecked_line())
    return StageResult(
        stage="validate",
        exit_code=0,
        _artifacts={
            "documents": len(loaded.flux_documents) + len(loaded.fluid_documents),
            "fluxDocuments": len(loaded.flux_documents),
            "fluidDocuments": len(loaded.fluid_documents),
            "simulations": simulations,
            **({"warnings": warnings} if warnings else {}),
        },
        stdout=(
            f"{directory}: valid -- {len(loaded.flux_documents)} FLUX document(s), "
            f"{len(loaded.fluid_documents)} FLUID contract(s), "
            f"{len(simulations)} Simulation(s)" + "".join(f"\nwarning: {line}" for line in warnings)
        ),
    )
