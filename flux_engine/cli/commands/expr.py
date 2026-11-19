# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim expr``: evaluate a flux-expr/1 expression, or print an attribute model."""

from __future__ import annotations

import argparse
import json
from typing import Dict, Tuple

from flux_engine.cli._args import _BUNDLE_HELP, _SIMULATION_ID_HELP, _add_json_flag
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "expr": (Example("fluxsim expr model --bundle ./bundle", "every attribute in scope"),),
    "expr eval": (
        Example(
            'fluxsim expr eval "tenureMonths > 12" --bundle ./bundle \\\n'
            "    --values '{\"tenureMonths\": 24}'",
            "check an expression and evaluate it against one subject",
        ),
    ),
    "expr model": (Example("fluxsim expr model --bundle ./bundle", "every attribute in scope"),),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim expr`` to *sub*, the commands of the ``fluxsim`` parser."""
    expr_p = sub.add_parser(
        "expr",
        help="flux-expr/1: evaluate an expression, print an attribute model",
        description="Parse, check and evaluate one flux-expr/1 expression, or print the model.",
    )
    expr_sub = expr_p.add_subparsers(dest="expr_command", title="commands", metavar="<command>")
    eval_p = expr_sub.add_parser(
        "eval",
        help="parse, check and evaluate one expression",
        description="Evaluate one expression against a subject view supplied as JSON.",
        # `tenure_months`, the spelling this example shipped with, is not an
        # attribute of any bundle in this repository: the one worked example
        # the expression language ships exited 1 against the project's own
        # example bundle. The attribute is `tenureMonths`. The "Model your
        # customers" how-to (docs/how-to/model-customers.md) has a worked
        # `expr eval` call against a bundle that exists.
    )
    eval_p.add_argument("source", help="the expression source")
    eval_p.add_argument("--bundle", required=True, help=_BUNDLE_HELP)
    eval_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    eval_p.add_argument("--values", required=True, help="a subject view as JSON")
    _add_json_flag(eval_p)
    model_p = expr_sub.add_parser(
        "model",
        help="print the attrmodel/1 document for a bundle",
        description="Print the attribute model an expression in this bundle is checked against.",
    )
    model_p.add_argument("--bundle", required=True, help=_BUNDLE_HELP)
    model_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    _add_json_flag(model_p)

    eval_p.set_defaults(stage_name="expr-eval")
    model_p.set_defaults(stage_name="expr-model")


def _cmd_expr_eval(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.expr import build_attribute_model, check, compile_expr, parse
    from flux_engine.expr.compile import make_view
    from flux_engine.expr.errors import UnknownPathError, with_known_attributes
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.resolve import load_bundle
    from flux_engine.verticals import pack_of_world

    loaded = load_bundle(args.bundle)
    cast = resolve_cast(loaded.flux_documents, simulation_id=args.simulation_id)
    pack = pack_of_world(cast.world)
    model = build_attribute_model(
        type("BundleView", (), {"flux_docs": loaded.flux_documents})(),
        pack,
        simulation_id=cast.simulation_id,
    )
    # --values first: a JSON typo in it was being reported as an error in
    # the EXPRESSION, because the expression was checked before the values
    # were parsed at all.
    try:
        values = json.loads(args.values)
    except json.JSONDecodeError as error:
        return _print_result(
            StageResult(
                stage="expr-eval",
                exit_code=1,
                stderr=(
                    f"--values is not valid JSON (line {error.lineno}, "
                    f"column {error.colno}): a subject view is a JSON object, "
                    "e.g. '{\"tenureMonths\": 24}'"
                ),
            ),
            as_json=args.json,
        )
    node = parse(args.source)
    # The engine holds the whole attribute model in memory here; an
    # FLX-6004 that does not print it is withholding the one list that
    # fixes the error in a keystroke.
    try:
        check(node, model.subject)
    except UnknownPathError as error:
        raise with_known_attributes(error, model.subject.aliases) from None
    predicate = compile_expr(node, model.subject)
    view = make_view(model.subject, values)
    result = predicate(view)
    return _print_result(
        StageResult(
            stage="expr-eval",
            exit_code=0,
            _artifacts={"result": result},
            stdout=json.dumps(result),
        ),
        as_json=args.json,
    )


def _cmd_expr_model(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.expr import build_attribute_model
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.resolve import load_bundle
    from flux_engine.verticals import pack_of_world

    loaded = load_bundle(args.bundle)
    cast = resolve_cast(loaded.flux_documents, simulation_id=args.simulation_id)
    pack = pack_of_world(cast.world)
    model = build_attribute_model(
        type("BundleView", (), {"flux_docs": loaded.flux_documents})(),
        pack,
        simulation_id=cast.simulation_id,
    )
    document = model.to_document()
    return _print_result(
        StageResult(
            stage="expr-model",
            exit_code=0,
            _artifacts=document,
            stdout=json.dumps(document, indent=2, sort_keys=True),
        ),
        as_json=args.json,
    )
