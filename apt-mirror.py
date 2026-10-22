#!/usr/bin/env python3
"""apt-mirror for Python: mirror apt repositories in pure stdlib Python 3.

A clean reimplementation of the classic Perl apt-mirror.  It mirrors apt
repositories (release, index and archive files) into a local mirror tree,
manages the skel and mirror directories, and generates a cleanup script.
It has no third-party runtime dependencies and no external tool
requirements: fetching and decompression happen in-process.

Usage:
    apt-mirror.py [configfile]

Without an argument the tool uses /etc/apt/mirror.list.  The config file
is line-oriented:

    set <key> [<value>]        set a config variable (value may be quoted)
    deb <uri> <dist> <comps>   mirror a binary repository
    deb-src <uri> <dist> ...   mirror a source repository
    clean <uri>                directory key the cleanup covers
    skip-clean <uri>           directory key excluded from the cleanup

``#`` comment lines and blank lines are ignored.
"""

import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Maximum number of $name expansion passes before a value is declared
#: non-converging (a substitution loop).
MAX_EXPANSION_PASSES = 16

_VAR_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

_MACHINE_TO_ARCH: dict[str, str] = {
    "x86_64": "amd64",
    "i686": "i386",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "armhf",
    "ppc64le": "powerpc64el",
    "s390x": "s390x",
}

#: Every config variable of spec §5.1 except `defaultarch`, which is
#: resolved separately by detect_defaultarch() (a config `set` line may
#: still override it).
DEFAULT_CONFIG: dict[str, str] = {
    "nthreads": "20",
    "base_path": "/var/spool/apt-mirror",
    "mirror_path": "$base_path/mirror",
    "skel_path": "$base_path/skel",
    "var_path": "$base_path/var",
    "cleanscript": "$var_path/clean.sh",
    "postmirror_script": "$var_path/postmirror.sh",
    "run_postmirror": "1",
    "limit_rate": "100m",
    "use_proxy": "off",
    "http_proxy": "",
    "https_proxy": "",
    "proxy_user": "",
    "proxy_password": "",
    "certificate": "",
    "private_key": "",
    "ca_certificate": "",
    "no_check_certificate": "0",
    "auth_no_challenge": "0",
    "unlink": "0",
    "_autoclean": "0",
    "_tilde": "0",
    "_plus": "0",
    "_contents": "1",
}


class AptMirrorError(Exception):
    """Fatal apt-mirror error; ``str(e)`` is the full user-facing message."""


@dataclass(frozen=True)
class Repo:
    """One ``deb`` or ``deb-src`` mirror line.

    ``type`` is "deb" or "deb-src".  ``components=None`` marks a flat
    repository, whose indexes and archives live directly under
    ``<uri>/<distribution>/`` instead of ``<uri>/dists/<distribution>/``.
    """

    type: str
    arch: str
    uri: str
    distribution: str
    components: list[str] | None


@dataclass
class ParsedConfig:
    """Result of parsing a mirror.list config file.

    ``variables`` holds the defaults (raw) plus the expanded values of
    every ``set`` line; ``binaries`` and ``sources`` are the repository
    entries; ``clean`` and ``skipclean`` contain sanitised URIs that
    bound the cleanup scope.
    """

    variables: dict[str, str]
    binaries: list[Repo]
    sources: list[Repo]
    clean: set[str]
    skipclean: set[str]


def detect_defaultarch() -> str:
    """Return the default Debian architecture for bare ``deb`` lines.

    Detection chain: ``dpkg --print-architecture`` first, then a mapping
    of ``platform.machine()``, and finally the hardcoded "amd64" fallback.
    """
    try:
        result = subprocess.run(
            ("dpkg", "--print-architecture"),
            check=True,
            capture_output=True,
            text=True,
        )
        arch = result.stdout.strip()
        if arch:
            return arch
    except (OSError, subprocess.CalledProcessError):
        pass
    return _MACHINE_TO_ARCH.get(platform.machine().lower(), "amd64")


def _expand_once(value: str, variables: dict[str, str], where: str) -> str:
    """Expand ``$name`` references in one pass.

    Every reference must name a key already in ``variables``; an
    undefined reference raises AptMirrorError naming the key and the
    context given by ``where``.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise AptMirrorError(
                f"apt-mirror: {where}: reference to undefined variable '{name}'"
            )
        return variables[name]

    return _VAR_REF_RE.sub(replace, value)


def _expand_template(template: str, variables: dict[str, str], where: str) -> str:
    """Expand ``$name`` references in ``template`` until the value is stable.

    Raises AptMirrorError if a reference is undefined or if no stable
    value is reached within MAX_EXPANSION_PASSES passes (a substitution
    loop).
    """
    value = template
    for _ in range(MAX_EXPANSION_PASSES):
        expanded = _expand_once(value, variables, where)
        if expanded == value:
            return expanded
        value = expanded
    raise AptMirrorError(
        f"apt-mirror: {where}: variable expansion does not converge "
        f"(no stable value after {MAX_EXPANSION_PASSES} passes)"
    )


def get_var(variables: dict[str, str], key: str) -> str:
    """Return ``variables[key]`` with ``$name`` references expanded.

    Expansion is recursive and stops when a pass no longer changes the
    value.  Raises AptMirrorError if ``key`` is not in ``variables`` (the
    message names the key) or if expansion does not converge within
    MAX_EXPANSION_PASSES passes.
    """
    if key not in variables:
        raise AptMirrorError(f"apt-mirror: unknown variable '{key}'")
    return _expand_template(variables[key], variables, f"while expanding '{key}'")


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
    if uri.endswith("/"):
        uri = uri[:-1]
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


def round_number(x: float) -> str:
    """Round ``x`` to one decimal (ties round up) and format as a string.

    Mirrors the original Perl ``round_number``: the magnitude is rounded to
    the nearest tenth and the result is always rendered with at least one
    decimal digit (whole numbers gain ``.0``).  The sign is preserved.
    """
    minus = "-" if x < 0 else ""
    tenths = int((abs(x) + 0.05) * 10)
    whole, frac = divmod(tenths, 10)
    if frac:
        return f"{minus}{whole}.{frac}"
    return f"{minus}{whole}.0"


def format_bytes(n: int) -> str:
    """Format a byte count as a human-readable size string.

    Below 1024 the value renders as ``<n> bytes``; otherwise the largest
    binary unit (KiB / MiB / GiB, all 1024-based) divides it and the quotient
    is rendered with ``round_number``.  Ports the original Perl
    ``format_bytes``.
    """
    if n < 1024:
        return f"{n} bytes"
    if n >= 1024 * 1024 * 1024:
        value, unit = n / (1024 * 1024 * 1024), "GiB"
    elif n >= 1024 * 1024:
        value, unit = n / (1024 * 1024), "MiB"
    else:
        value, unit = n / 1024, "KiB"
    return f"{round_number(value)} {unit}"


_RATE_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*$")
_RATE_MULTIPLIERS: dict[str, float] = {
    "k": 1_000.0,
    "K": 1_000.0,
    "m": 1_000_000.0,
    "M": 1_000_000.0,
    "g": 1_000_000_000.0,
    "G": 1_000_000_000.0,
}


def parse_rate(value: str) -> float:
    """Parse a ``limit_rate`` value into bytes per second.

    Accepts a number (int or decimal) with an optional suffix: ``k``/``K``
    (×1e3), ``m``/``M`` (×1e6), ``g``/``G`` (×1e9), or a bare number.  Raises
    AptMirrorError (naming the offending literal) for an empty, negative, or
    non-numeric value.
    """
    match = _RATE_RE.match(value)
    if match is None:
        raise AptMirrorError(f"apt-mirror: invalid limit_rate '{value}'")
    number = float(match.group(1))
    if number < 0:
        raise AptMirrorError(f"apt-mirror: invalid limit_rate '{value}'")
    suffix = match.group(2)
    if not suffix:
        return number
    return number * _RATE_MULTIPLIERS[suffix]


def _arch_from_options(options: str) -> str | None:
    """Return the ``arch=`` value of a bracketed deb-line option string.

    Option keys are comma-separated, but the ``arch`` value is itself a
    comma-separated architecture list, so tokens that do not look like a
    ``key=`` start are treated as continuations of the previous value.
    Returns None when no ``arch=`` option is present.
    """
    parts: list[str] = []
    in_arch = False
    for token in options.split(","):
        token = token.strip()
        key, sep, value = token.partition("=")
        if sep and re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", key):
            in_arch = key == "arch"
            if in_arch and value.strip():
                parts.append(value.strip())
        elif in_arch:
            parts.append(token)
    return ",".join(parts) if parts else None


_SET_RE = re.compile(r"^set\s+(\S+)\s*(\S.*)?$")
_DEB_RE = re.compile(
    r"^(?P<type>deb-src|deb-\S+|deb)\s+"
    r"(?:\[(?P<options>[^\]]*)\]\s+)?"
    r"(?P<uri>\S+)\s+"
    r"(?P<distribution>\S+)"
    r"(?:\s+(?P<components>\S+(?:\s+\S+)*))?\s*$"
)
_CLEAN_RE = re.compile(r"^(?P<kind>skip-clean|clean)\s+(?P<uri>\S+)$")


def parse_config_line(line: str) -> dict | None:
    """Parse one config line into a small dict.

    Returns None for blank lines and ``#`` comment lines.  Any other
    recognised shape yields one of:

    - ``{"type": "set", "key": k, "value": v}`` with ``value=None`` when
      the ``set`` line carries no value at all;
    - ``{"type": "deb" | "deb-src", "arch": a, "uri": u,
      "distribution": d, "components": c}`` where ``arch=None`` means the
      default architecture (no ``deb-<arch>`` suffix and no ``arch=``
      option) and ``c=None`` marks a flat repository;
    - ``{"type": "clean" | "skip-clean", "uri": u}``.

    Only the ``arch=a,b`` bracket option is honoured; other option keys
    are accepted and ignored, and an ``arch=`` option overrides a
    ``deb-<arch>`` suffix.  Raises ValueError for a non-blank line that
    matches no known shape (parse_config converts that into a
    line-numbered AptMirrorError).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    match = _SET_RE.match(stripped)
    if match:
        key, value = match.group(1), match.group(2)
        if value is not None:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
        return {"type": "set", "key": key, "value": value}

    match = _DEB_RE.match(stripped)
    if match:
        token = match.group("type")
        if token == "deb-src":
            repo_type, suffix = "deb-src", None
        elif token == "deb":
            repo_type, suffix = "deb", None
        else:
            repo_type, suffix = "deb", token[len("deb-") :]
        options = match.group("options")
        arch: str | None = None
        if options:
            arch = _arch_from_options(options)
        if arch is None:
            arch = suffix
        components = match.group("components")
        return {
            "type": repo_type,
            "arch": arch,
            "uri": match.group("uri"),
            "distribution": match.group("distribution"),
            "components": components.split() if components is not None else None,
        }

    match = _CLEAN_RE.match(stripped)
    if match:
        return {"type": match.group("kind"), "uri": match.group("uri")}

    raise ValueError(f"unrecognised config line: {stripped!r}")


def parse_config(path: str | Path) -> ParsedConfig:
    """Parse a mirror.list config file into a ParsedConfig.

    Reads the file stripping carriage returns, seeds the variable table
    with DEFAULT_CONFIG plus the detected defaultarch, then processes the
    file line by line: ``set`` lines are expanded with the variable table
    as it stands at that point, ``deb``/``deb-src`` lines become Repo
    entries (a bare line takes the default architecture), and
    clean/skip-clean URIs are sanitised with the ``_tilde``/``_plus``
    values known so far.

    Raises AptMirrorError naming the 1-based line number for an
    unparseable line, a ``set`` without a value, an undefined variable
    reference, and any other config validation failure.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise AptMirrorError(
            f"apt-mirror: invalid config file specified ({config_path})"
        )
    variables = dict(DEFAULT_CONFIG)
    variables["defaultarch"] = detect_defaultarch()
    binaries: list[Repo] = []
    sources: list[Repo] = []
    clean: set[str] = set()
    skipclean: set[str] = set()
    text = config_path.read_text(encoding="utf-8")
    for line_number, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.rstrip("\r").strip()
        try:
            parsed = parse_config_line(line)
        except ValueError as error:
            raise AptMirrorError(
                f"apt-mirror: invalid line in config file ({line_number}: {line})"
            ) from error
        if parsed is None:
            continue
        where = f"config file line {line_number}: {line}"
        line_type = parsed["type"]
        if line_type == "set":
            key = parsed["key"]
            value = parsed["value"]
            if value is None:
                raise AptMirrorError(
                    f"apt-mirror: set without value in config file "
                    f"({line_number}: {line})"
                )
            variables[key] = _expand_template(value, variables, where)
        elif line_type in ("deb", "deb-src"):
            arch = parsed["arch"] or variables["defaultarch"]
            repo = Repo(
                type=line_type,
                arch=arch,
                uri=parsed["uri"],
                distribution=parsed["distribution"],
                components=parsed["components"],
            )
            if line_type == "deb":
                binaries.append(repo)
            else:
                sources.append(repo)
        else:  # clean / skip-clean
            target = clean if line_type == "clean" else skipclean
            target.add(
                sanitise_uri(
                    parsed["uri"],
                    tilde=variables["_tilde"] not in ("", "0"),
                    plus=variables["_plus"] not in ("", "0"),
                )
            )
    return ParsedConfig(
        variables=variables,
        binaries=binaries,
        sources=sources,
        clean=clean,
        skipclean=skipclean,
    )
