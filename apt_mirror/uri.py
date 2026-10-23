"""Path/URL normalisation for the mirror tree (spec §5.4)."""

import re

# Path/URL normalisation for the mirror tree (spec §5.4).  Each ``_RDS_*``
# pattern is applied, in order, to completion before the next one runs.
_RDS_DOT = re.compile(r"/\./")  # ``/./`` -> ``/`` (redundant ``.`` component)
_RDS_SLASH = re.compile(r"(?<!:)//")  # ``//`` -> ``/`` (never across ``://``)
_RDS_DOTDOT = re.compile(r"(?<!:/)/[^/]+/\.\./")  # ``/seg/../`` -> ``/``
_RDS_PATTERNS = (_RDS_DOT, _RDS_SLASH, _RDS_DOTDOT)


def sanitise_uri(url: str, tilde: bool = False, plus: bool = False) -> str:
    """Turn a URL into a mirror-tree directory key.

    Strips the ``scheme://`` prefix and, when the host segment carries an
    ``@``, the leading ``userinfo@`` part; applies the ``_tilde`` / ``_plus``
    percent-encodings (``~`` -> ``%7E``, ``+`` -> ``%2B``) when requested; and
    removes a single trailing ``/``.  Ports the original Perl ``sanitise_uri``
    with identical observable behaviour (spec §5.4).
    """
    uri = re.sub(r"^[A-Za-z0-9_]+://", "", url)
    if "@" in uri.split("/", 1)[0]:
        uri = re.sub(r"^[^@]*@", "", uri, count=1)
    if tilde:
        uri = uri.replace("~", "%7E")
    if plus:
        uri = uri.replace("+", "%2B")
    uri = uri.removesuffix("/")
    return uri


def remove_double_slashes(path: str, tilde: bool = False, plus: bool = False) -> str:
    """Normalise a path inside the mirror tree.

    Repeatedly collapses ``/./`` to ``/``, ``//`` to ``/`` (never across
    ``://``), and ``/<segment>/../`` to ``/`` (never across ``:``), applying
    the three rules in that fixed order and each to a fixpoint; then applies
    the ``_tilde`` / ``_plus`` percent-encodings.  Ports the original Perl
    ``remove_double_slashes`` with identical observable behaviour (spec §5.4).
    """
    result = path
    for pattern in _RDS_PATTERNS:
        result, changed = pattern.subn("/", result)
        while changed:
            result, changed = pattern.subn("/", result)
    if tilde:
        result = result.replace("~", "%7E")
    if plus:
        result = result.replace("+", "%2B")
    return result
