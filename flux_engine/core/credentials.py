# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a name or a value is a credential (``password``, ``access-key``, ``sas``).

One answer for every place that refuses a credential: the sinks' options and environment variables
(:mod:`flux_engine.sinks._secrets`) and the options of a generator plugin a bundle declares
(:mod:`flux_engine.seam.generators`). Kept in ``core`` because the second is read by the seam, which
may not import the sinks. Standard library only.
"""

from __future__ import annotations

import re
from typing import FrozenSet, List, Optional
from urllib.parse import unquote_plus

__all__ = [
    "credential_key",
    "credential_dsn",
    "credential_option",
    "credential_query",
    "credential_url",
    "credential_value",
    "MASK",
    "dsn_secret_values",
    "names_environment_variable",
]

#: A word that names a credential on its own.
_WORDS: FrozenSet[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "credential",
        "credentials",
        "sas",
        "signature",
        "sig",
        "apikey",
        "passphrase",
        "authorization",
        "bearer",
        "authkey",
        "secretaccesskey",
        "sslkey",
        "passfile",
    }
)

#: The word before ``key`` that makes it a credential (``access-key``, ``api.key``).
_KEY_PREFIXES: FrozenSet[str] = frozenset(
    {
        "access",
        "account",
        "api",
        "client",
        "private",
        "secret",
        "storage",
        "master",
        "sse",
        "encryption",
        "ssl",
    }
)

#: Words that end a longer one-word spelling of a credential (``authtoken``, ``dbpassword``).
_COMPOUND_ENDINGS = ("password", "passwd", "secret", "token", "apikey")
#: ``accesskey``, ``privatekey``: a prefix and ``key`` written as one word.
_COMPOUND_KEYS: FrozenSet[str] = frozenset(prefix + "key" for prefix in _KEY_PREFIXES)

#: Spellings of a credential that a separator may split (``pass-phrase``, ``auth-key``), compared
#: as two or three adjacent words written together.
_SPLIT_SPELLINGS: FrozenSet[str] = frozenset(
    {
        "passphrase",
        "authkey",
        "secretaccesskey",
        "sslkey",
        "passfile",
        "authorization",
        "bearer",
        "password",
        "passwd",
        "apikey",
    }
)

_SEPARATORS = re.compile(r"[-_.]+")
#: ``accessKey`` is read as ``access`` ``Key``.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def credential_key(name: str) -> bool:
    """Whether the option or variable *name* names a credential.

    The name is split on ``-``, ``_`` and ``.`` (and at a lower-to-upper case
    change), and compared case-folded. It is a credential when a word is
    ``password``, ``passwd``, ``pwd``, ``secret``, ``token``, ``credential``,
    ``credentials``, ``sas``, ``signature``, ``sig`` or ``apikey``; when the
    word ``key`` follows ``access``, ``account``, ``api``, ``client``,
    ``private``, ``secret``, ``storage``, ``master``, ``sse`` or
    ``encryption`` or ``ssl``; or when the whole name is ``key``. A word that
    writes one of those without the separator (``accesskey``, ``authtoken``)
    counts as well, as do ``passphrase``, ``passfile``, ``authkey``,
    ``secretaccesskey``, ``authorization`` and ``bearer`` in any separated spelling
    (``pass-phrase``, ``auth_key``).
    """
    folded = name.casefold()
    # Read the name twice: split at case changes (``accessKey``) and not (``pAsSwOrD``).
    for text in (_CAMEL.sub("-", name).casefold(), folded):
        words = [word for word in _SEPARATORS.split(text) if word]
        if _words_name_credential(words):
            return True
    return False


def _words_name_credential(words: List[str]) -> bool:
    if words == ["key"]:
        return True
    for index, word in enumerate(words):
        if word in _WORDS or word in _COMPOUND_KEYS or word.endswith(_COMPOUND_ENDINGS):
            return True
        if word == "key" and index > 0 and words[index - 1] in _KEY_PREFIXES:
            return True
        for width in (2, 3):
            if "".join(words[index : index + width]) in _SPLIT_SPELLINGS:
                return True
    return False


def names_environment_variable(name: str) -> bool:
    """Whether the option *name* holds the *name* of an environment variable (it ends in ``-env``).

    ``uri-env`` and ``db.password-env`` carry a variable's name, never a secret, so neither is
    refused for naming a credential.
    """
    return name.casefold().endswith("-env")


def credential_option(name: str) -> bool:
    """Whether the option *name* is refused for naming a credential.

    The one rule for a sink's options and a generator plugin's options: a name that
    :func:`credential_key` flags, unless it ends in ``-env`` (:func:`names_environment_variable`).
    """
    return credential_key(name) and not names_environment_variable(name)


#: ``scheme://`` at the start of a string.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
#: What a ``KEY=VALUE`` pair is in a connection string, wherever it sits: a URL query (``?a=1&b=2``,
#: ``;`` separated too), a fragment (``#access_token=x``), a libpq string (``host=h password='a b'``),
#: an ODBC string (``Server=h;PWD={a;b}``). A key starts after anything that cannot be part of a
#: name; a value is quoted with ``'``, ``"`` or ``{}`` (an unterminated quote runs to the end, so no
#: input makes the match backtrack), or runs to white space, ``;``, ``&`` or a quote.
_PAIR = re.compile(
    r"(?<![\w.%\-])(?P<key>[A-Za-z_%][\w.%\-]*)[ \t\n\r\f\v]*=[ \t\n\r\f\v]*"
    r"(?P<value>'(?:[^'\\]|\\.)*'?|\"[^\"]*\"?|\{[^}]*\}?|[^\s;&#?'\"{]*)"
)
#: Names a DSN uses for a password that :func:`credential_key` does not read as one.
_DSN_KEYS: FrozenSet[str] = frozenset({"pass", "pw"})
#: A Windows drive path such as ``C:\data``, which is no ``user:password@`` form.
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
#: ``//user:PW@``: user information with a password, with or without a scheme before it.
_PASSWORD_AT = re.compile(r"//[^\s/@:?#]*:[^\s/?#]+@")
#: ``MASK`` replaces a secret value in anything printed.
MASK = "***"


def _last_word_key(name: str) -> bool:
    words = [w for w in _SEPARATORS.split(_CAMEL.sub("-", name).casefold()) if w]
    return len(words) > 1 and words[-1] == "key"


def credential_query(query: str, *, connection_string: bool = True) -> bool:
    """Whether a URL query string (``a=1&sig=...``, or ``;`` separated) has a parameter named like a credential.

    With *connection_string* (the default) a name whose last word is ``key`` counts too
    (``subscription-key``, ``licenseKey``).
    """
    return any(
        _secret_pair_key(unquote_plus(pair.partition("=")[0]), connection_string)
        for pair in re.split(r"[&;]", query)
        if pair
    )


def credential_url(target: str) -> bool:
    """Whether *target* is a URL (``scheme://...``) that carries user information, a query or a fragment.

    A target that is not a URL, such as ``out/`` or ``duckdb``, is never one.
    """
    match = _SCHEME.match(target)
    if match is None:
        return False
    rest = target[match.end() :]
    if "?" in rest or "#" in rest:
        return True
    authority = rest.partition("/")[0]
    return "@" in authority


def _secret_pair_key(key: str, connection_string: bool = True) -> bool:
    """Whether a ``KEY=VALUE`` pair's *key* names a credential.

    In a connection string (a query, fragment, libpq or ODBC string) a key whose last word is
    ``key`` counts as well; in free text (a plugin's value) it does not, so ``sort_key=name`` stays.
    """
    key = unquote_plus(key)
    return (
        credential_option(key)
        or key.casefold() in _DSN_KEYS
        or (connection_string and _last_word_key(key) and not names_environment_variable(key))
    )


def _unquoted(value: str) -> str:
    if value[:1] in ("'", '"', "{"):
        value = value[1:]
        if value[-1:] in ("'", '"', "}"):
            value = value[:-1]
    return value


def _userinfo(token: str) -> Optional[str]:
    """The user information of one white-space free *token* (``user:PW`` of ``user:PW@host/db``), or ``None``."""
    match = _SCHEME.match(token)
    rest = token[match.end() :] if match else token[2:] if token.startswith("//") else token
    if match is None and _DRIVE.match(rest):
        return None
    location = re.split(r"[?#]", rest, maxsplit=1)[0]
    authority = location.partition("/")[0]
    return authority.rpartition("@")[0] if "@" in authority else None


def _userinfo_password(text: str) -> Optional[str]:
    for token in text.split():
        info = _userinfo(token)
        if info is not None and ":" in info and info.partition(":")[2]:
            return info.partition(":")[2]
    return None


def dsn_secret_values(text: str) -> List[str]:
    """Every secret *text* holds as a connection string: the password of ``user:PW@host`` and the value of each credential-named pair.

    ``host=h password='a b'`` yields ``a b``; ``?mode=rwc;sig=x#access_token=y`` yields ``x`` and ``y``.
    """
    found: List[str] = []
    for match in _PAIR.finditer(text):
        if _secret_pair_key(match.group("key")):
            value = _unquoted(match.group("value"))
            if value:
                found.append(value)
    password = _userinfo_password(text)
    if password:
        found.append(password)
    return found


def credential_dsn(text: str) -> bool:
    """Whether *text*, a connection URI or DSN, carries a secret inline.

    True for a password in URL user information (``//user:PW@host``, ``user:PW@account/db``); a
    credential-named pair (:func:`credential_key`) with a value in a query split on ``?``, ``&`` or
    ``;``, in a fragment, in a libpq ``key=value`` string (the value may be quoted) or in an ODBC
    ``Key=Value;`` string. ``host=127.0.0.1 dbname=x`` and ``file:x.db?mode=ro`` carry none.
    """
    return bool(dsn_secret_values(text))


#: A token or key a person pastes. Each alternative follows a gitleaks rule (MIT, v8.30.1), adapted to
#: linear time: no group repeats inside a repeat.
_SECRET_SHAPES = re.compile(
    # gitleaks `aws-access-token`: A3T[A-Z0-9], AKIA, ASIA, ABIA, ACCA, plus the other IAM id
    # prefixes, in upper case as gitleaks has them; and the lower-case ``akia`` form alone, so
    # ordinary words that begin ``asia`` or ``acca`` are not read as keys.
    r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b"
    r"|\bakia[a-z0-9]{16}\b"
    # gitleaks `private-key`: a PEM or PGP private key header (``PRIVATE KEY BLOCK``), and a certificate.
    r"|-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY(?: BLOCK)?|CERTIFICATE)-----"
    r"|\bgh[pousr]_[A-Za-z0-9]{36,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{22,}\b"
    r"|\bxox[abposr]-[A-Za-z0-9-]{10,}"
    r"|\bAIza[0-9A-Za-z_-]{35}\b"
    # gitleaks `jwt`: three dot-separated parts, the first two starting ``ey`` (base64 of ``{"``).
    r"|\bey[A-Za-z0-9]{17,}\.ey[A-Za-z0-9/\\_-]{17,}\.[A-Za-z0-9/\\_-]{10,}"
    # gitleaks `jwt-base64`: a base64 JWT, which starts ``ZXlK`` (base64 of ``eyJ``).
    r"|\bZXlK[A-Za-z0-9/\\_+-]{40,}"
    # gitleaks `curl-auth-header`, its ``Authorization: Bearer`` branch; the token is an RFC 6750
    # section 2.1 ``b64token``: letters, digits and ``-._~+/`` then ``=`` padding.
    r"|\b(?:Bearer|BEARER)[ \t]+[A-Za-z0-9._~+/-]{8,}=*"
)
#: ``scheme://`` followed by the rest of a URL, up to whitespace or a quote.
_URL_AT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")


def credential_value(text: str) -> bool:
    """Whether the string *text* holds a credential.

    True for a URL with user information (``https://user:secret@host``, ``//user:secret@host``) or a
    credential-named query parameter (``?sig=...``, ``?X-Amz-Signature=...``); a credential-named
    ``KEY=VALUE`` pair of a connection string (``password=...``, ``#access_token=...``); an
    ``Authorization``-style ``Bearer <token>``; a JWT; and a pasted key: an AWS access key id, a PEM
    or PGP private key block, a GitHub, Slack or Google API key.
    """
    if _SECRET_SHAPES.search(text) or _PASSWORD_AT.search(text):
        return True
    if any(
        _secret_pair_key(m.group("key"), False) and _unquoted(m.group("value"))
        for m in _PAIR.finditer(text)
    ):
        return True
    for match in _URL_AT.finditer(text):
        rest = match.group(0).partition("://")[2]
        authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
        if "@" in authority:
            return True
        if credential_query(rest.partition("?")[2].partition("#")[0], connection_string=False):
            return True
    return False
