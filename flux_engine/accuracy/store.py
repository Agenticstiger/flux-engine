# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The on-disk layout of the outcome-accuracy state, and every read and write of it.

The seal is the integrity anchor of the whole outcome-accuracy claim: a
prediction is digested with its ``registered_at`` and written down before
the outcome is known, so any later edit produces a different digest rather
than a quietly updated one. That write used to live inside the CLI's own
command handlers -- presentation-layer code -- so the layout, the
containment check and the sealed-record bytes were unreachable to a
library host, and a second consumer had to re-derive all three by reading
argparse handlers.

This module owns them instead. It is deliberately small and total:

* :class:`AccuracyStore` knows the two directories and nothing about
  argparse, exit codes or output rendering;
* every path it returns is one identifier (``FLX-7003`` for any other
  spelling) under the store's directory, so a prediction id that tried to
  climb out of it is refused before anything is written;
* the bytes it writes are the same sorted-key JSON the CLI wrote, so a
  state directory written by an earlier build reads back unchanged.

The seal holds only if the store keeps it, which it does in three places:

* **Written once.** :meth:`AccuracyStore.register` publishes a sealed
  record whole or not at all, and never replaces one. Registering the same
  claim again (a document that seals to the same digest) succeeds and
  writes nothing; a different claim under a registered id is ``FLX-7007``,
  which names the record and both digests.
* **Verified on every read.** :meth:`AccuracyStore.read_prediction` and
  :meth:`AccuracyStore.documents_registered_at` recompute the seal from a
  record's fields and compare it with the ``sealDigest`` stored beside
  them. A record that does not match, is not a sealed PredictionRecord, or
  is the record of another id than its file's name is ``FLX-7008``, so
  nothing is scored or published from it.
* **No link followed.** Every record is read and written through a
  descriptor of its directory, and the store's own directory and the two
  inside it are opened with ``O_NOFOLLOW``
  (:mod:`flux_engine.accuracy._entries`). Nothing below the state
  directory is resolved: a link, or the wrong type of file, where a record
  or one of those three directories belongs is ``FLX-7009``. A verdict
  written over a link replaces the link, and the file it pointed at is not
  touched.

The seal is a digest, not a signature: whoever can write the store can
write a matching ``sealDigest`` too. What the check on read catches is a
record changed without being sealed again; what a third party checks a
record against is the digest published when it was registered, which
``register`` prints and ``fluxsim accuracy record`` lists.

The CLI now parses arguments and prints results; the layout lives here.
"""

from __future__ import annotations

import errno
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from flux_engine.accuracy._entries import (
    EntryRefused,
    create_once,
    list_names,
    open_directory,
    read_entry,
    replace_entry,
)
from flux_engine.accuracy.records import FLX_7007, FLX_7008, FLX_7009, seal_prediction
from flux_engine.api.accuracy import (
    FLX_7012,
    AccuracyVerdict,
    PredictionRecord,
    require_identifier,
)
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.core.errors import FluxError

__all__ = ["DEFAULT_STATE_DIR", "AccuracyStore", "Registration", "prediction_from_document"]

#: Where the state lives when a caller names no directory -- the same
#: default every other stage uses.
DEFAULT_STATE_DIR = ".flux"

_PREDICTIONS = "predictions"
_VERDICTS = "verdicts"
_SUFFIX = ".json"

#: How many times ``register`` publishes again when the entry that refused
#: the first publish is gone before it can be read.
_PUBLISH_ATTEMPTS = 3


def _record_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(document), indent=2, sort_keys=True) + "\n").encode("utf-8")


def prediction_from_document(document: Mapping[str, Any]) -> PredictionRecord:
    """The :class:`~flux_engine.api.accuracy.PredictionRecord` a registration document describes.

    The one reading of the document, shared by ``register`` (the caller's
    file), by ``score`` and ``record`` (the sealed record) and by the check
    on read, so a record is verified against its seal exactly the way it
    was sealed. A document that is not a PredictionRecord raises
    ``KeyError``, ``TypeError``, ``ValueError`` or
    ``decimal.InvalidOperation``, and an unsafe ``prediction_id`` raises
    ``FLX-7003``.
    """
    return PredictionRecord(
        prediction_id=document["prediction_id"],
        run_digest=DigestRef(
            value=document["run_digest"], domain=DigestDomain.JCS_DOCUMENT, subject="runDigest"
        ),
        metric_id=document["metric_id"],
        statement=document["statement"],
        predicted=Decimal(str(document["predicted"])),
        horizon_end=document["horizon_end"],
        registered_at=document["registered_at"],
        # Presence, not truthiness: the store keeps the submitted document
        # verbatim and a decimal field may be the JSON number 0, which is
        # falsy. The interval is part of what the seal covers.
        interval_low=(
            Decimal(str(document["interval_low"])) if "interval_low" in document else None
        ),
        interval_high=(
            Decimal(str(document["interval_high"])) if "interval_high" in document else None
        ),
        # The two members a forecast seals (ADR 0029 section 4); absent in an older record.
        state_digest=_optional_text(document, "stateDigest"),
        input_digest=_optional_text(document, "inputDigest"),
    )


def _optional_text(document: Mapping[str, Any], key: str) -> Optional[str]:
    if key not in document:
        return None
    value = document[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} is a digest text")
    return value


#: What reading a document that is not a PredictionRecord can raise.
_NOT_A_PREDICTION = (ArithmeticError, LookupError, TypeError, ValueError, FluxError)


def _why_not_a_prediction(error: BaseException) -> str:
    if isinstance(error, KeyError):
        return f"it has no {error.args[0]!r}"
    if isinstance(error, ArithmeticError):
        return "a decimal field is not a number"
    return str(error)


@dataclass(frozen=True)
class Registration:
    """What one :meth:`AccuracyStore.register` call left in the store."""

    #: The sealed record's path.
    path: Path
    #: The seal of the registered claim.
    seal: DigestRef
    #: ``False`` when the same claim was registered already: nothing was written.
    created: bool


@dataclass(frozen=True)
class AccuracyStore:
    """``<state_dir>/accuracy/``: sealed predictions and the verdicts on them.

    ``predictions/<predictionId>.json`` holds the caller's own document
    plus the ``sealDigest`` taken over it; ``verdicts/<predictionId>.json``
    holds the outcome of scoring it. One directory per kind, one file per
    prediction id, so a prediction and its verdict are found by the same
    name.
    """

    root: Path

    @classmethod
    def for_state_dir(cls, state_dir: Union[str, Path, None]) -> "AccuracyStore":
        """The store under *state_dir* (default ``./.flux``)."""
        return cls(root=Path(state_dir or DEFAULT_STATE_DIR) / "accuracy")

    @property
    def predictions_dir(self) -> Path:
        return self.root / _PREDICTIONS

    @property
    def verdicts_dir(self) -> Path:
        return self.root / _VERDICTS

    @property
    def _base(self) -> Path:
        """The store's directory under its holder, the holder resolved and the store not.

        The state directory that holds the store is the operator's choice, so
        a link there is followed; the store's own directory, the two inside it
        and the records are never resolved, because a link in place of any of
        them is refused (``FLX-7009``) when it is opened. Resolving one first
        would follow it, and answer for wherever it points.
        """
        return self.root.parent.resolve() / self.root.name

    def prediction_path(self, prediction_id: str) -> Path:
        """The sealed-record path for *prediction_id*: one identifier in ``predictions``.

        The identifier grammar (``FLX-7003``) keeps the name a single path
        component, so the path cannot leave the store.
        """
        require_identifier(prediction_id, field_name="prediction_id")
        return self._base / _PREDICTIONS / f"{prediction_id}{_SUFFIX}"

    def verdict_path(self, prediction_id: str) -> Path:
        """The verdict path for *prediction_id*: one identifier in ``verdicts``."""
        require_identifier(prediction_id, field_name="prediction_id")
        return self._base / _VERDICTS / f"{prediction_id}{_SUFFIX}"

    def register(self, prediction: PredictionRecord, document: Mapping[str, Any]) -> Registration:
        """Seal *prediction* and write *document* plus its ``sealDigest``, once.

        *document* is the caller's own record as it was read, so the file on
        disk is what was submitted rather than a re-rendering of it; the
        digest is taken over the model, not over the file, and *document*
        must describe the same claim (``ValueError`` otherwise, so the store
        never writes a record its own check on read would refuse).

        A record already registered under the id is never replaced. When it
        seals to the same digest, the call succeeds and writes nothing
        (:attr:`Registration.created` is ``False``); otherwise it is
        ``FLX-7007``. A registered record that does not verify is
        ``FLX-7008``, and a link at the record's name is ``FLX-7009``.
        """
        # The path -- identifier grammar and containment -- before anything
        # is created, so a refused prediction id leaves no directory behind.
        path = self.prediction_path(prediction.prediction_id)
        seal = seal_prediction(prediction)
        _require_same_claim(document, seal)
        data = _record_bytes({**document, "sealDigest": seal.value})
        directory = _open_kind(self.root, self._base, _PREDICTIONS, create=True)
        assert directory is not None  # created above
        try:
            for _attempt in range(_PUBLISH_ATTEMPTS):
                if create_once(directory, path.name, data):
                    return Registration(path=path, seal=seal, created=True)
                raw = read_entry(directory, path.name)
                if raw is not None:
                    return _registered_again(path, raw, prediction.prediction_id, seal, document)
        except EntryRefused as refused:
            raise FluxError(FLX_7009, _not_followed(path, str(refused))) from None
        finally:
            os.close(directory)
        raise OSError(
            errno.EAGAIN,
            f"{path} was created and removed {_PUBLISH_ATTEMPTS} times while it was being "
            "registered; run the command again",
        )

    def check_registrable(self, prediction: PredictionRecord, document: Mapping[str, Any]) -> None:
        """Raise what :meth:`register` would raise before it writes, and write nothing.

        The same refusals in the same order: the identifier and path, a document that is not the
        claim (``ValueError``), a record under the id that does not verify (``FLX-7008``,
        ``FLX-7009``) and a registered record of another claim (``FLX-7007``). A caller that
        registers several records checks all of them first, so a refusal leaves none written.
        What it cannot foresee is a write that fails (a full disk).
        """
        path = self.prediction_path(prediction.prediction_id)
        seal = seal_prediction(prediction)
        _require_same_claim(document, seal)
        directory = _open_kind(self.root, self._base, _PREDICTIONS, create=False)
        if directory is None:
            return
        try:
            raw = read_entry(directory, path.name)
        except EntryRefused as refused:
            raise FluxError(FLX_7009, _not_followed(path, str(refused))) from None
        finally:
            os.close(directory)
        if raw is not None:
            _registered_again(path, raw, prediction.prediction_id, seal, document)

    def read_prediction(self, prediction_id: str) -> Optional[Dict[str, Any]]:
        """The sealed record for *prediction_id*, verified, or ``None`` when none is registered.

        A record that does not match its seal is ``FLX-7008``; a link or
        the wrong type of file in its place is ``FLX-7009``.
        """
        path = self.prediction_path(prediction_id)
        raw = _read_record(self.root, self._base, _PREDICTIONS, path)
        if raw is None:
            return None
        return _verified(path, raw, prediction_id)

    def write_verdict(
        self, verdict: AccuracyVerdict, extra: Optional[Mapping[str, Any]] = None
    ) -> Path:
        """Write the outcome of scoring one prediction, and return its path.

        A verdict may be written again (a prediction scored once more), so
        it replaces the one before through a staged file and one
        ``os.replace``: it is never half-written, and a link at its name is
        replaced rather than written through.

        *extra* holds the optional members a forecast's verdict carries beside the four
        every verdict has (ADR 0029 section 10): the observation, the two scores, the grades
        and the drift. A verdict written without them is the document it always was, and a
        member of *extra* never replaces one of the four.
        """
        path = self.verdict_path(verdict.prediction_id)
        data = _record_bytes(
            {
                **(extra or {}),
                "prediction_id": verdict.prediction_id,
                "metric_id": verdict.metric_id,
                "outcome": verdict.outcome.value,
                "basis": verdict.basis.value,
            }
        )
        directory = _open_kind(self.root, self._base, _VERDICTS, create=True)
        assert directory is not None  # created above
        try:
            replace_entry(directory, path.name, data)
        except EntryRefused as refused:
            raise FluxError(FLX_7009, _not_followed(path, str(refused))) from None
        finally:
            os.close(directory)
        return path

    def read_verdict(self, prediction_id: str) -> Optional[Dict[str, Any]]:
        """The recorded verdict for *prediction_id*, or ``None``."""
        path = self.verdict_path(prediction_id)
        raw = _read_record(self.root, self._base, _VERDICTS, path)
        if raw is None:
            return None
        document: Dict[str, Any] = json.loads(raw.decode("utf-8"))
        return document

    def documents_registered_at(self, registered_at: str) -> List[Dict[str, Any]]:
        """Every sealed record whose ``registered_at`` is exactly *registered_at*.

        Exact string equality, not an instant comparison: ``registered_at``
        is the sealed prediction's own spelling of the moment, and two
        spellings of one instant are two registrations as far as the seal is
        concerned.

        Every record in the store is verified, not only the ones that match:
        a record whose ``registered_at`` was edited would otherwise leave the
        registration it was sealed in without a word. Every record that fails
        is named in one refusal (``FLX-7008``, or ``FLX-7009`` for a link),
        and nothing is returned while one does.
        """
        return [
            document
            for document in self.documents()
            if document.get("registered_at") == registered_at
        ]

    def documents(self) -> List[Dict[str, Any]]:
        """Every sealed record of the store, verified, in the order of their ids.

        The same verification as :meth:`documents_registered_at`, which filters this list: a
        record that fails its seal, or a link in a record's place, is refused with every other
        that fails (``FLX-7008``, ``FLX-7009``), and nothing is returned while one does. A store
        that holds no record gives an empty list.
        """
        directory_path = self._base / _PREDICTIONS
        directory = _open_kind(self.root, self._base, _PREDICTIONS, create=False)
        if directory is None:
            return []
        found: List[Dict[str, Any]] = []
        refusals: List[FluxError] = []
        try:
            for name in list_names(directory):
                # A staged file ends in ".tmp", so only records are read.
                if not name.endswith(_SUFFIX):
                    continue
                path = directory_path / name
                try:
                    raw = read_entry(directory, name)
                    if raw is None:
                        continue
                    found.append(_verified(path, raw, name[: -len(_SUFFIX)]))
                except EntryRefused as refused:
                    refusals.append(FluxError(FLX_7009, _not_followed(path, str(refused))))
                except FluxError as refusal:
                    refusals.append(refusal)
        finally:
            os.close(directory)
        if refusals:
            raise _all_of(refusals, directory_path)
        return found


def _open_kind(root: Path, base: Path, kind: str, *, create: bool) -> Optional[int]:
    """A descriptor on the store's directory *kind*.

    A link in place of the store's own directory or of *kind* is ``FLX-7009``,
    naming whichever of the two it is under *base*, the store's directory as
    the store shows it.
    """
    try:
        return open_directory(root, kind, create=create)
    except EntryRefused as refused:
        shown = base if refused.entry == root else base / kind
        raise FluxError(FLX_7009, _not_followed(shown, str(refused))) from None


def _read_record(root: Path, base: Path, kind: str, path: Path) -> Optional[bytes]:
    """The bytes of the record at *path* (in *kind*), or ``None`` when there is none."""
    directory = _open_kind(root, base, kind, create=False)
    if directory is None:
        return None
    try:
        return read_entry(directory, path.name)
    except EntryRefused as refused:
        raise FluxError(FLX_7009, _not_followed(path, str(refused))) from None
    finally:
        os.close(directory)


def _verified(path: Path, raw: bytes, prediction_id: str) -> Dict[str, Any]:
    """The sealed record *raw*, read from *path*, refused (``FLX-7008``) unless it verifies."""

    def refusal(reason: str) -> FluxError:
        return FluxError(FLX_7008, f"the sealed record of {prediction_id!r} at {path} {reason}")

    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # undecodable, not JSON, or nested past the parser
        raise refusal("is not JSON the store can read") from None
    if not isinstance(document, dict):
        raise refusal("is not a JSON object")
    stored = document.get("sealDigest")
    if not isinstance(stored, str):
        raise refusal("carries no sealDigest")
    try:
        prediction = prediction_from_document(document)
        recomputed = seal_prediction(prediction).value
    except FluxError as error:
        if error.code is FLX_7012:
            raise FluxError(
                FLX_7012, f"the sealed record of {prediction_id!r} at {path}: {error.detail}"
            ) from None
        raise refusal(f"is not a sealed PredictionRecord: {_why_not_a_prediction(error)}") from None
    except _NOT_A_PREDICTION as error:
        raise refusal(f"is not a sealed PredictionRecord: {_why_not_a_prediction(error)}") from None
    if prediction.prediction_id != prediction_id:
        reason = f"is the record of {prediction.prediction_id!r}"
        if prediction.prediction_id.casefold() == prediction_id.casefold():
            # Not an edit: a filesystem that ignores case (macOS's default)
            # gives ids that differ only by case one file.
            reason += (
                "; the two ids differ only by case, and a filesystem that ignores case "
                "keeps them in one file, so give this prediction an id that differs by "
                "more than case"
            )
        raise refusal(reason)
    if recomputed != stored:
        raise refusal(
            f"does not match its seal: it carries sealDigest {stored}, "
            f"and its fields seal to {recomputed}"
        )
    return document


def _short(digest: str) -> str:
    """The first 16 hexadecimal digits of *digest*, whatever algorithm prefix it carries."""
    return digest.rpartition(":")[2][:16]


def _another_bundle(registered: Mapping[str, Any], submitted: Mapping[str, Any]) -> str:
    """Words for a refusal when the sealed record belongs to another bundle, else ``""``.

    Both records must carry an ``inputDigest`` and the two must differ. A forecast's id is the
    item's id and its state, so two bundles that read one state and name an item alike share
    an id.
    """
    held, given = registered.get("inputDigest"), submitted.get("inputDigest")
    if not isinstance(held, str) or not isinstance(given, str) or held == given:
        return ""
    return (
        f"; the registered record belongs to another bundle (inputDigest {_short(held)}), "
        f"and this one is {_short(given)}. An item has one prediction per state, so give the "
        "item its own id in one of the two bundles"
    )


def _registered_again(
    path: Path, raw: bytes, prediction_id: str, seal: DigestRef, document: Mapping[str, Any]
) -> Registration:
    """The outcome of registering *seal* where the record *raw* already is.

    The same claim is not a second registration, so nothing is written;
    any other claim is ``FLX-7007``. The record there is verified first, so
    a record that fails its own seal is reported as that (``FLX-7008``)
    rather than compared.
    """
    record = _verified(path, raw, prediction_id)
    registered = record["sealDigest"]
    if registered != seal.value:
        raise FluxError(
            FLX_7007,
            f"{prediction_id!r} is registered at {path} with sealDigest {registered}; "
            f"the submitted document seals to {seal.value}. Nothing was written"
            f"{_another_bundle(record, document)}",
        )
    return Registration(path=path, seal=seal, created=False)


def _require_same_claim(document: Mapping[str, Any], seal: DigestRef) -> None:
    """Raise ``ValueError`` unless *document* seals to *seal*."""
    try:
        described = seal_prediction(prediction_from_document(document)).value
    except _NOT_A_PREDICTION as error:
        raise ValueError(
            f"document is not a PredictionRecord: {_why_not_a_prediction(error)}"
        ) from None
    if described != seal.value:
        raise ValueError(
            f"document describes another claim than the prediction: it seals to {described}, "
            f"the prediction to {seal.value}"
        )


def _not_followed(path: Path, reason: str) -> str:
    return f"{path} {reason}; the accuracy store does not follow links or read special files"


def _all_of(refusals: List[FluxError], directory: Path) -> FluxError:
    """One refusal for every record that failed, carrying the first one's code."""
    if len(refusals) == 1:
        return refusals[0]
    listed = "; ".join(f"{refusal.code.code}: {refusal.detail}" for refusal in refusals)
    return FluxError(
        refusals[0].code,
        f"{len(refusals)} entries under {directory} are not sealed records that verify: {listed}",
    )
