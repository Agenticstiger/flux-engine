# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim story``: one entity's history, read from a committed run."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, Tuple

from flux_engine.cli._args import _RUN_ID_HELP, _add_json_flag, _add_state_dir, _whole_number_type
from flux_engine.cli._output import _detach_stdout, _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "story": (
        Example(
            "fluxsim story --run-id de72fa4b22711c52 --emit customer --key 42",
            "the rows that link to customer 42 and the rows it links to, two hops out",
        ),
        Example(
            "fluxsim story --run-id de72fa4b22711c52 --emit store_returns \\\n"
            "    --key sr_item_sk=7 --key sr_ticket_number=1001 --depth 1 --json",
            "one row of a composite key and its direct neighbours, one JSON object per line",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim story`` to *sub*, the commands of the ``fluxsim`` parser."""
    story_p = sub.add_parser(
        "story",
        help="one entity's history, read from a committed run",
        description=(
            "Tell the history of one row of a committed run: the rows that link to it and the "
            "rows it links to, through the relationships the run's contract declares, a few "
            "hops out, ordered by their first date or timestamp column. Each stream is "
            "checked against the receipt's outputDigest before it is read, and nothing is "
            "written. Every row of the expose that matches --key is the entity, and its rows "
            "come first. A first line says how many rows matched. Then one line per row: its "
            "date, its expose, its key, its hop (0 for the entity's own rows, n for a row "
            "reached after n relationships), the relationship it was reached by (- at hop 0), "
            "then column=value pairs, with control characters written out. With --json, a "
            "story is one JSON object per row, with hop and relationship, and no envelope; a "
            "refusal is the usual JSON envelope."
        ),
    )
    story_p.add_argument("--run-id", required=True, help=_RUN_ID_HELP)
    story_p.add_argument(
        "--emit",
        required=True,
        metavar="EXPOSE",
        help="the expose whose row is the entity, by its exposeId",
    )
    story_p.add_argument(
        "--key",
        action="append",
        required=True,
        metavar="VALUE|COLUMN=VALUE",
        help=(
            "the entity's key, compared as the canonical text of the column's value: one VALUE "
            "for the expose's one-column key, or COLUMN=VALUE once per column of a composite key"
        ),
    )
    story_p.add_argument(
        "--depth",
        default=2,
        type=_whole_number_type(0),
        metavar="N",
        help="how many links away from the entity the story reaches (default: 2)",
    )
    story_p.add_argument(
        "--limit",
        default=200,
        type=_whole_number_type(1),
        metavar="N",
        help="the most rows the story keeps, nearest the entity first; says when it cut (default: 200)",
    )
    _add_state_dir(story_p)
    _add_json_flag(story_p)

    story_p.set_defaults(stage_name="story")


def _cmd_story(args: argparse.Namespace) -> int:
    from flux_engine.stages.story import story

    result = story(
        state_dir=args.state_dir,
        run_id=args.run_id,
        expose_id=args.emit,
        keys=args.key,
        depth=args.depth,
        limit=args.limit,
    )
    if not result.success or not args.json:
        return _print_result(result, as_json=args.json and not result.success)
    # A story told is one JSON object per row, not the envelope: the rows are the output. A refusal
    # is the envelope every other command prints.
    try:
        for row in result.artifacts["rows"]:
            print(json.dumps(row, sort_keys=True))
        sys.stdout.flush()
    except BrokenPipeError:
        _detach_stdout()
        return int(result.exit_code)
    if result.stderr:
        sys.stdout.flush()
        print(result.stderr, file=sys.stderr)
    return result.exit_code
