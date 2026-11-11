# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``calibration-provenance.json``: where every dial's value came from.

One artifact, one question answered per dial: *prior source*. In v0.1 the
answer is the same for every dial -- ``"authored"``, because
:mod:`flux_engine.calibrate.declared` is the only calibration path that
exists and it never fits anything (see its module docstring). The file
exists now, with a closed and honest answer, so a v0.2 fitted calibrator
extends the *set of possible answers* (``"authored"`` joined by
``"fitted"``) rather than inventing the file's shape from scratch.

The envelope mirrors :func:`flux_engine.core.artifact.write_artifact` by
hand rather than by calling it: that function's ``EnvelopeKind`` literal
does not yet list a calibration-provenance kind (see this package's
REQUESTS in the build report). Once it does, :func:`build_calibration_provenance`
is already exactly the payload ``write_artifact`` would merge under the
same four envelope fields, so migrating is a one-line change at the call
site, not a reshape of this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Union

__all__ = [
    "PRIOR_SOURCE_AUTHORED",
    "SCHEMA_VERSION",
    "DialProvenance",
    "build_calibration_provenance",
    "write_calibration_provenance",
]

#: The only prior-source value v0.1 ever writes: nothing here is fitted.
PRIOR_SOURCE_AUTHORED = "authored"

#: Closed set a later increment grows, never breaks -- a reader pinned to
#: v1 of this schema keeps understanding every v1 document a v2 engine
#: still writes.
SCHEMA_VERSION = "calibration-provenance/1"


@dataclass(frozen=True)
class DialProvenance:
    """Where one dial's value came from, and the value itself.

    ``value`` is rendered as its exact decimal string (never a JSON
    number): KERNEL-SPEC §2.1 refuses a float in anything that ends up in
    an artifact a receipt might digest.
    """

    prior_source: str
    value: Decimal

    def to_json(self) -> Dict[str, Any]:
        return {"priorSource": self.prior_source, "value": format(self.value, "f")}


def build_calibration_provenance(
    *,
    world_ref: str,
    dials: Mapping[str, Decimal],
    generated_at: str,
    generated_by: str = "flux-engine",
) -> Dict[str, Any]:
    """Build the calibration-provenance document for *dials* on *world_ref*.

    Every dial in *dials* gets an entry; in v0.1 every entry's
    ``priorSource`` is :data:`PRIOR_SOURCE_AUTHORED` -- there is no other
    path yet (see the module docstring).
    """
    entries = {
        name: DialProvenance(prior_source=PRIOR_SOURCE_AUTHORED, value=value).to_json()
        for name, value in dials.items()
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "CalibrationProvenance",
        "generated_at": generated_at,
        "generated_by": generated_by,
        "worldRef": world_ref,
        "dials": entries,
    }


def write_calibration_provenance(
    path: Union[str, Path],
    *,
    world_ref: str,
    dials: Mapping[str, Decimal],
    generated_at: str,
    generated_by: str = "flux-engine",
) -> Path:
    """Write :func:`build_calibration_provenance`'s document to *path*.

    Sorted-key JSON with a trailing newline, matching
    :func:`flux_engine.core.artifact.write_artifact`'s byte shape exactly,
    so two runs over identical dials (modulo ``generated_at``) produce
    byte-identical files.
    """
    document = build_calibration_provenance(
        world_ref=world_ref, dials=dials, generated_at=generated_at, generated_by=generated_by
    )
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return p
