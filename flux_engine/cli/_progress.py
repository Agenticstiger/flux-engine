# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Stage-boundary progress lines for the long-running commands.

``fluxsim plan``/``apply``/``run``/``replay``/``verify``/``ship`` do tens of
seconds of work on a real bundle and, until this module existed, printed
nothing at all until they were finished -- a frozen terminal a reader
cannot tell from a hang. This is the smallest thing that fixes that
without touching a single byte of what the commands actually emit:

* the lines go to **stderr**, never stdout, so a piped ``fluxsim plan >
  plan.txt`` and a ``--json`` envelope stay byte-identical;
* they are written only when stderr is a terminal, so a CI log, a
  ``2>&1 | tee`` capture and a test's captured stderr are unchanged;
* ``--json`` suppresses them outright -- a caller asking for machine
  output has said it is not a human watching;
* ``FLUX_NO_PROGRESS`` turns them off for a human who does not want them.

**And it ticks while it works.** One opening line and then nothing for 44
seconds is a frozen terminal too -- an opening line does not distinguish
"still going" from "hung". A daemon thread prints the elapsed seconds
every :data:`DEFAULT_HEARTBEAT_SECONDS` through the same gate as the lines
around it, so it appears on a terminal and nowhere else.

Nothing here reaches a digest: the elapsed seconds are wall-clock, they
are printed and discarded, and no run artifact records them. That is why
this module lives under ``cli/`` and not in the kernel, whose packages
are forbidden ``time`` outright.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import IO, Iterator, Optional

__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "ENV_VAR",
    "progress_enabled",
    "stage_progress",
]

#: How often the ticker says the stage is still running. Long enough that a
#: five-second command prints nothing extra, short enough that a reader
#: never waits longer than this to learn the process is alive.
DEFAULT_HEARTBEAT_SECONDS = 5.0

#: Set to any non-empty value to silence the progress lines.
ENV_VAR = "FLUX_NO_PROGRESS"


def progress_enabled(*, as_json: bool, stream: Optional[IO[str]] = None) -> bool:
    """Whether stage-boundary lines should be written to *stream*.

    A terminal, no ``--json``, and no ``FLUX_NO_PROGRESS``. Anything else
    -- a pipe, a file, a captured stream in a test -- reads as "not a
    human watching this happen", and the command stays silent.
    """
    if as_json:
        return False
    if os.environ.get(ENV_VAR):
        return False
    target = sys.stderr if stream is None else stream
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):  # a closed or non-file-like stream
        return False


@contextmanager
def stage_progress(
    label: str,
    *,
    as_json: bool,
    stream: Optional[IO[str]] = None,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> Iterator[None]:
    """Announce *label* on entry, tick while it runs, report its seconds on exit.

    ``label`` is the present participle of what the command is about to do
    plus its subject -- ``"planning ./bundle"`` -- so the line reads as a
    sentence and names the thing being worked on. The closing line is
    written even when the body raises, because "it stopped after 41.7s" is
    exactly the fact a reader of a failed run wants.

    The ticker between them is a **daemon** thread, so it can never hold
    the process open past the command, and it is started only after the
    gate above has already decided this is a terminal a human is watching
    -- a ``--json`` caller, a pipe and a captured stream get no thread at
    all, not a silenced one. It writes to the same stream as the lines
    around it, which is never stdout.

    *heartbeat_seconds* exists so a test does not have to wait five
    seconds to observe one tick.
    """
    target = sys.stderr if stream is None else stream
    if not progress_enabled(as_json=as_json, stream=target):
        yield
        return
    started = time.monotonic()
    print(f"{label} ...", file=target, flush=True)
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(heartbeat_seconds):
            print(f"{label} ... {time.monotonic() - started:.0f}s", file=target, flush=True)

    ticker = threading.Thread(target=beat, name="flux-progress", daemon=True)
    ticker.start()
    try:
        yield
    finally:
        stop.set()
        # Bounded: a join with no timeout would make a wedged writer (a
        # stream whose `write` blocks) hang the command it was there to
        # reassure a reader about.
        ticker.join(timeout=1.0)
        print(f"{label}: {time.monotonic() - started:.1f}s", file=target, flush=True)
