# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The FLUX specification's quickstart bundle is refused by code, not by a crash.

The quickstart (``docs/guide/quickstart.md`` in the FLUX specification
repository) builds a World, a FLUID contract and a Simulation that the offline
validator passes. ``fluxsim validate`` passed them too, and ``fluxsim run``
stopped with ``KeyError: "unknown vertical ''"`` and the generic "run again
with FLUX_DEBUG=1" hint. With ``vertical: telco`` and a one-trait Persona
added, it stopped with ``KeyError: 'start'`` instead. Through the real binary,
the documents as the guide prints them: each command now names the missing key,
the fix and the page that explains it, and writes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from flux_engine._links import error_url

from .conftest import parse_json_stdout, refused, run_flux

WORLD = """\
fluxVersion: "0.5.0"
kind: World
id: hello-world
name: Hello World
metadata:
  owner:
    team: my-team
spec:
  seed: 7
  population:
    size: 1000
    lifecycleMix: { active: 0.8, churned: 0.2 }
"""

CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: demo.gold.hello_stream
name: Hello Stream
metadata:
  owner:
    team: my-team
exposes:
  - exposeId: hello_stream
    kind: stream
    contract:
      schema:
        - name: customer_id
          type: STRING
          required: true
    binding:
      platform: kafka
      format: kafka_topic
      location:
        topic: demo.hello.v1
"""

SIMULATION = """\
fluxVersion: "0.5.0"
kind: Simulation
id: hello-sim
name: Hello Simulation
metadata:
  owner:
    team: my-team
spec:
  worldRef: hello-world
  emits:
    - productRef: demo.gold.hello_stream
      exposeId: hello_stream
      fluidVersion: "0.7.5"
"""

PERSONA = """\
fluxVersion: "0.5.0"
kind: Persona
id: hello-persona
name: Hello Persona
metadata:
  owner:
    team: my-team
spec:
  traits:
    digitalFluency: 0.6
"""

NO_VERTICAL = (
    "FLX-5042: the World names no vertical this engine ships -- World 'hello-world' declares "
    "no spec.vertical; add one under its spec, naming a pack this engine ships (telco, towers), "
    "for example 'vertical: telco'"
)
NO_TIME_BOUNDS = (
    "FLX-5023: the Simulation declares no time window -- Simulation 'hello-sim' declares no "
    "spec.timeBounds; add the window the run covers under its spec, for example "
    '\'timeBounds: {start: "2026-07-01T00:00:00Z", end: "2026-09-30T23:59:59Z"}\''
)


def _bundle(directory: Path, documents: Dict[str, str]) -> Path:
    directory.mkdir()
    for name, text in documents.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def _quickstart(tmp_path: Path) -> Path:
    return _bundle(
        tmp_path / "my-universe",
        {"world.flux.yml": WORLD, "hello.fluid.yml": CONTRACT, "sim.flux.yml": SIMULATION},
    )


def _with_vertical_and_persona(tmp_path: Path) -> Path:
    return _bundle(
        tmp_path / "my-universe",
        {
            "world.flux.yml": WORLD.replace("  seed: 7\n", "  seed: 7\n  vertical: telco\n"),
            "hello.fluid.yml": CONTRACT,
            "persona.flux.yml": PERSONA,
            "sim.flux.yml": SIMULATION.replace(
                "  worldRef: hello-world\n",
                "  worldRef: hello-world\n  personaRefs: [hello-persona]\n",
            ),
        },
    )


#: What ``fluxsim run`` prints on stdout when its plan stage refuses.
RUN_STOPPED = "apply: not run (plan exited 1)\n"


def _refusal(code: str, line: str) -> List[str]:
    """What a refused command prints on stderr: the line, then the page that explains it."""
    return [line, f"more: fluxsim explain {code}  {error_url(code)}"]


def test_validate_refuses_the_quickstart_bundle_for_its_vertical(tmp_path: Path) -> None:
    bundle = str(_quickstart(tmp_path))

    assert refused(["validate", bundle], tmp_path) == _refusal(
        "FLX-5042", f"hello-sim: {NO_VERTICAL}"
    )
    envelope = parse_json_stdout(run_flux(["validate", bundle, "--json"], cwd=tmp_path))
    assert envelope["error"] == {"code": "FLX-5042", "message": f"hello-sim: {NO_VERTICAL}"}


def test_run_refuses_the_quickstart_bundle_instead_of_crashing(tmp_path: Path) -> None:
    state = tmp_path / ".flux"
    args = ["run", str(_quickstart(tmp_path)), "--state-dir", str(state)]

    assert refused(args, tmp_path, stdout=RUN_STOPPED) == _refusal("FLX-5042", NO_VERTICAL)
    assert not state.exists()


def test_expr_refuses_a_world_with_no_vertical(tmp_path: Path) -> None:
    args = ["expr", "model", "--bundle", str(_quickstart(tmp_path))]

    assert refused(args, tmp_path) == _refusal("FLX-5042", NO_VERTICAL)


def test_with_a_vertical_and_a_persona_the_time_window_is_refused_next(tmp_path: Path) -> None:
    bundle = str(_with_vertical_and_persona(tmp_path))
    state = tmp_path / ".flux"
    run = ["run", bundle, "--state-dir", str(state)]

    assert refused(["validate", bundle], tmp_path) == _refusal(
        "FLX-5023", f"hello-sim: {NO_TIME_BOUNDS}"
    )
    assert refused(run, tmp_path, stdout=RUN_STOPPED) == _refusal("FLX-5023", NO_TIME_BOUNDS)
    assert not state.exists()
