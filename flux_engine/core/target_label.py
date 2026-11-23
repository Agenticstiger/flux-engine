# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What may be printed or stored about a sink target: a label of its non-secret parts.

A target is a free string a person typed: a path, a URL, a libpq or ODBC connection string, or
something else. Recognising every shape a secret can take in one is never complete, so nothing that
is printed or stored is the string itself. :func:`target_label` builds the one label that is shown
for it, from an allowlist of what a label may hold:

* a URL (``scheme://...``, ``jdbc:mysql://...``, ``//host/...``, ``file:path``) keeps its scheme,
  host, port and path. User information (everything before the last ``@``), a query and a fragment
  are written ``…``, and so is a host that is not a plain name or address, a port that is not all
  digits and a path that holds a character a secret could hide behind (``% = @ ;``). When the text
  before the last ``@`` holds a ``/``, nothing but ``scheme://…`` is shown;
* a ``key=value`` connection string (libpq or ODBC) keeps ``host``, ``hostaddr``, ``port``,
  ``dbname``, ``database``, ``server``, ``driver`` and ``sslmode`` pairs whose value is made of
  letters, digits and ``. _ - : / [ ]``, and counts the rest: ``host=h (2 settings withheld)``;
* a plain path (none of ``@ = ; ? #``, no scheme; white space is fine) is kept as typed, but only
  for a sink whose target is a directory or a name (:data:`PLAIN_SINKS`). Any other sink's string,
  an ADBC ``uri`` or a ``db.*`` value among them, may be anything, so a bare word is never shown;
* anything else is the sink's name and a short digest: ``adbc target sha256:1a2b3c4d5e6f``.

:func:`replace_target` is the other half: in a message a library wrote, it replaces the target and
each part the label withheld by what the label shows, so a driver that echoes its input prints the
label. Standard library only.
"""

from __future__ import annotations

import json
import re
from typing import Dict, FrozenSet, Iterable, List, NamedTuple, Optional, Tuple, Union
from urllib.parse import quote, quote_plus, unquote

from flux_engine.core.canonical import sha256_hex

__all__ = ["PLAIN_SINKS", "WITHHELD", "replace_target", "spellings", "target_label"]

#: What stands in for a part of a target the label does not show.
WITHHELD = "…"

#: The sinks whose target is a directory or a name, never a connection string: a plain path is a
#: label for these and for no other sink.
PLAIN_SINKS: FrozenSet[str] = frozenset({"parquet", "arrow-ipc", "delta", "iceberg"})
#: The ADBC drivers: what ``--sink adbc:DRIVER`` names, and what a label may start with before a colon.
_DRIVERS: FrozenSet[str] = frozenset({"sqlite", "duckdb", "postgresql", "snowflake", "bigquery"})
#: The pairs of a connection string a label shows.
_SHOWN_KEYS: FrozenSet[str] = frozenset(
    {"host", "hostaddr", "port", "dbname", "database", "server", "driver", "sslmode"}
)
#: What a shown pair's value may be made of.
_SHOWN_VALUE = re.compile(r"[A-Za-z0-9._:/\[\]-]+")
#: A host and its port: a name, an address or a bracketed IPv6 address, then ``:`` and digits.
_HOSTPORT = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]*)(?::[0-9]*)?")
#: What a URL's path may be made of: no ``%``, ``=``, ``@``, ``;`` or white space.
_PATH = re.compile(r"[\w.~:/\\\[\]+,$-]*")

_SCHEME = r"[A-Za-z][A-Za-z0-9+.-]*"
#: ``scheme://``, ``jdbc:mysql://`` or the scheme-relative ``//``, then the rest.
_HIERARCHICAL = re.compile(rf"(?P<head>(?:(?i:jdbc:)?{_SCHEME}:)?//)(?P<rest>.*)", re.DOTALL)
#: ``scheme:`` then the rest.
_OPAQUE = re.compile(rf"(?P<head>{_SCHEME}:)(?P<rest>.*)", re.DOTALL)
_KEY = re.compile(r"[A-Za-z_][\w.-]*")
_SPACE = " \t\n\r\f\v"
#: A plain path: none of ``@ = ; ? #``; white space is allowed.
_PLAIN = re.compile(r"[^@=;?#]*")
#: ``scheme:`` at the start: a string that has one is not a plain path. A drive (``C:\``, ``C:/``) is no scheme.
_SCHEMED = re.compile(r"(?![A-Za-z]:(?:[\\/]|$))[A-Za-z][A-Za-z0-9+.-]*:")
#: A label this module wrote for a string it would not show.
_DIGEST_LABEL = re.compile(r"[A-Za-z][\w.-]* target sha256:[0-9a-f]{12}")
#: The count a connection string's label ends with.
_COUNT = re.compile(r"\((?P<count>[0-9]+) settings? withheld\)")
#: ``$NAME``: what a label shows for a value read from an environment variable.
_ENV_REF = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
#: What follows ``iceberg:`` in a label: a catalogue name and namespace levels.
_ICEBERG = re.compile(r"[A-Za-z0-9._/-]+")
#: A value after ``=`` in a query, a ``;`` or ``&`` separated list or a libpq string, quoted or not.
_VALUE = re.compile(r"=\s*(?:'(?P<quoted>[^']*)'|\"(?P<double>[^\"]*)\"|(?P<value>[^\s&;,'\"#]+))")
#: The password of ``user:password@host``.
_PASSWORD = re.compile(r"[^\s:/@]+:(?P<password>[^\s/@]+)@")
#: The shortest value in a secret position that a digest label withholds.
_MIN_SECRET = 4
#: The shortest withheld part that is replaced in a message: a shorter one would mangle it.
_MIN_PART = 3
#: Where the stand-in characters of :func:`_replace_all` start (the Unicode private use area).
_MARK_BASE = 0xE000


class _Label(NamedTuple):
    label: str
    #: The parts of the string the label does not show, each as written.
    withheld: Tuple[str, ...]


def target_label(sink: str, raw: str) -> str:
    """The label of *raw*, the target of the sink called *sink*: the one text printed or stored for it.

    ``postgresql://u:pw@h:5432/db?x=1`` is ``postgresql://…@h:5432/db?…``;
    ``Server=h;Database=d;Uid=u;Pwd=x`` is ``Server=h;Database=d (2 settings withheld)``;
    ``s3://bkt/pfx`` is itself, and so is ``out/run`` for a sink in :data:`PLAIN_SINKS`; a string
    that is none of those is ``adbc target sha256:`` and twelve hex digits. A label is its own label.
    """
    return _build(sink, raw).label


def spellings(text: str) -> List[str]:
    """The ways a library may write *text* in a message: as it is, percent-encoded and as a quoted string."""
    forms = [
        text,
        quote(text),
        quote(text, safe=""),
        quote_plus(text),
        repr(text)[1:-1],
        json.dumps(text)[1:-1],
    ]
    return list(dict.fromkeys(forms))


def replace_target(text: str, sink: str, raws: Iterable[Union[str, Tuple[str, str]]]) -> str:
    """*text* with every place it holds one of *raws* (a target, an option's value) written as its label.

    An item of *raws* is the string, or a pair of the string and what to write for it (``$SIM_URI``
    for what a variable holds). The string itself is replaced by its label, in the form
    ``scheme://`` takes when a library collapses it (``scheme:/``) and in each of its
    :func:`spellings` (percent-encoded, quoted), and each part the label withheld (the user
    information, the value of a dropped pair, the query, the fragment) that is three characters or
    more is replaced by ``…`` in its spellings, so a message that holds only the part is covered
    too. A string of fewer than three characters, white space apart, is left alone: it would mangle
    every message it is in. Nothing is searched for: only the strings the caller knows are replaced.
    """
    return _replace_all(text, _replacements(sink, raws))


def _replacements(sink: str, raws: Iterable[Union[str, Tuple[str, str]]]) -> Dict[str, str]:
    replacements: Dict[str, str] = {}
    for item in raws:
        raw, shown = (item, None) if isinstance(item, str) else item
        for candidate in _candidates(raw):
            _add(replacements, candidate, _build(sink, candidate), shown)
    return replacements


def _candidates(raw: str) -> List[str]:
    """*raw*, and the forms of it a library may write: without its white space, and each of its lines.

    A value that ends in a line break is echoed without it, and a message is read a line at a time.
    """
    forms = [raw, raw.strip(), *(line.strip() for line in raw.splitlines())]
    return [form for form in dict.fromkeys(forms) if len(form.strip()) >= _MIN_PART]


def _add(replacements: Dict[str, str], raw: str, built: _Label, shown: Optional[str]) -> None:
    """Add what to write for *raw* and for each part *built* withheld, each in its spellings."""
    for form in (*spellings(raw), raw.replace("://", ":/")):
        replacements.setdefault(form, shown or built.label)
    for part in built.withheld:
        for form in (*spellings(part), *spellings(unquote(part))):
            if len(form) >= _MIN_PART:
                replacements.setdefault(form, WITHHELD)


def _replace_all(text: str, replacements: Dict[str, str]) -> str:
    """*text* with each key of *replacements* written as its value: longest first, in one pass.

    Plain ``str.replace``, no pattern: a string of any size costs a pass per key. Each key found is
    first swapped for a stand-in character that is in no key, no value and no text, so a value is
    never searched again.
    """
    keys = [key for key in sorted(replacements, key=len, reverse=True) if key and key in text]
    if not keys:
        return text
    used = set(text).union(*(set(key) | set(replacements[key]) for key in keys))
    marks: Dict[str, str] = {}
    code = _MARK_BASE
    for key in keys:
        while chr(code) in used:
            code += 1
        marks[key] = chr(code)
        code += 1
    for key in keys:
        text = text.replace(key, marks[key])
    for key in keys:
        text = text.replace(marks[key], replacements[key])
    return text


def _build(sink: str, raw: str) -> _Label:
    if not raw or _DIGEST_LABEL.fullmatch(raw) or _COUNT.fullmatch(raw):
        return _Label(raw, ())
    if sink == "adbc" and raw in _DRIVERS:
        return _Label(raw, ())
    built = _url(raw) or _pairs(raw) or _driver_label(raw, sink)
    if built is not None:
        return built
    if sink in PLAIN_SINKS and _PLAIN.fullmatch(raw) and _SCHEMED.match(raw) is None:
        return _Label(raw, ())
    digest = sha256_hex(raw.encode("utf-8", "surrogatepass"))[:12]
    return _Label(f"{sink} target sha256:{digest}", _secret_positions(raw))


def _secret_positions(raw: str) -> Tuple[str, ...]:
    """The values in *raw* that sit where a secret does: after an ``=`` and a password in user information.

    For a string shown as a digest only, so a library that writes it again in another form still has
    these replaced in a message. Keys, schemes and ordinary words are not among them.
    """
    values = [
        match.group("quoted") or match.group("double") or match.group("value") or ""
        for match in _VALUE.finditer(raw)
    ]
    values += [match.group("password") for match in _PASSWORD.finditer(raw)]
    return tuple(dict.fromkeys(value for value in values if len(value) >= _MIN_SECRET))


def _pieces(*texts: str) -> List[str]:
    """Each of *texts* and its ``/`` segments: what a message may hold of a part that was withheld."""
    pieces: List[str] = []
    for text in texts:
        pieces.append(text)
        pieces.extend(text.split("/") if "/" in text else ())
    return pieces


# -- URLs ---------------------------------------------------------------------


def _url(raw: str) -> Optional[_Label]:
    if any(char in _SPACE for char in raw):
        return None
    match = _HIERARCHICAL.fullmatch(raw)
    if match is not None:
        return _hierarchical(match.group("head"), match.group("rest"))
    match = _OPAQUE.fullmatch(raw)
    if match is not None and match.group("head").casefold() == "file:":
        return _file(match.group("head"), match.group("rest"))
    return None


def _hierarchical(head: str, rest: str) -> _Label:
    shown = head
    withheld: List[str] = []
    # User information is everything before the last ``@``, so a password holding ``#``, ``?``
    # or ``@`` is withheld whole. When it holds a ``/`` too, nothing after the scheme is shown:
    # it is a path that holds an ``@``, or a secret in one.
    at = rest.rfind("@")
    if at >= 0:
        userinfo, rest = rest[:at], rest[at + 1 :]
        if "/" in userinfo:
            return _Label(head + WITHHELD, tuple(p for p in _pieces(userinfo, rest) if p))
        withheld.extend((userinfo, userinfo.partition(":")[2]))
        shown += WITHHELD + "@"
    location, tail = _cut_tail(rest)
    hostport, slash, path = location.partition("/")
    for part, parts in (_host(hostport), _path(slash + path), _tail(tail)):
        shown += part
        withheld.extend(parts)
    return _Label(shown, tuple(part for part in withheld if part))


def _file(head: str, rest: str) -> _Label:
    location, tail = _cut_tail(rest)
    path, path_withheld = _path(location)
    suffix, suffix_withheld = _tail(tail)
    parts = (*path_withheld, *suffix_withheld)
    return _Label(head + path + suffix, tuple(part for part in parts if part))


def _cut_tail(rest: str) -> Tuple[str, str]:
    """*rest* split before its first ``?`` or ``#``: the location, then the query and fragment."""
    cut = min(
        (index for index in (rest.find("?"), rest.find("#")) if index >= 0), default=len(rest)
    )
    return rest[:cut], rest[cut:]


def _host(hostport: str) -> Tuple[str, List[str]]:
    """The host and port as shown, and what was withheld: only a name or an address, and digits."""
    if all(_HOSTPORT.fullmatch(item) for item in hostport.split(",")):
        return hostport, []
    return WITHHELD, [hostport, *hostport.replace(",", ":").split(":")]


def _path(path: str) -> Tuple[str, List[str]]:
    if _PATH.fullmatch(path):
        return path, []
    return ("/" if path.startswith("/") else "") + WITHHELD, _pieces(path)


def _tail(tail: str) -> Tuple[str, List[str]]:
    """What a label shows of a query and a fragment, and what it withheld."""
    if not tail:
        return "", []
    shown = ""
    withheld: List[str] = []
    fragment, hash_ = tail[1:], "#"
    if tail[0] == "?":
        query, hash_, fragment = tail[1:].partition("#")
        shown += "?" + WITHHELD
        withheld.append(query)
        withheld.extend(pair.partition("=")[2] for pair in re.split(r"[&;]", query))
    if hash_:
        shown += "#" + WITHHELD
        withheld.append(fragment)
    return shown, withheld


def _driver_label(raw: str, sink: str) -> Optional[_Label]:
    """A label this module composes in front of a part it labelled: ``duckdb:file:x.db?…``, ``sqlite:$VAR``."""
    scheme, colon, rest = raw.partition(":")
    if not colon or not rest or not (scheme in _DRIVERS or (scheme == "iceberg" == sink)):
        return None
    if _ENV_REF.fullmatch(rest) or (scheme == "iceberg" and _ICEBERG.fullmatch(rest)):
        return _Label(raw, ())
    inner = _url(rest) or _pairs(rest)
    if (inner is not None and inner.label == rest) or _DIGEST_LABEL.fullmatch(rest):
        return _Label(raw, ())
    return None


# -- key=value connection strings -----------------------------------------------


class _Pair(NamedTuple):
    key: str
    value: str
    #: The value as written, quotes and escapes included.
    written: str
    #: The text between the end of the pair before it and its key.
    gap: str


def _pairs(raw: str) -> Optional[_Label]:
    body, claimed = raw, 0
    count = re.search(r" " + _COUNT.pattern + r"\Z", raw)
    if count is not None:
        body, claimed = raw[: count.start()], int(count.group("count"))
    pairs = _tokenize(body)
    if not pairs:
        return None
    shown, withheld = _split_pairs(pairs, ";" in body)
    total = claimed + len(pairs) - len(shown)
    label = (";" if ";" in body else " ").join(shown)
    if total:
        label += f"{' ' if label else ''}({total} setting{'' if total == 1 else 's'} withheld)"
    return _Label(label, tuple(part for part in withheld if part))


def _split_pairs(pairs: List[_Pair], odbc: bool) -> Tuple[List[str], List[str]]:
    """The pairs a label shows (as ``key=value``) and the text of those it withholds."""
    shown: List[str] = []
    withheld: List[str] = []
    absorbed = False
    for pair in pairs:
        # Read as libpq, ``Pwd=x;Server=h`` is one value: a pair after a withheld one, with no
        # white space in it or before the pair, is withheld with it.
        if any(char in _SPACE for char in pair.gap):
            absorbed = False
        hidden = (
            absorbed
            or pair.key.casefold() not in _SHOWN_KEYS
            or _SHOWN_VALUE.fullmatch(pair.value) is None
        )
        if hidden:
            withheld.extend((pair.value, pair.written))
            absorbed = odbc and not any(char in _SPACE for char in pair.written)
        else:
            shown.append(f"{pair.key}={pair.value}")
    return shown, withheld


def _skip(text: str, index: int, chars: str) -> int:
    while index < len(text) and text[index] in chars:
        index += 1
    return index


def _tokenize(text: str) -> Optional[List[_Pair]]:
    """The ``key=value`` pairs of a libpq or ODBC string, or ``None`` when it does not parse cleanly.

    A string with a ``;`` is read as ODBC (``;`` separates, a value may be ``{...}`` with ``}}`` for
    a brace), any other as libpq (white space separates, a value may be ``'...'`` with a backslash
    escape). Anything the two readings could split differently is refused rather than guessed.
    """
    odbc = ";" in text
    separators = _SPACE + (";" if odbc else "")
    pairs: List[_Pair] = []
    index = 0
    while True:
        start = index
        index = _skip(text, index, separators)
        if index >= len(text):
            return pairs
        read = _pair_at(text, index, odbc, text[start:index])
        if read is None:
            return None
        pair, index = read
        pairs.append(pair)


def _pair_at(text: str, index: int, odbc: bool, gap: str) -> Optional[Tuple[_Pair, int]]:
    key = _KEY.match(text, index)
    if key is None:
        return None
    index = _skip(text, key.end(), _SPACE)
    if text[index : index + 1] != "=":
        return None
    start = _skip(text, index + 1, _SPACE)
    parsed = _value(text, start, odbc)
    if parsed is None:
        return None
    value, end = parsed
    return _Pair(key.group(0), value, text[start:end], gap), end


def _value(text: str, index: int, odbc: bool) -> Optional[Tuple[str, int]]:
    """The value at *index* and where it ends, or ``None`` when it is not read cleanly."""
    first = text[index : index + 1]
    if first == "'":
        return _after_quoted(text, _quoted(text, index + 1), odbc)
    if first == "{":
        return _after_quoted(text, _braced(text, index + 1), odbc)
    return _bare_odbc(text, index) if odbc else _bare_libpq(text, index)


def _quoted(text: str, index: int) -> Optional[Tuple[str, int]]:
    """A libpq ``'...'`` value, from just after its opening quote, backslash escapes read."""
    chars: List[str] = []
    while index < len(text) and text[index] != "'":
        if text[index] == "\\":
            index += 1
            if index >= len(text):
                return None
        chars.append(text[index])
        index += 1
    return ("".join(chars), index + 1) if index < len(text) else None


def _braced(text: str, index: int) -> Optional[Tuple[str, int]]:
    """An ODBC ``{...}`` value, from just after its opening brace, ``}}`` read as one brace."""
    chars: List[str] = []
    while index < len(text):
        if text[index] == "}":
            if text[index + 1 : index + 2] != "}":
                return "".join(chars), index + 1
            chars.append("}")
            index += 2
            continue
        chars.append(text[index])
        index += 1
    return None


def _bare_odbc(text: str, index: int) -> Optional[Tuple[str, int]]:
    end = text.find(";", index)
    end = len(text) if end < 0 else end
    plain = text[index:end].rstrip(_SPACE)
    if any(char in _SPACE or char == "\\" for char in plain):
        return None
    return plain, end


def _bare_libpq(text: str, index: int) -> Optional[Tuple[str, int]]:
    chars: List[str] = []
    while index < len(text) and text[index] not in _SPACE:
        if text[index] == "\\":
            index += 1
            if index >= len(text):
                return None
        chars.append(text[index])
        index += 1
    return "".join(chars), index


def _after_quoted(
    text: str, parsed: Optional[Tuple[str, int]], odbc: bool
) -> Optional[Tuple[str, int]]:
    """*parsed* when what follows a quoted value ends the pair: ``'a'b`` is read differently by different drivers."""
    if parsed is None:
        return None
    index = parsed[1]
    after = _skip(text, index, _SPACE)
    if after < len(text) and not (text[after] == ";" if odbc else after > index):
        return None
    return parsed
