# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The register-discipline gate: one word list, and a scan that reaches every public document.

Two holes this pins shut.

**Five copies of the word list.** ``scripts/register_lint.py``,
``flux_engine.evidence.report`` and three test modules each held their own
literal tuple of the same ten reserved words, with nothing asserting they
were equal -- so adding a word to the CI gate left ``EVIDENCE.md``'s
runtime refusal (the shipped, customer-facing surface) blind to it.
:mod:`flux_engine._vocabulary` is now the single definition; these tests
assert every consumer reads it rather than a copy.

**A gate that could not see the file it was meant to gate.**
``_scan_docs`` special-cased ``README.md`` and otherwise scanned only
``docs/``, so ``GOLDEN-CHANGELOG.md`` carried a banned word past a green
run of the lint. The scan now covers every Markdown file at the
repository root; the negative controls below prove it fires there, still
fires under ``docs/``, and still exempts the internal
``docs/spec`` tree.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from flux_engine import _vocabulary
from flux_engine.evidence import report

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.unit


def _load_register_lint() -> Any:
    """Import ``scripts/register_lint.py`` by path (``scripts/`` is not a package)."""
    path = REPO_ROOT / "scripts" / "register_lint.py"
    spec = importlib.util.spec_from_file_location("register_lint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("register_lint", module)
    spec.loader.exec_module(module)
    return module


def test_every_gate_reads_the_same_word_list() -> None:
    register_lint = _load_register_lint()
    assert tuple(register_lint.BANNED_WORDS) == _vocabulary.BANNED_WORDS
    assert tuple(report._BANNED_WORDS) == _vocabulary.BANNED_WORDS
    # Not just equal values: the same compiled matcher, so a gate cannot
    # be built from a stale copy of the list.
    assert register_lint._PATTERN is _vocabulary.BANNED_PATTERN


def test_a_word_added_to_the_list_arms_both_gates() -> None:
    """The property the five copies could not offer: one edit, both surfaces."""
    for word in _vocabulary.BANNED_WORDS:
        assert report.find_banned_words(f"prose containing {word} here")
        assert _load_register_lint()._scan_text("x.md", f"prose containing {word} here")


@pytest.mark.parametrize(
    "module_name,attribute",
    [
        ("tests.core.test_errors", "BANNED_WORDS"),
        ("tests.concordance.test_errors", "BANNED_WORDS"),
        ("tests.api.test_api_surface", "_BANNED_WORDS"),
    ],
)
def test_the_independent_test_copies_still_match_the_canonical_list(
    module_name: str, attribute: str
) -> None:
    """Three test modules keep their own literal copy on purpose.

    Their stated reason is a good one: an independent copy is a control,
    so relaxing the lint script cannot quietly relax the test. What was
    missing is the other half -- nothing said the copies still matched, so
    a word added to the gate silently left three surfaces unchecked. This
    asserts the equality without taking the control away.
    """
    module = importlib.import_module(module_name)
    assert (
        tuple(getattr(module, attribute)) == _vocabulary.BANNED_WORDS
    ), f"{module_name}.{attribute} has drifted from flux_engine._vocabulary.BANNED_WORDS"


def _fake_repo(root: Path) -> None:
    (root / "README.md").write_text("A clean readme.\n")
    (root / "GOLDEN-CHANGELOG.md").write_text("a top-level `concordance`-style block\n")
    (root / "NOTES.txt").write_text("this proves nothing is scanned here\n")
    docs = root / "docs"
    docs.mkdir()
    (docs / "public.md").write_text("this proves the docs scan still works\n")
    (docs / "spec").mkdir()
    (docs / "spec" / "x.md").write_text("this proves internal docs are exempt\n")


def test_scan_docs_covers_repository_root_markdown(tmp_path: Path, monkeypatch) -> None:
    register_lint = _load_register_lint()
    _fake_repo(tmp_path)
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = register_lint._scan_docs()

    assert any(hit.startswith("GOLDEN-CHANGELOG.md:") for hit in hits), hits
    assert any(hit.startswith("docs/public.md:") for hit in hits), hits
    # The internal specification tree stays exempt, and a non-Markdown file
    # at the root is not swept in by the widening.
    assert not any("docs/spec" in hit for hit in hits), hits
    assert not any("NOTES.txt" in hit for hit in hits), hits


def test_scan_docs_is_clean_on_a_clean_tree(tmp_path: Path, monkeypatch) -> None:
    register_lint = _load_register_lint()
    (tmp_path / "README.md").write_text("A clean readme.\n")
    (tmp_path / "CHANGELOG.md").write_text("A clean changelog: the fidelity check passed.\n")
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    assert register_lint._scan_docs() == []


def test_this_repository_passes_the_documents_scan() -> None:
    """The gate over the real tree -- README, every root Markdown file, docs/."""
    hits = _load_register_lint()._scan_docs()
    assert hits == [], "banned vocabulary in a public document:\n" + "\n".join(hits)


def test_scan_docs_covers_html_under_docs(tmp_path: Path, monkeypatch) -> None:
    """HTML was the one format the docs scan did not read.

    ``docs/`` is the surface most likely to be published and HTML is the most
    publishable thing in it, so excluding it left the gate reading the formats
    least likely to reach a reader.

    Counted rather than estimated: of the three HTML files that reached main on
    2026-09-07, two carried a genuine violation and three sentences in total
    were reworded, all of them the same word. The third file's two occurrences
    are both real package references and needed no change, which is what the
    masking tests below are about.
    """
    register_lint = _load_register_lint()
    _fake_repo(tmp_path)
    (tmp_path / "docs" / "page.html").write_text(
        "<p>this proves the point</p>\n"
        '<p title="a certified result">an attribute reaches a reader too</p>\n'
    )
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = register_lint._scan_docs()

    assert any(hit.startswith("docs/page.html:1:") for hit in hits), hits
    # Raw HTML is scanned rather than stripped to its text first, so a word in
    # a `title` attribute is a finding: it reaches a reader as prose does. The
    # line numbers above are the other half of that choice -- scanning raw
    # keeps every offset, so a hit names the line a person can go and edit.
    assert any(hit.startswith("docs/page.html:2:") for hit in hits), hits


def test_a_module_path_in_prose_is_not_a_finding(tmp_path: Path, monkeypatch) -> None:
    """A package named as a path is the package's name, not a use of the word.

    Written bare here, with no markup around it, because that is how Markdown
    and plain text write one and because it is what isolates this rule: wrap
    these in ``<code>`` and the markup rule below would carry the test on its
    own, leaving this one passing for the wrong reason.
    """
    register_lint = _load_register_lint()
    _fake_repo(tmp_path)
    (tmp_path / "docs" / "paths.html").write_text(
        "<p>See flux_engine.concordance.record for the record, built from\n"
        "   flux_engine/concordance/boundary/merkle.py, with its tests under\n"
        "   tests/concordance.</p>\n"
    )
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = [hit for hit in register_lint._scan_docs() if "paths.html" in hit]
    assert hits == [], hits


def test_a_package_name_alone_in_a_markup_element_is_not_a_finding(
    tmp_path: Path, monkeypatch
) -> None:
    """How a package table writes a package: the cell holds the name and
    nothing else.

    This repository's own architecture table lists every package this way, so
    rewriting one row to satisfy the gate would make that row inconsistent
    with a dozen siblings. The element is exempt because its whole content is
    one identifier-shaped token, which is the bound the next test checks.
    """
    register_lint = _load_register_lint()
    _fake_repo(tmp_path)
    (tmp_path / "docs" / "table.html").write_text(
        '<tr><td class="mono">concordance</td><td>Fidelity attestation.</td></tr>\n'
        "<p>Tests live in <code>tests/concordance</code>.</p>\n"
    )
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = [hit for hit in register_lint._scan_docs() if "table.html" in hit]
    assert hits == [], hits


def test_prose_inside_a_code_element_is_still_a_finding(tmp_path: Path, monkeypatch) -> None:
    """The control on the exemption above.

    A markup element is exempt only when its whole content is a single
    identifier-shaped token. Without that bound, ``<code>`` would be the
    easiest place in the repository to carry a reserved word past the gate.
    """
    register_lint = _load_register_lint()
    _fake_repo(tmp_path)
    (tmp_path / "docs" / "hidden.html").write_text("<p><code>this proves the point</code></p>\n")
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = [hit for hit in register_lint._scan_docs() if "hidden.html" in hit]
    assert any("'proves'" in hit for hit in hits), hits


def test_masking_preserves_offsets() -> None:
    """Every mask substitutes ``#`` of equal length, so a hit's line and column
    still address the original text. A mask that shortened the text would move
    every finding after it onto the wrong line.
    """
    for text in (
        "see flux_engine.concordance.record here",
        "see flux_engine/concordance/boundary/merkle.py here",
        "see tests/concordance here",
        '<td class="mono">concordance</td>',
    ):
        assert len(_vocabulary.mask_module_references(text)) == len(text), text


def test_a_bare_reserved_word_in_a_markup_element_is_still_a_finding(
    tmp_path: Path, monkeypatch
) -> None:
    """The hole an unscoped element rule opens, kept closed.

    An earlier version of the mask exempted any single identifier-shaped token
    inside ``<code>``, ``<td>``, ``<th>`` or ``<span>``. A reserved word alone
    is exactly that shape, so the rule delivered the word whole to a reader and
    called it a module reference. The exemption is now scoped to the packages
    named in ``_RESERVED_WORD_PACKAGES``.
    """
    register_lint = _load_register_lint()
    _fake_repo(tmp_path)
    (tmp_path / "docs" / "smuggle.html").write_text(
        "<p>Result: <span>certified</span>.</p>\n<p>It <code>proves</code> the point.</p>\n"
    )
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = [hit for hit in register_lint._scan_docs() if "smuggle.html" in hit]
    assert any("'certified'" in hit for hit in hits), hits
    assert any("'proves'" in hit for hit in hits), hits


def test_a_path_shaped_string_naming_nothing_real_is_still_a_finding(
    tmp_path: Path, monkeypatch
) -> None:
    """The second hole: a reference is exempt because it names a package that
    exists, not because it is punctuated like one.

    An earlier version matched any ``flux_engine.*`` or ``tests/*`` shape, so a
    reserved word tacked onto a fabricated path was masked away.
    """
    register_lint = _load_register_lint()
    _fake_repo(tmp_path)
    (tmp_path / "docs" / "fabricated.html").write_text(
        "<p>See tests/certified_output.log for the raw record.</p>\n"
        "<p>Detail lives at flux_engine.report.certified_summary for now.</p>\n"
    )
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = [hit for hit in register_lint._scan_docs() if "fabricated.html" in hit]
    assert len(hits) == 2, hits
    assert all("'certified'" in hit for hit in hits), hits


def test_the_package_cell_is_exempt_whatever_the_tag_case() -> None:
    """HTML tag names are case-insensitive, so the mask's are too. The package
    name itself is matched case-sensitively: a capitalised word opening a cell
    is prose, not an identifier.
    """
    assert (
        _vocabulary.find_banned_words('<TD CLASS="mono">concordance</TD>', mask_module_paths=True)
        == ()
    )
    assert _vocabulary.find_banned_words(
        '<td class="mono">Concordance</td>', mask_module_paths=True
    ) == ("concordance",)
