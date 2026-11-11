# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The written pages of the documentation: shape, links and safety for VuePress.

What is held here needs no engine. Every page says what it is (front matter that
parses as YAML) and opens with one title. Every link, image and heading anchor leads
somewhere. Every section index lists every page of its section. No page holds text
that a Vue template compiler would read as code. The commands and files on the pages
are run by ``test_run_pages.py``.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from urllib.parse import unquote

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

#: The sections of the documentation, in Diataxis terms.
SECTIONS = ("tutorial", "how-to", "reference", "explanation")

pytestmark = pytest.mark.unit

_FRONT = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.S)
_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[(?P<label>[^\]]+)\]:[ \t]*(?P<target>\S+)", re.M)
_REFERENCE_USE = re.compile(r"\[(?P<text>[^\]]+)\]\[(?P<label>[^\]]*)\]")
_EXTERNAL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HEADING = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(?P<ticks>`{3,}|~{3,})")


def _pages() -> List[Path]:
    pages = [DOCS / "README.md"]
    for section in SECTIONS:
        pages += sorted((DOCS / section).rglob("*.md"))
    return pages


def _prose_pages() -> List[Path]:
    """Every page except the per-command and per-code ones, whose text is the code's own."""
    generated = (DOCS / "reference" / "cli", DOCS / "reference" / "errors")
    return [
        page for page in _pages() if not any(directory in page.parents for directory in generated)
    ]


def _name(page: Path) -> str:
    try:
        return page.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(page)


def _front_matter(page: Path) -> Dict[str, Any]:
    match = _FRONT.match(page.read_text(encoding="utf-8"))
    assert match, f"{_name(page)} has no front matter"
    try:
        fields = yaml.safe_load(match.group("body"))
    except yaml.YAMLError as error:
        raise AssertionError(
            f"{_name(page)}: the front matter is not valid YAML: {error}"
        ) from None
    assert isinstance(fields, dict), f"{_name(page)}: the front matter is not a mapping"
    return fields


def _without_fences(text: str) -> str:
    """*text* with every fenced block (and the fences) taken out, line by line."""
    kept: List[str] = []
    closing = ""
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if closing:
            if (
                fence
                and fence.group("ticks")[0] == closing[0]
                and len(fence.group("ticks")) >= len(closing)
            ):
                closing = ""
            continue
        if fence:
            closing = fence.group("ticks")
            continue
        kept.append(line)
    return "\n".join(kept)


def _prose(text: str) -> str:
    """*text* without fenced blocks, HTML comments and inline code spans."""
    text = _without_fences(text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def _headings(page: Path) -> List[Tuple[int, str]]:
    found = []
    for line in _without_fences(page.read_text(encoding="utf-8")).splitlines():
        match = _HEADING.match(line)
        if match:
            found.append((len(match.group("marks")), match.group("text")))
    return found


# --- anchors ------------------------------------------------------------------------

_SPECIAL = re.compile(r"[\s~`!@#$%^&*()\-_+=\[\]{}|\\;:\"'“”‘’<>,.?/]+")


def _slug(heading: str) -> str:
    """The id VuePress 2 gives a heading (the slugify of ``@mdit-vue/shared``).

    Each run of spaces and punctuation becomes one hyphen, the ends are trimmed, a
    leading digit gets an underscore, and the rest is lower case. Accents are dropped.
    """
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"(?<!\w)_{1,3}([^_]+)_{1,3}(?!\w)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[̀-ͯ]", "", text)
    text = re.sub(r"[\u0000-\u001f]", "", text)
    text = _SPECIAL.sub("-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return re.sub(r"^(\d)", r"_\1", text).lower()


def _anchors(page: Path) -> Set[str]:
    """The ids of a page's headings; a repeated heading gets ``-1``, ``-2``... like the site."""
    seen: Dict[str, int] = {}
    ids: Set[str] = set()
    for _level, text in _headings(page):
        slug = _slug(text)
        if slug in seen:
            seen[slug] += 1
            slug = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 0
        ids.add(slug)
    return ids


# --- links --------------------------------------------------------------------------


def _targets(page: Path) -> List[str]:
    """Every link, image and reference definition target on a page, as written."""
    text = _prose(page.read_text(encoding="utf-8"))
    targets = [match.group("target") for match in _INLINE_LINK.finditer(text)]
    targets += [match.group("target") for match in _REFERENCE_DEFINITION.finditer(text)]
    return targets


def _links(page: Path) -> List[Tuple[str, str, str]]:
    """``(as written, path, fragment)`` for every target that stays on this site."""
    found = []
    for target in _targets(page):
        if _EXTERNAL.match(target):
            continue
        path_part, _, fragment = target.partition("#")
        found.append((target, unquote(path_part), unquote(fragment)))
    return found


def _undefined_references(page: Path) -> List[str]:
    text = _prose(page.read_text(encoding="utf-8"))
    defined = {m.group("label").lower() for m in _REFERENCE_DEFINITION.finditer(text)}
    missing = []
    for match in _REFERENCE_USE.finditer(text):
        label = (match.group("label") or match.group("text")).lower()
        if label not in defined:
            missing.append(match.group(0))
    return missing


# --- the pages ----------------------------------------------------------------------


@pytest.mark.parametrize("page", _pages(), ids=_name)
def test_a_page_says_what_it_is(page: Path) -> None:
    fields = _front_matter(page)
    title = fields.get("title")
    assert isinstance(title, str) and title.strip(), f"{_name(page)} has no title"
    description = fields.get("description")
    assert isinstance(description, str), f"{_name(page)} has no description"
    # A page for one command or code is described by its catalog title, which can be short.
    minimum = 20 if page in _prose_pages() else 1
    assert len(description.strip()) >= minimum, f"{_name(page)} has no useful description"


@pytest.mark.parametrize("page", _pages(), ids=_name)
def test_a_page_opens_with_exactly_one_title(page: Path) -> None:
    levels = [level for level, _text in _headings(page)]
    assert levels, f"{_name(page)} has no heading"
    assert levels[0] == 1, f"{_name(page)} does not open with a level-one heading"
    assert levels.count(1) == 1, f"{_name(page)} has {levels.count(1)} level-one headings"


def test_page_titles_are_unique() -> None:
    titles: Dict[str, List[str]] = {}
    for page in _pages():
        titles.setdefault(str(_front_matter(page)["title"]), []).append(_name(page))
    clashes = {title: where for title, where in titles.items() if len(where) > 1}
    # The generated reference pages are titled by their code or command, never alike.
    assert clashes == {}, clashes


@pytest.mark.parametrize("page", _pages(), ids=_name)
def test_every_link_image_and_anchor_leads_somewhere(page: Path) -> None:
    broken = []
    for target, path_part, fragment in _links(page):
        resolved = (page.parent / path_part).resolve() if path_part else page
        if not resolved.is_file():
            broken.append(target)
        elif fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
            broken.append(f"{target} (no such heading)")
    if page in _prose_pages():
        # In the generated pages a pair of brackets can be a quoted pattern, not a link.
        broken += [f"{reference} (no such reference)" for reference in _undefined_references(page)]
    assert broken == [], f"{_name(page)} links to nothing: {broken}"


@pytest.mark.parametrize("section", SECTIONS)
def test_a_section_index_lists_every_page_of_the_section(section: str) -> None:
    index = DOCS / section / "README.md"
    listed = {
        (index.parent / path_part).resolve() for _target, path_part, _ in _links(index) if path_part
    }
    # The generated reference is listed through its own index pages.
    expected = {
        page.resolve() for page in (DOCS / section).glob("*.md") if page.name != "README.md"
    }
    for sub in (DOCS / section).iterdir():
        if sub.is_dir():
            expected.add((sub / "README.md").resolve())
    missing = sorted(_name(p) for p in expected - listed)
    assert missing == [], f"{_name(index)} does not link: {missing}"


@pytest.mark.parametrize("page", _prose_pages(), ids=_name)
def test_a_page_holds_nothing_a_vue_template_would_read(page: Path) -> None:
    """VuePress compiles each page as a Vue template: a bare ``<tag>`` or ``{{ }}`` breaks the build."""
    body = _FRONT.sub("", page.read_text(encoding="utf-8"), count=1)
    prose = _prose(body)
    assert "{{" not in prose and "}}" not in prose, _name(page)
    stray = re.findall(r"<(?!/?(?:details|summary|br|kbd|sup|sub)\b)[A-Za-z/][^>]*>", prose)
    assert stray == [], f"{_name(page)} has markup VuePress would try to compile: {stray}"


def test_every_link_in_the_readme_leads_somewhere() -> None:
    """The README is read on GitHub, not compiled by VuePress, so a link may name a folder."""
    readme = REPO_ROOT / "README.md"
    broken = []
    for target, path_part, fragment in _links(readme):
        resolved = (readme.parent / path_part).resolve() if path_part else readme
        if not resolved.exists():
            broken.append(target)
        elif fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
            broken.append(f"{target} (no such heading)")
    assert broken == [], f"README.md links to nothing: {broken}"


def test_the_landing_page_links_to_all_four_sections() -> None:
    linked = {(DOCS / path_part).resolve() for _target, path_part, _ in _links(DOCS / "README.md")}
    for section in SECTIONS:
        assert (DOCS / section / "README.md").resolve() in linked, section


# --- the checks themselves ----------------------------------------------------------


@pytest.mark.parametrize(
    "heading,expected",
    [
        # Taken from the ids a VuePress 2 build of these pages produced.
        ("Ship the plain starter", "ship-the-plain-starter"),
        (
            "The evidence pack, and what it does not claim",
            "the-evidence-pack-and-what-it-does-not-claim",
        ),
        (
            "`flux_engine.api` — the governed extension contract",
            "flux-engine-api-—-the-governed-extension-contract",
        ),
        ("The agentic tier", "the-agentic-tier"),
        ("Step 4: Test it", "step-4-test-it"),
        ("2. Change the population", "_2-change-the-population"),
        ("What's [linked](./x.md) here?", "what-s-linked-here"),
    ],
)
def test_a_heading_becomes_the_id_the_site_gives_it(heading: str, expected: str) -> None:
    assert _slug(heading) == expected


def test_fenced_code_is_not_prose() -> None:
    text = "# T\n\n```console\n$ echo '# not a heading'  [x](./nope.md)\n```\n\n````md\n```\n````\n"
    assert _without_fences(text).strip() == "# T"


def _page(tmp_path: Path, text: str) -> Path:
    page = tmp_path / "page.md"
    page.write_text(text, encoding="utf-8")
    return page


def test_front_matter_that_is_not_yaml_is_caught(tmp_path: Path) -> None:
    page = _page(tmp_path, "---\ntitle: Model: your customers\ndescription: x\n---\n\n# T\n")
    with pytest.raises(AssertionError, match="not valid YAML"):
        _front_matter(page)


def test_every_kind_of_target_is_found(tmp_path: Path) -> None:
    page = _page(
        tmp_path,
        "# T\n\n[a](./a.md#x) ![b](./b.png) [c][ref] [d]: not a definition\n\n[ref]: ./c.md\n"
        "\n`[hidden](./nope.md)`\n\n```md\n[fenced](./nope.md)\n```\n\n<!-- [gone](./nope.md) -->\n",
    )
    assert sorted(_targets(page)) == ["./a.md#x", "./b.png", "./c.md"]


def test_a_reference_with_no_definition_is_found(tmp_path: Path) -> None:
    page = _page(tmp_path, "# T\n\n[text][missing] and [other][defined]\n\n[defined]: ./x.md\n")
    assert _undefined_references(page) == ["[text][missing]"]


def test_a_repeated_heading_gets_a_numbered_id(tmp_path: Path) -> None:
    page = _page(tmp_path, "# T\n\n## Steps\n\n## Steps\n\n```\n## not a heading\n```\n")
    assert _anchors(page) == {"t", "steps", "steps-1"}


def test_a_second_title_is_seen(tmp_path: Path) -> None:
    page = _page(tmp_path, "# One\n\n```sh\n# a comment, not a title\n```\n\n# Two\n")
    assert [level for level, _ in _headings(page)] == [1, 1]
