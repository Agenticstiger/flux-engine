# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``ColumnLineageDatasetFacet`` (1-2-0) that ``flux_engine.lineage.emitter`` builds.

The facet is built by hand, like every other facet in that module, so the
engine needs no OpenLineage import to write one. What keeps it honest is the
last test here: with the ``lineage`` extra installed, the facet is read by the
client's own generated classes, which refuse a key they do not know.
"""

from __future__ import annotations

import json

import pytest

from flux_engine.lineage.emitter import (
    COLUMN_LINEAGE_SCHEMA_URL,
    OPENLINEAGE_PRODUCER,
    column_input_field,
    column_lineage_facet,
)

pytestmark = pytest.mark.unit

_AMOUNT = column_input_field(
    namespace="flux://sim-1",
    name="event",
    field="data.amount",
    description="named by the vertical's alias table",
)


def test_an_input_field_is_an_exact_unmasked_direct_copy() -> None:
    assert _AMOUNT == {
        "namespace": "flux://sim-1",
        "name": "event",
        "field": "data.amount",
        "transformations": [
            {
                "type": "DIRECT",
                "subtype": "IDENTITY",
                "description": "named by the vertical's alias table",
                "masking": False,
            }
        ],
    }


def test_the_facet_keeps_every_column_and_gives_one_with_no_input_an_empty_list() -> None:
    facet = column_lineage_facet([("amount", [_AMOUNT]), ("note", [])])
    assert facet["_producer"] == OPENLINEAGE_PRODUCER
    assert facet["_schemaURL"] == COLUMN_LINEAGE_SCHEMA_URL
    assert facet["fields"] == {
        "amount": {"inputFields": [_AMOUNT]},
        "note": {"inputFields": []},
    }
    assert facet["dataset"] == [], "FLUX records no filter, join or grouping over a dataset"


def test_the_facet_is_a_copy_so_a_caller_cannot_move_it_by_editing_its_inputs() -> None:
    inputs = [dict(_AMOUNT)]
    facet = column_lineage_facet([("amount", inputs)])
    inputs[0]["field"] = "moved"
    assert facet["fields"]["amount"]["inputFields"][0]["field"] == "data.amount"


def test_the_facet_is_json_and_the_same_bytes_every_time() -> None:
    first = json.dumps(column_lineage_facet([("amount", [_AMOUNT])]), sort_keys=True)
    second = json.dumps(column_lineage_facet([("amount", [_AMOUNT])]), sort_keys=True)
    assert first == second


def test_the_openlineage_client_reads_the_facet_as_its_own_1_2_0_class() -> None:
    """The clients' generated classes know exactly the keys the spec names.

    Building each one from the dictionaries this module produces fails with a
    ``TypeError`` on a key the class does not have, and the facet's
    ``_schemaURL`` must be the one the class itself reports.
    """
    generated = pytest.importorskip("openlineage.client.generated.column_lineage_dataset")
    facet = column_lineage_facet([("amount", [_AMOUNT]), ("note", [])])

    assert facet["_schemaURL"] == generated.ColumnLineageDatasetFacet._get_schema()
    fields = {
        column: generated.Fields(
            inputFields=[
                generated.InputField(
                    **{
                        **item,
                        "transformations": [
                            generated.Transformation(**transformation)
                            for transformation in item["transformations"]
                        ],
                    }
                )
                for item in entry["inputFields"]
            ]
        )
        for column, entry in facet["fields"].items()
    }
    built = generated.ColumnLineageDatasetFacet(fields=fields, dataset=facet["dataset"])
    assert sorted(built.fields) == ["amount", "note"]
    assert built.fields["amount"].inputFields[0].transformations[0].subtype == "IDENTITY"
