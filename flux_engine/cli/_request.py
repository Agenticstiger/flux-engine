# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --contract C`` with no profile: write the profile request (ADR 0018).

The first of the two steps of ``init`` for data whose profile does not exist
yet. The request lists every statistic, column metric and fact share the bundle
needs (docs/spec/PROFILE-REQUEST-SPEC.md); a profiler measures them where the
data lives, and ``init`` runs again with the profile it writes.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flux_engine.api.stages import StageResult

__all__ = [
    "HELD_OUT_SUFFIX",
    "REQUEST_SUFFIX",
    "default_held_out_path",
    "default_request_path",
    "run_request",
]

#: What the default request file is named after the bundle directory: ``demo`` gives
#: ``demo.profile-request.yml``, beside it, never inside it, so the directory stays
#: empty for the second step.
REQUEST_SUFFIX = ".profile-request.yml"

#: The held-out request's name beside the bundle directory (ADR 0028 section 6): ``demo`` gives
#: ``demo.held-out-request.yml``.
HELD_OUT_SUFFIX = ".held-out-request.yml"


def default_request_path(directory: str, suffix: str = REQUEST_SUFFIX) -> Path:
    """The request's default place: beside *directory*, named after it, with *suffix*."""
    target = Path(directory)
    if target.name in ("", "..") or target.parent == target:
        # ``.`` and ``..`` name no directory to sit beside: resolve them to the directory they are.
        target = target.resolve()
    name = target.name or "bundle"
    if target.parent == target:  # the filesystem root has no beside
        return Path(f"{name}{suffix}")
    return target.with_name(f"{name}{suffix}")


def default_held_out_path(directory: str, request: Optional[str] = None) -> Path:
    """Where the held-out request goes: beside the request, named after the bundle directory.

    With ``--request FILE`` it is named after that file instead, with its extension replaced.
    """
    if request is not None:
        target = Path(request)
        return target.with_name(f"{target.stem}{HELD_OUT_SUFFIX}")
    return default_request_path(directory).with_name(
        default_request_path(directory).name[: -len(REQUEST_SUFFIX)] + HELD_OUT_SUFFIX
    )


def _write_new(path: Path, data: bytes) -> None:
    """Create *path* exclusively and never through a link; a name already taken is refused.

    The file is created readable and writable by its owner only (mode ``0600``):
    it names the contract's columns and rules, and whoever hands it to a
    profiler can widen that with ``chmod``.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _floor_columns(document: Any, asked: Any) -> List[Tuple[str, str, str, str]]:
    """Each labelled column of the request whose floor a profile of exact statistics does not meet.

    A profile that releases any statistic exactly grades P1 (ADR 0020), so a label whose floor is
    above P1 (``pii`` and the rest of the P3 labels) is met only by a profile that releases every
    statistic under noise. Returned as ``(exposeId, column, sensitivity, minimum)``, in name order.
    The built-in floors are the ones a bundle written now carries, as in ``init``'s own check.
    """
    from flux_engine.core.grades import Pattern, at_least
    from flux_engine.governance.privacy import effective_floors, sensitivities_of
    from flux_engine.seam.keys import declared_exposes

    exposes = declared_exposes(document)
    asked_columns = {
        table.expose_id: {column.name for column in table.columns} for table in asked.tables
    }
    labels = sensitivities_of(
        [(name, exposes[name]) for name in sorted(asked_columns) if name in exposes]
    )
    floors = effective_floors(None)
    return [
        (expose_id, column, label, floors[label].value)
        for (expose_id, column), label in sorted(labels.items())
        if column in asked_columns[expose_id]
        and label in floors
        and not at_least(Pattern.P1, floors[label])
    ]


def _consumer_ask(document: Any, consumers: Optional[str]) -> Tuple[Any, Any]:
    """The consumers plan and the ask its registry puts in the request, or ``(None, None)``."""
    if consumers is None:
        return None, None
    from flux_engine.author.consumers import plan_consumers
    from flux_engine.consumers.ask import build_ask
    from flux_engine.seam.keys import compile_keys

    plan = plan_consumers(Path(consumers), document, compile_keys(document))
    return plan, build_ask(plan.registry, plan.checked, plan.schema)


def _held_out_text(ask: Any, digest: str, asked: Any) -> Optional[str]:
    """The held-out request, rendered, when the consumers ask holds queries out; else ``None``."""
    from flux_stats.request import ProfileRequest, RequestTable, check_request, render_request

    if ask is None or not ask.held_out:
        return None
    held_out = ProfileRequest(
        contract_digest=digest,
        fractional_bits=asked.fractional_bits,
        tables=tuple(
            RequestTable(expose_id=name, row_count=False, columns=())
            for name in ask.held_out_tables
        ),
        queries=ask.held_out,
    )
    check_request(held_out)
    return render_request(held_out)


def _write_requests(
    target: Path, text: str, held_out_target: Path, held_out_text: Optional[str]
) -> Optional[StageResult]:
    """Write the request and the held-out request; the refusal as a result, or ``None`` when written."""
    from flux_engine.seam.coverage import printable

    if held_out_text is not None and os.path.lexists(held_out_target):
        return StageResult(
            stage="init",
            exit_code=1,
            stderr=(
                f"init: {printable(str(held_out_target))} exists, and init never overwrites; "
                "move it, or name another request with --request, which names the held-out "
                "request beside it"
            ),
        )
    try:
        _write_new(target, text.encode("utf-8"))
        if held_out_text is not None:
            _write_new(held_out_target, held_out_text.encode("utf-8"))
    except FileExistsError:
        return StageResult(
            stage="init",
            exit_code=1,
            stderr=(
                f"init: {printable(str(target))} exists, and init never overwrites; "
                "name another file with --request"
            ),
        )
    except OSError as error:
        return StageResult(
            stage="init",
            exit_code=1,
            stderr=f"init: cannot write {printable(str(target))}: {error.strerror or error}",
        )
    return None


def _grade_lines(below: List[Tuple[str, str, str, str]]) -> List[str]:
    """The note on the columns whose sensitivity sets a minimum grade, or none."""
    from flux_engine.seam.coverage import printable

    if not below:
        return []
    shown = ", ".join(
        f"{printable(expose_id)}.{printable(column)} is {label} (needs {minimum} or stronger)"
        for expose_id, column, label, minimum in below[:5]
    )
    more = f", and {len(below) - 5} more" if len(below) > 5 else ""
    return [
        "",
        f"grade: a column's sensitivity sets a minimum grade for the run: {shown}{more}. "
        "The profile must release every statistic it publishes under noise "
        "(docs/spec/PROFILE-SPEC.md section 3a), or `plan` refuses the bundle (FLX-4005)",
    ]


def _consumer_lines(
    plan: Any, ask: Any, workload_sets: int, held_out_target: Path, wrote_held_out: bool
) -> List[str]:
    """The note on the consumers the request was built from."""
    from flux_engine.seam.coverage import printable

    lines = [
        "",
        f"consumers: {len(plan.registry.queries)} "
        f"{'query' if len(plan.registry.queries) == 1 else 'queries'} read with sqlglot "
        f"{plan.sqlglot_version}, {len(plan.requirements)} "
        f"{'requirement' if len(plan.requirements) == 1 else 'requirements'} to apply once a "
        "profile is given; pass the same --consumers file with --profile",
        f"the request asks for the rate of {len(ask.queries)} "
        f"{'query' if len(ask.queries) == 1 else 'queries'} and lists {workload_sets} "
        f"{'set' if workload_sets == 1 else 'sets'} of columns the queries read together",
    ]
    if wrote_held_out:
        lines.append(
            f"wrote the held-out request {printable(str(held_out_target))}: "
            f"{len(ask.held_out)} {'query' if len(ask.held_out) == 1 else 'queries'}, no "
            "column"
        )
    return lines


def _request_artifacts(
    target: Path,
    digest: str,
    asked: Any,
    columns: int,
    steps: List[str],
    below: List[Tuple[str, str, str, str]],
    ask: Any,
    workload_sets: int,
    held_out_target: Optional[Path],
) -> Dict[str, Any]:
    """The artifacts of a written request; the queries' counts only when consumers gave an ask."""
    return {
        "request": str(target),
        "contractDigest": digest,
        "tables": len(asked.tables),
        "columns": columns,
        "relationships": len(asked.relationships),
        "facts": len(asked.facts),
        "next": steps,
        **(
            {
                "queries": len(ask.queries),
                "workload": workload_sets,
                "heldOutRequest": str(held_out_target) if held_out_target is not None else None,
                "heldOutQueries": len(ask.held_out),
            }
            if ask is not None
            else {}
        ),
        "grade": {
            "belowMinimum": [
                {
                    "exposeId": expose_id,
                    "column": column,
                    "sensitivity": label,
                    "minimum": minimum,
                }
                for expose_id, column, label, minimum in below
            ]
        },
    }


def run_request(
    directory: str, contract: str, request: Optional[str], *, consumers: Optional[str] = None
) -> StageResult:
    """Write the profile request *contract* implies, and report it as a :class:`StageResult`.

    Exit ``1`` when the contract cannot be used (the message names it and, for a
    refusal the engine shares, its ``FLX`` code) or the request cannot be created.
    A request file that exists is never overwritten.
    """
    from flux_engine.author import AuthorError
    from flux_engine.author.bundle import _read_contract
    from flux_engine.core.digest import digest_of_document
    from flux_engine.core.errors import FluxError
    from flux_engine.seam.coverage import printable
    from flux_engine.seam.profile_request import request_for_contract
    from flux_stats.request import check_request, render_request

    target = Path(request) if request is not None else default_request_path(directory)
    held_out_target = default_held_out_path(directory, request)
    try:
        document, _ = _read_contract(Path(contract))
        digest = digest_of_document(document, subject="contract").value
        plan, ask = _consumer_ask(document, consumers)
        asked = request_for_contract(
            document,
            contract_digest=digest,
            queries=ask.queries if ask is not None else (),
            reads=ask.reads if ask is not None else (),
        )
        check_request(asked)
        held_out_text = _held_out_text(ask, digest, asked)
    except AuthorError as error:
        return StageResult(stage="init", exit_code=1, stderr=f"init: {printable(str(error))}")
    except FluxError as error:
        return StageResult(
            stage="init",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=f"init: {printable(str(error))}",
            error_code=error.code.code,
        )
    text = render_request(asked)
    refused = _write_requests(target, text, held_out_target, held_out_text)
    if refused is not None:
        return refused
    columns = sum(len(table.columns) for table in asked.tables)
    shown = printable(str(target))
    steps = [
        f"measure what {shlex.quote(str(target))} lists with your profiler, and write a profile",
        f"fluxsim init {shlex.quote(directory)} --contract {shlex.quote(contract)} "
        "--profile <the profile>",
    ]
    below = _floor_columns(document, asked)
    lines = [
        f"wrote the profile request {shown}: {len(asked.tables)} "
        f"{'table' if len(asked.tables) == 1 else 'tables'}, {columns} "
        f"{'column' if columns == 1 else 'columns'}, {len(asked.relationships)} "
        f"{'relationship' if len(asked.relationships) == 1 else 'relationships'}, "
        f"{len(asked.facts)} {'fact' if len(asked.facts) == 1 else 'facts'}",
    ]
    lines += _grade_lines(below)
    workload_sets = sum(len(table.workload) for table in asked.tables)
    if plan is not None and ask is not None:
        wrote_held_out = held_out_text is not None
        lines += _consumer_lines(plan, ask, workload_sets, held_out_target, wrote_held_out)
        steps[-1] += f" --consumers {shlex.quote(str(consumers))}"
        if wrote_held_out:
            steps.insert(
                1,
                f"measure {shlex.quote(str(held_out_target))} too: its profile is read by "
                "`verify` and `calibrate` only, and no bundle pins it",
            )
    lines += [
        "",
        "next:",
        *[f"  {step}" for step in steps],
    ]
    return StageResult(
        stage="init",
        exit_code=0,
        _artifacts=_request_artifacts(
            target,
            digest,
            asked,
            columns,
            steps,
            below,
            ask,
            workload_sets,
            held_out_target if held_out_text is not None else None,
        ),
        stdout="\n".join(lines),
    )
