# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The generator plugins a Simulation declares: the ``generators`` key (ADR 0032 section 2).

A learned model trained elsewhere can generate the values of an expose in place of the kernel.
Which plugin does it, which release of which distribution, and what the run may say about its
privacy are decided here, in the bundle, before any plugin code runs. A Simulation declares them
under the top-level ``generators`` key of its namespace, one entry per emitted ``exposeId``, in the
FLUX bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        generators:
          subscriber:
            plugin: learned
            distribution: my-package
            version: "1.2.0"
            grade: P4
            privacy:
              unit: one data subject with all of its records
              epsilon: "3"
              delta: "0.00001"
              accounting: a Renyi accountant over the training steps
            model: {digest: "sha256:..."}
            reproducibility: environment
            options: {temperature: "1"}

* ``plugin`` is an entry-point name, never a module path, a file or an object reference.
  ``distribution`` and ``version`` pin the distribution that must provide it, exactly.
* ``grade`` is ``P4`` or ``P5``. ``P0``, ``P1`` and ``P3`` say what a profile rests on, so a plugin
  cannot state them: a plugin that could state ``P0`` would clear every floor, ``pii`` included.
* For ``P4``, ``privacy`` holds the account in NIST SP 800-226's terms (the unit of privacy, the
  loss as ``epsilon`` and ``delta`` or as ``rho``, and the accounting) and ``model.digest`` is
  required. For ``P5``, ``privacy`` is refused and ``model.digest`` is optional. The engine checks
  the form of the account, never how the model was trained.
* ``reproducibility`` is ``bytes``, ``environment`` or ``none``.
* ``options`` is plain data: strings, exact decimals, booleans, lists and mappings. A mapping key
  anywhere inside it that names a credential is refused, and the refusal names the key and never a
  value: a bundle never holds a credential.

**The declaration changes the rows an expose generates**, so it is bound into ``H`` before any row
is drawn: :func:`bound_into_h` is the member the entry for ``H`` of the declaring expose gains,
present only then, so a bundle that declares no plugin hashes exactly as before. ``G`` keeps the
member, because the committed statistics of a plugin's expose are those of its values. Nothing here
imports, finds or calls a plugin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.credentials import credential_option, credential_value
from flux_engine.core.errors import ErrorCode, FluxError
from flux_engine.core.grades import Pattern

__all__ = [
    "GENERATORS_KEY",
    "GRADES",
    "GeneratorDeclaration",
    "PrivacyAccount",
    "REPRODUCIBILITY",
    "bound_into_h",
    "declaration_for",
    "read_generators_block",
]

#: The top-level key of the namespace that declares generator plugins, and the member an emit's
#: ``H`` gains.
GENERATORS_KEY = "generators"

#: The grades a plugin may state. ``P0``, ``P1`` and ``P3`` say what a profile rests on.
GRADES: Tuple[Pattern, ...] = (Pattern.P4, Pattern.P5)

#: What a plugin's run reproduces (ADR 0032 section 6).
REPRODUCIBILITY: Tuple[str, ...] = ("bytes", "environment", "none")

_ENTRY_KEYS = (
    "plugin",
    "distribution",
    "version",
    "grade",
    "privacy",
    "model",
    "reproducibility",
    "options",
)
_REQUIRED = ("plugin", "distribution", "version", "grade", "reproducibility")
_PRIVACY_KEYS = ("unit", "accounting", "epsilon", "delta", "rho")
_PLUGIN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
#: An exact decimal: no sign, no exponent, no float.
_DECIMAL = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
_LONGEST_TEXT = 1000
_LONGEST_DECIMAL = 1000
_MOST_ENTRIES = 4096
#: The deepest ``options`` may nest. The bundle's own read bounds a document at 64 levels.
_MOST_DEPTH = 64


def _shown(text: str) -> str:
    """*text* as a diagnostic names it: quoted when it is not plain text, and cut when it is long."""
    text = text if len(text) <= 80 else text[:80] + "..."
    return text if text.isprintable() else repr(text)


@dataclass(frozen=True)
class PrivacyAccount:
    """The account a ``P4`` declaration states, in NIST SP 800-226's terms.

    ``epsilon`` and ``delta``, or ``rho``, are exact decimals as written.
    """

    unit: str
    accounting: str
    epsilon: Optional[str] = None
    delta: Optional[str] = None
    rho: Optional[str] = None

    @property
    def loss(self) -> str:
        """The privacy loss as the bundle states it: ``epsilon = 3, delta = 0.00001`` or ``rho = 1``."""
        if self.rho is not None:
            return f"rho = {self.rho}"
        return f"epsilon = {self.epsilon}, delta = {self.delta}"

    def to_document(self) -> Dict[str, str]:
        """The account as it is bound into ``H``: each part as written, only the ones declared."""
        document = {"unit": self.unit, "accounting": self.accounting}
        for key in ("epsilon", "delta", "rho"):
            value = getattr(self, key)
            if value is not None:
                document[key] = value
        return document


@dataclass(frozen=True)
class GeneratorDeclaration:
    """One declared plugin: the expose it generates, what pins it, and what it may state.

    ``version`` is the exact version of ``distribution`` that must provide the entry point
    ``plugin``. ``options`` is plain data and is never to be changed: a copy of what the bundle
    declared. ``privacy`` is set for ``P4`` and ``None`` for ``P5``. ``model_digest`` is
    ``sha256:`` and 64 lowercase hex digits, required for ``P4``.
    """

    expose_id: str
    plugin: str
    distribution: str
    version: str
    grade: Pattern
    reproducibility: str
    privacy: Optional[PrivacyAccount] = None
    model_digest: Optional[str] = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def to_document(self) -> Dict[str, Any]:
        """The declaration as it is bound into ``H``, only the parts declared."""
        document: Dict[str, Any] = {
            "plugin": self.plugin,
            "distribution": self.distribution,
            "version": self.version,
            "grade": self.grade.value,
            "reproducibility": self.reproducibility,
        }
        if self.privacy is not None:
            document["privacy"] = self.privacy.to_document()
        if self.model_digest is not None:
            document["model"] = {"digest": self.model_digest}
        if self.options:
            document["options"] = _copy(self.options)
        return document


def declaration_for(
    declarations: Sequence[GeneratorDeclaration], expose_id: str
) -> Optional[GeneratorDeclaration]:
    """The declaration for the expose *expose_id*, or ``None`` when no plugin generates it."""
    wanted = nfc(expose_id)
    return next((item for item in declarations if item.expose_id == wanted), None)


def bound_into_h(
    declarations: Sequence[GeneratorDeclaration], expose_id: str
) -> Optional[Dict[str, Any]]:
    """The member of *expose_id*'s entry for ``H`` that its declaration makes, or ``None``.

    The declaration as :meth:`GeneratorDeclaration.to_document` writes it, when the expose has one;
    ``None`` otherwise, so the entry gains no key.
    """
    found = declaration_for(declarations, expose_id)
    return found.to_document() if found is not None else None


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy(item) for item in value]
    return value


def _text(value: Any, where: str, code: ErrorCode) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FluxError(code, f"{where} is a non-empty string")
    if len(value) > _LONGEST_TEXT:
        raise FluxError(code, f"{where} is at most {_LONGEST_TEXT} characters")
    return value


def _decimal(value: Any, where: str, code: ErrorCode) -> Tuple[Fraction, str]:
    """An exact decimal written as a string, with its exact value."""
    if not isinstance(value, str):
        raise FluxError(code, f'{where} is an exact decimal written as a string, such as "0.00001"')
    if len(value) > _LONGEST_DECIMAL or not _DECIMAL.fullmatch(value):
        raise FluxError(
            code,
            f'{where} is a decimal with no sign and no exponent, such as "0.00001", written in '
            f"at most {_LONGEST_DECIMAL} characters",
        )
    return Fraction(value), value


def _read_privacy(block: Any, where: str, code: ErrorCode) -> PrivacyAccount:
    """The account of a ``P4`` plugin; refused when a part is missing, malformed or out of range."""
    if not isinstance(block, Mapping):
        raise FluxError(
            code, f"{where} is a mapping holding unit, accounting, and epsilon and delta or rho"
        )
    unknown = sorted(_shown(str(key)) for key in block if str(key) not in _PRIVACY_KEYS)
    if unknown:
        raise FluxError(
            code, f"{where} declares {unknown}; the account carries {list(_PRIVACY_KEYS)}"
        )
    unit = _text(block.get("unit"), f"{where}.unit", code)
    accounting = _text(block.get("accounting"), f"{where}.accounting", code)
    loss = [key for key in ("epsilon", "delta", "rho") if key in block]
    if loss not in (["epsilon", "delta"], ["rho"]):
        raise FluxError(
            code,
            f"{where} states its loss as epsilon and delta, or as rho alone; it declares "
            f"{loss or 'neither'}",
        )
    if "rho" in block:
        rho, rho_text = _decimal(block["rho"], f"{where}.rho", code)
        if rho <= 0:
            raise FluxError(code, f"{where}.rho is finite and positive")
        return PrivacyAccount(unit, accounting, rho=rho_text)
    epsilon, epsilon_text = _decimal(block["epsilon"], f"{where}.epsilon", code)
    if epsilon <= 0:
        raise FluxError(code, f"{where}.epsilon is finite and positive")
    delta, delta_text = _decimal(block["delta"], f"{where}.delta", code)
    if not 0 <= delta < 1:
        raise FluxError(code, f"{where}.delta is at least 0 and below 1")
    return PrivacyAccount(unit, accounting, epsilon=epsilon_text, delta=delta_text)


def _read_model_digest(block: Any, where: str, code: ErrorCode) -> Optional[str]:
    """The ``model.digest`` an entry declares, or ``None`` when it declares no model."""
    if block is None:
        return None
    if not isinstance(block, Mapping) or any(str(key) != "digest" for key in block):
        raise FluxError(code, f"{where} is a mapping holding only 'digest'")
    if "digest" not in block:
        return None
    digest = block["digest"]
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise FluxError(code, f"{where}.digest is 'sha256:' and 64 lowercase hex digits")
    return digest


def _read_options(block: Any, where: str, code: ErrorCode) -> Dict[str, Any]:
    """The plain data an entry hands its plugin; a credential-shaped key anywhere is refused."""
    if block is None:
        return {}
    if not isinstance(block, Mapping):
        raise FluxError(code, f"{where} is a mapping of plain data")
    copied = _plain(block, where, code, depth=1, path=())
    assert isinstance(copied, dict)
    return copied


def _plain(value: Any, where: str, code: ErrorCode, *, depth: int, path: Tuple[str, ...]) -> Any:
    """A copy of *value* when it is plain data, else a refusal that never repeats a value."""
    here = f"{where}{''.join(path)}"
    if depth > _MOST_DEPTH:
        raise FluxError(code, f"{where} nests more than {_MOST_DEPTH} levels")
    if isinstance(value, Mapping):
        found: Dict[str, Any] = {}
        keys: List[str] = []
        for key in value:
            if not isinstance(key, str):
                raise FluxError(code, f"{here} has a key that is not a string")
            keys.append(key)
        for key in sorted(keys):
            if credential_option(key):
                raise FluxError(
                    code,
                    f"{here} has the key {_shown(key)!r}, which names a credential: a bundle "
                    "never holds one, so a plugin reads it from its own environment",
                )
        for key in sorted(keys):
            found[key] = _plain(
                value[key], where, code, depth=depth + 1, path=path + (f"[{_shown(key)!r}]",)
            )
        return found
    if isinstance(value, (list, tuple)):
        return [
            _plain(item, where, code, depth=depth + 1, path=path + (f"[{index}]",))
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        if credential_value(value):
            raise FluxError(
                code,
                f"{here} holds a value that is a credential (a URL with user information or a "
                "signature in its query, a password pair, a bearer token, a JWT, an access key, or a private "
                "key): a bundle never holds "
                "one, so a plugin reads it from its own environment",
            )
        return value
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise FluxError(code, f"{here} is a finite decimal")
        return value
    raise FluxError(
        code,
        f"{here} is plain data: a string, an exact decimal, a boolean, a list or a mapping",
    )


def _read_entry(expose_id: str, entry: Any, *, where: str, code: ErrorCode) -> GeneratorDeclaration:
    if not isinstance(entry, Mapping):
        raise FluxError(code, f"{where} is a mapping declaring {list(_REQUIRED)}")
    unknown = sorted(_shown(str(key)) for key in entry if str(key) not in _ENTRY_KEYS)
    if unknown:
        raise FluxError(
            code, f"{where} declares {unknown}; a generator carries {list(_ENTRY_KEYS)}"
        )
    missing = [key for key in _REQUIRED if entry.get(key) is None]
    if missing:
        raise FluxError(code, f"{where} does not declare {missing}")
    plugin = entry["plugin"]
    if not isinstance(plugin, str) or not _PLUGIN.fullmatch(plugin):
        raise FluxError(
            code,
            f"{where}.plugin is an entry-point name, a letter then letters, digits, "
            "'_', '.' and '-', never a module path, a file or an object reference",
        )
    distribution = _text(entry["distribution"], f"{where}.distribution", code)
    version = _text(entry["version"], f"{where}.version", code)
    grade_value = entry["grade"]
    grade = next((item for item in GRADES if grade_value == item.value), None)
    if grade is None:
        raise FluxError(
            code,
            f"{where}.grade is P4 or P5, not {_shown(str(grade_value))!r}: P0, P1 and P3 say "
            "what a profile rests on, so a plugin cannot state them",
        )
    reproducibility = entry["reproducibility"]
    if reproducibility not in REPRODUCIBILITY:
        raise FluxError(code, f"{where}.reproducibility is one of {list(REPRODUCIBILITY)}")
    privacy: Optional[PrivacyAccount] = None
    if grade is Pattern.P4:
        if entry.get("privacy") is None:
            raise FluxError(
                code,
                f"{where} is graded P4 and declares no privacy: state the unit, the loss "
                "(epsilon and delta, or rho) and the accounting",
            )
        privacy = _read_privacy(entry["privacy"], f"{where}.privacy", code)
    elif "privacy" in entry and entry["privacy"] is not None:
        raise FluxError(
            code, f"{where} is graded P5, a model with no privacy account, and declares privacy"
        )
    digest = _read_model_digest(entry.get("model"), f"{where}.model", code)
    if grade is Pattern.P4 and digest is None:
        raise FluxError(code, f"{where} is graded P4 and declares no model.digest")
    options = _read_options(entry.get("options"), f"{where}.options", code)
    return GeneratorDeclaration(
        expose_id=expose_id,
        plugin=plugin,
        distribution=distribution,
        version=version,
        grade=grade,
        reproducibility=reproducibility,
        privacy=privacy,
        model_digest=digest,
        options=options,
    )


def read_generators_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode
) -> Tuple[GeneratorDeclaration, ...]:
    """Read a ``generators`` block, or refuse it with *code*, naming where.

    *emits* is every ``exposeId`` the Simulation emits. The block is a mapping from an emitted
    ``exposeId`` to its declaration. Refused: a block that is not a mapping, more than 4 096
    entries, an ``exposeId`` the Simulation does not emit, an entry that is not a mapping or
    carries a key other than those of ADR 0032 section 2, a missing required key, a ``plugin``
    that is not an entry-point name, a ``grade`` other than ``P4`` and ``P5``, a ``P4`` without
    its account or its ``model.digest``, a ``P5`` with a ``privacy``, a decimal that is not exact
    or is out of range, a ``digest`` that is not ``sha256:`` and 64 lowercase hex digits, a
    ``reproducibility`` other than ``bytes``, ``environment`` and ``none``, and an option that is
    not plain data or whose mapping key names a credential. Returned in ``exposeId`` order.
    """
    if not isinstance(block, Mapping):
        raise FluxError(code, f"{where} is a mapping from an emitted exposeId to its generator")
    if len(block) > _MOST_ENTRIES:
        raise FluxError(code, f"{where} holds more than {_MOST_ENTRIES} entries")
    found: Dict[str, GeneratorDeclaration] = {}
    for key in sorted(block, key=lambda item: nfc(str(item))):
        expose_id = nfc(str(key))
        at = f"{where}[{_shown(expose_id)!r}]"
        if expose_id not in emits:
            raise FluxError(
                code,
                f"{at} names an expose this Simulation does not emit (it emits {sorted(emits)})",
            )
        if expose_id in found:
            raise FluxError(code, f"{at} declares the generator twice")
        found[expose_id] = _read_entry(expose_id, block[key], where=at, code=code)
    return tuple(found[key] for key in sorted(found))
