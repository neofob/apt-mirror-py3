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

import argparse
import base64
import bz2
import contextlib
import email.utils
import fcntl
import filecmp
import gzip
import http.client
import logging
import lzma
import os
import platform
import posixpath
import queue
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("apt-mirror")

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


# ---------------------------------------------------------------------------
# HTTP(S) fetch layer (spec §5.3)
# ---------------------------------------------------------------------------

#: Response bodies are read (and paced) in chunks this large.
_CHUNK_SIZE = 256 * 1024

#: Total per-URL attempts (1 initial + 4 retries) on transient failures.
_MAX_ATTEMPTS = 5

#: Socket timeout, in seconds, applied to every connection.
_SOCKET_TIMEOUT = 30.0

_USERINFO_RE = re.compile(r"(?<=://)[^@/]*@")


def _strip_userinfo(url: str) -> str:
    """Remove a ``user[:password]@`` prefix from a URL, if present."""
    return _USERINFO_RE.sub("", url, count=1)


def _config_on(value: str | None) -> bool:
    """Return True for a config value that switches an option on.

    "1", "on", "yes", "true" (case-insensitive, surrounding whitespace
    ignored) are on; everything else — including "", "0", "off" and None —
    is off.
    """
    if value is None:
        return False
    return value.strip().lower() in ("1", "on", "yes", "true")


class FetchError(Exception):
    """A single URL could not be fetched; the run may continue without it.

    ``url`` and ``reason`` are both credential-free and safe to print,
    log, or persist; ``str(e)`` is a single-line user-facing message with
    the ``apt-mirror:`` prefix.
    """

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"apt-mirror: {url}: {reason}")
        self.url = url
        self.reason = reason


class FetchNotFound(FetchError):
    """The server answered 404 for the URL (raised without retry)."""


class _TruncatedBodyError(http.client.HTTPException):
    """A Content-Length-bound response ended before the declared length.

    Sizing reads (``read(n)``) return EOF silently, so a connection that
    dies mid-body only shows up as a short byte count; this turns that
    into a retryable transport error.
    """


def _basic_token(username: str, password: str) -> str:
    """The ``Authorization: Basic`` header value for ``username:password``."""
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


@dataclass(frozen=True)
class _Target:
    """Connection parts of one http(s) URL, plus its Basic credential.

    ``credential`` is the ready-to-send ``Authorization`` value for
    userinfo embedded in the URL, or None when the URL carries no
    userinfo; ``origin`` is the RFC 7235 origin (scheme, host, port);
    ``clean_url`` is the same URL without userinfo, safe for every error
    message and log line.
    """

    scheme: str
    host: str
    port: int
    netloc: str
    target: str
    clean_url: str
    origin: tuple[str, str, int]
    credential: str | None


def _parse_target(url: str) -> _Target:
    """Split an http(s) URL into the parts a fetch needs.

    Raises AptMirrorError (before any connection could be opened) for a
    non-http(s) scheme and FetchError for an unparseable URL, an
    out-of-range port, or a URL without a host.  Used for the initial URL
    and for every redirect Location alike.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        # The port property can also raise ValueError (non-numeric or
        # out-of-range port), so it stays inside the same guard.
        port = parsed.port
    except ValueError as error:
        raise FetchError(_strip_userinfo(url), f"invalid URL: {error}") from error
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise AptMirrorError(
            f"apt-mirror: unsupported scheme '{parsed.scheme}' in URL "
            f"{_strip_userinfo(url)}: only http and https are supported"
        )
    host = parsed.hostname
    if not host:
        raise FetchError(_strip_userinfo(url), "URL has no host")
    default_port = 443 if scheme == "https" else 80
    port = port or default_port
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    netloc = host if port == default_port else f"{host}:{port}"
    clean_url = f"{scheme}://{netloc}{target}"
    credential = None
    if parsed.username is not None:
        credential = _basic_token(parsed.username, parsed.password or "")
    return _Target(
        scheme, host, port, netloc, target, clean_url, (scheme, host, port), credential
    )


class RateLimiter:
    """Pace a byte stream so its running average stays at or below a ceiling.

    Call :meth:`pace` once per chunk just written; the limiter sleeps until
    every byte paced since the first call has had its share of
    ``rate_bps``.  Natural delays (server latency, scheduling) credit
    against the budget, so a rate at or above the wire speed costs
    effectively zero overhead; a non-positive rate disables pacing.
    """

    def __init__(self, rate_bps: float) -> None:
        self._rate_bps = float(rate_bps)
        self._deadline = 0.0  # monotonic time the next byte may be sent by

    def pace(self, n_bytes: int) -> None:
        """Sleep until ``n_bytes`` more bytes fit within the running rate."""
        if n_bytes <= 0 or self._rate_bps <= 0:
            return
        self._deadline = (
            max(self._deadline, time.monotonic()) + n_bytes / self._rate_bps
        )
        wait = self._deadline - time.monotonic()
        if wait > 0:
            time.sleep(wait)


class Fetcher:
    """Download files over HTTP(S) with atomic writes, resume, and retries.

    Built from the spec §5.1 config ``variables``: ``limit_rate`` (an
    invalid value raises AptMirrorError via parse_rate) drives a
    per-Fetcher RateLimiter; ``no_check_certificate``, ``ca_certificate``,
    ``certificate`` and ``private_key`` shape the TLS context; ``use_proxy``
    with ``http_proxy`` / ``https_proxy`` / ``proxy_user`` /
    ``proxy_password`` routes transfers through a proxy; and
    ``auth_no_challenge`` controls origin Basic authentication.
    Fetches follow 301/302/303/307/308 Location redirects up to
    ``max_redirects`` hops (each a fresh GET); a redirect to a different
    origin drops the Authorization header (RFC 7235), and a Location-less
    redirect or an exhausted cap is a FetchError.
    """

    def __init__(
        self,
        variables: dict[str, str],
        backoff_base: float = 1.0,
        max_redirects: int = 10,
    ) -> None:
        """Prepare the rate limiter, TLS context, and proxy table.

        ``backoff_base`` scales the retry backoff schedule (base, 2x, 4x,
        8x seconds between the up-to-5 attempts); ``max_redirects`` caps
        the Location redirects followed by each fetch (default 10); both
        exist so tests can keep them fast.
        """
        self._backoff_base = backoff_base
        self._max_redirects = max_redirects
        self._limiter = RateLimiter(
            parse_rate(variables.get("limit_rate", DEFAULT_CONFIG["limit_rate"]))
        )
        self._auth_no_challenge = _config_on(variables.get("auth_no_challenge") or "0")
        self._tls_context = self._build_tls_context(variables)
        self._proxies = self._build_proxy_table(variables)

    @staticmethod
    def _build_tls_context(variables: dict[str, str]) -> ssl.SSLContext:
        """Build the TLS context for https URLs (spec §5.1).

        ``no_check_certificate`` disables verification entirely (documented
        as insecure); ``ca_certificate`` replaces the default trust store;
        ``certificate`` plus ``private_key`` add a client certificate.  A
        missing or unreadable file raises AptMirrorError.
        """
        if _config_on(variables.get("no_check_certificate") or "0"):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
            ca_certificate = (variables.get("ca_certificate") or "").strip()
            if ca_certificate:
                try:
                    context.load_verify_locations(ca_certificate)
                except (OSError, ssl.SSLError) as error:
                    raise AptMirrorError(
                        f"apt-mirror: invalid ca_certificate "
                        f"'{ca_certificate}': {error}"
                    ) from error
        certificate = (variables.get("certificate") or "").strip()
        private_key = (variables.get("private_key") or "").strip()
        if certificate and private_key:
            try:
                context.load_cert_chain(certificate, private_key)
            except (OSError, ssl.SSLError) as error:
                raise AptMirrorError(
                    f"apt-mirror: invalid certificate/private_key pair: {error}"
                ) from error
        return context

    @staticmethod
    def _build_proxy_table(
        variables: dict[str, str],
    ) -> dict[str, tuple[str, int, bytes | None]]:
        """Build the per-scheme proxy table, only when use_proxy is on.

        Maps a target scheme to (proxy host, proxy port, an optional
        Proxy-Authorization header).  The proxy URL is always dialed in
        the clear (https targets are CONNECT-tunnelled through it).
        Userinfo embedded in the proxy URL wins over the separate
        proxy_user / proxy_password variables.  An invalid proxy URL is an
        AptMirrorError that does not echo the URL (it may carry secrets).
        """
        proxies: dict[str, tuple[str, int, bytes | None]] = {}
        if not _config_on(variables.get("use_proxy") or "off"):
            return proxies
        for key, target_scheme in (("http_proxy", "http"), ("https_proxy", "https")):
            raw = (variables.get(key) or "").strip()
            if not raw:
                continue
            try:
                proxy = urllib.parse.urlsplit(raw)
                if proxy.scheme not in ("http", "https") or not proxy.hostname:
                    raise ValueError("missing host")
                # The port property also raises ValueError (non-numeric or
                # out-of-range port), so it stays inside the same guard.
                port = proxy.port
            except ValueError:
                raise AptMirrorError(
                    f"apt-mirror: invalid proxy URL for '{key}'"
                ) from None
            username = proxy.username
            password = proxy.password
            if username is None:
                username = (variables.get("proxy_user") or "").strip()
                password = (variables.get("proxy_password") or "").strip()
            auth: bytes | None = None
            if username:
                token = base64.b64encode(f"{username}:{password}".encode())
                auth = b"Basic " + token
            proxies[target_scheme] = (proxy.hostname, port or 80, auth)
        return proxies

    def _connect(
        self,
        scheme: str,
        host: str,
        port: int,
        netloc: str,
        target: str,
        headers: dict[str, str],
    ) -> tuple[http.client.HTTPConnection, str]:
        """Open a connection to the target, honouring the proxy table.

        Returns the ready connection plus the request URL to send (an
        absolute-form URL for plain http through a proxy, the path target
        otherwise).  A proxy credential is added to ``headers``
        (Proxy-Authorization for plain http, CONNECT for tunnelled https).
        """
        proxy = self._proxies.get(scheme)
        if proxy is None:
            if scheme == "https":
                conn = http.client.HTTPSConnection(
                    host, port, timeout=_SOCKET_TIMEOUT, context=self._tls_context
                )
            else:
                conn = http.client.HTTPConnection(host, port, timeout=_SOCKET_TIMEOUT)
            return conn, target
        proxy_host, proxy_port, proxy_auth = proxy
        if scheme == "https":
            conn = http.client.HTTPSConnection(
                proxy_host,
                proxy_port,
                timeout=_SOCKET_TIMEOUT,
                context=self._tls_context,
            )
            conn.set_tunnel(host, port, proxy_authorization=proxy_auth or b"")
            return conn, target
        conn = http.client.HTTPConnection(
            proxy_host, proxy_port, timeout=_SOCKET_TIMEOUT
        )
        if proxy_auth:
            headers["Proxy-Authorization"] = proxy_auth.decode("ascii")
        # Plain http through a proxy needs the absolute-form request target.
        return conn, f"http://{netloc}{target}"

    @staticmethod
    def _range_start(response: http.client.HTTPResponse) -> int | None:
        """Return the first offset of a 206 Content-Range, or None if absent."""
        match = re.match(r"bytes (\d+)-", response.getheader("Content-Range") or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _require_complete(written: int, expected: int | None) -> None:
        """Raise a retryable error when a promised body arrived short.

        ``expected`` is the Content-Length captured before reading (None
        for chunked/unknown bodies, which have no promise to check).
        """
        if expected is not None and written != expected:
            raise _TruncatedBodyError(f"received {written} of {expected} bytes")

    def _write_body(
        self, response: http.client.HTTPResponse, part: Path, mode: str
    ) -> int:
        """Stream the response into ``part``, pacing each chunk to the rate.

        Returns the bytes written this call.  On a truncated body the HTTP
        exception propagates to the caller's retry logic while the partial
        data already written stays in ``part`` for the next resume attempt.
        """
        total = 0
        try:
            with open(part, mode) as handle:
                while chunk := response.read(_CHUNK_SIZE):
                    self._limiter.pace(len(chunk))
                    handle.write(chunk)
                    total += len(chunk)
        finally:
            response.close()
        return total

    def _walk_redirects(
        self,
        auth_state: dict[tuple[str, str, int], tuple[str | None, bool]],
        current: _Target,
        base_headers: dict[str, str],
    ) -> tuple[http.client.HTTPResponse, _Target, http.client.HTTPConnection]:
        """GET ``current``, following Location redirects up to the hop cap.

        Returns the final response (still open) with the final target and
        its open connection.  Every redirect issues a fresh GET on a
        fresh connection; a relative Location resolves against the current
        URL.  ``auth_state`` maps an origin to (credential, already sent)
        and is the source of truth for Authorization across hops: a
        same-origin redirect keeps its entry, a new origin starts with any
        userinfo embedded in the Location — anything else means the header
        is dropped (RFC 7235).  A 3xx without a usable Location, an
        exhausted cap, and a redirect to a foreign scheme raise
        FetchError/AptMirrorError; none of these count against the
        caller's retry budget.
        """
        hops = 0
        while True:
            headers = dict(base_headers)
            credential, sent = auth_state[current.origin]
            if credential is not None and sent:
                headers["Authorization"] = credential
            conn, request_url = self._connect(
                current.scheme,
                current.host,
                current.port,
                current.netloc,
                current.target,
                headers,
            )
            conn.request("GET", request_url, headers=headers)
            response = conn.getresponse()
            status = response.status
            if status not in (301, 302, 303, 307, 308):
                return response, current, conn
            location = response.getheader("Location")
            response.close()
            conn.close()
            if not location:
                raise FetchError(
                    current.clean_url,
                    f"HTTP {status} without a usable Location header",
                )
            hops += 1
            if hops > self._max_redirects:
                raise FetchError(
                    current.clean_url,
                    f"too many redirects (cap is {self._max_redirects})",
                )
            try:
                next_url = urllib.parse.urljoin(current.clean_url, location)
            except ValueError as error:
                raise FetchError(
                    current.clean_url, f"invalid redirect Location: {error}"
                ) from error
            next_target = _parse_target(next_url)
            auth_state.setdefault(
                next_target.origin,
                (
                    next_target.credential,
                    next_target.credential is not None and self._auth_no_challenge,
                ),
            )
            current = next_target

    def fetch(self, url: str, dest: Path) -> int:
        """Download ``url`` to ``dest`` atomically; return bytes written.

        The return value counts the bytes written to ``dest`` on *this*
        call: 0 when the server answered 304 (file untouched), the full
        size for a fresh 200 download, and only the appended portion when
        a partial ``<dest>.part`` is resumed via Range/206.

        Semantics (spec §5.3): only http/https — anything else raises
        AptMirrorError before any connection is opened.  Userinfo in the
        URL is stripped and sent as ``Authorization: Basic`` — always when
        ``auth_no_challenge`` is on, otherwise only after an initial 401
        challenge (one immediate, undelayed retry) — and never appears in
        any error, log line, or file.  301/302/303/307/308 responses are
        followed per their Location header up to ``max_redirects`` hops,
        each a fresh GET that does not count against the retry budget;
        a redirect to a different origin drops Authorization (RFC 7235),
        and a Location-less redirect or an exhausted cap is a FetchError.
        An existing ``dest`` triggers ``If-Modified-Since`` (304 -> 0); an
        existing ``<dest>.part`` triggers ``Range: bytes=N-`` resume (206
        appends, 200 restarts); on success the part is ``os.replace``d
        onto ``dest``.  Connection errors, timeouts, 429, and 5xx raise
        FetchError after up to _MAX_ATTEMPTS attempts with exponential
        backoff; 404 raises FetchNotFound and any other 4xx raises
        FetchError immediately.
        """
        first = _parse_target(url)
        auth_state: dict[tuple[str, str, int], tuple[str | None, bool]] = {
            first.origin: (
                first.credential,
                first.credential is not None and self._auth_no_challenge,
            )
        }
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(dest) + ".part")
        attempt = 0
        reason = "no attempt made"
        clean_url = first.clean_url
        while True:
            attempt += 1
            conn: http.client.HTTPConnection | None = None
            try:
                base_headers: dict[str, str] = {"User-Agent": "apt-mirror (python3)"}
                part_size = part.stat().st_size if part.exists() else 0
                if part_size:
                    base_headers["Range"] = f"bytes={part_size}-"
                elif dest.exists():
                    base_headers["If-Modified-Since"] = email.utils.formatdate(
                        timeval=dest.stat().st_mtime, usegmt=True
                    )
                response, final, conn = self._walk_redirects(
                    auth_state, first, base_headers
                )
                clean_url = final.clean_url
                status = response.status
                if status == 304:
                    response.close()
                    # dest is current; a leftover partial is stale.
                    if part.exists():
                        part.unlink()
                    return 0
                if status == 200:
                    expected = response.length
                    written = self._write_body(response, part, "wb")
                    self._require_complete(written, expected)
                    os.replace(part, dest)
                    return written
                if status == 206:
                    expected = response.length
                    start = self._range_start(response)
                    # A 206 that does not start where our partial ends is
                    # unusable for appending — restart from scratch.
                    mode = "ab" if start in (None, part_size) else "wb"
                    written = self._write_body(response, part, mode)
                    self._require_complete(written, expected)
                    os.replace(part, dest)
                    return written
                if status == 401:
                    response.close()
                    credential, sent = auth_state[final.origin]
                    if credential is not None and not sent:
                        # The server is challenging: raise the flag and
                        # re-walk immediately with the credential already
                        # at hand.  Not a failed attempt (no backoff).
                        auth_state[final.origin] = (credential, True)
                        attempt -= 1
                        continue
                    reason = "HTTP 401 (authentication failed)"
                    logger.warning("%s: %s", clean_url, reason)
                    raise FetchError(clean_url, reason)
                response.close()
                reason = f"HTTP {status}"
                if status == 404:
                    logger.warning("%s: %s", clean_url, reason)
                    raise FetchNotFound(clean_url, reason)
                if not (status == 429 or 500 <= status < 600):
                    logger.warning("%s: %s; not retryable", clean_url, reason)
                    raise FetchError(clean_url, reason)
            except (OSError, http.client.HTTPException) as error:
                reason = f"{type(error).__name__}: {error}"
            finally:
                if conn is not None:
                    conn.close()
            if attempt >= _MAX_ATTEMPTS:
                logger.warning(
                    "%s: %s (after %d attempts)", clean_url, reason, _MAX_ATTEMPTS
                )
                raise FetchError(
                    clean_url, f"{reason} (after {_MAX_ATTEMPTS} attempts)"
                )
            delay = self._backoff_base * (2 ** (attempt - 1))
            logger.warning(
                "%s: %s (attempt %d of %d); retrying in %.3fs",
                clean_url,
                reason,
                attempt,
                _MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Stage download orchestration (spec §5.3 concurrency, §5.11 output)
# ---------------------------------------------------------------------------

#: Wall-clock format for the ``Begin time`` / ``End time`` progress lines.
_TIME_FORMAT = "%a %b %e %H:%M:%S %Y %Z"


@dataclass
class StageResult:
    """Outcome of one download stage.

    ``ok`` counts the URLs that fetched cleanly, ``failed`` holds one
    ``(url, reason)`` pair per URL that could not be fetched, and ``bytes``
    is the total number of bytes written on this call.  URLs and reasons
    are in the credential-free form, safe to display or persist.
    """

    ok: int
    failed: list[tuple[str, str]]
    bytes: int


def download_stage(
    stage: str,
    urls: list[str],
    dest_root: Path,
    fetcher: Fetcher,
    variables: dict[str, str],
    var_path: Path,
) -> StageResult:
    """Download one stage's URLs into ``dest_root`` on a worker pool.

    Prints ``Downloading <N> files using <M> threads...`` with
    ``M = min(nthreads, len(urls))``, then ``Begin time: <ts>``; one
    ``[done/total]`` line per completed URL; then ``End time: <ts>`` (both
    timestamps in the fixed ``%a %b %e %H:%M:%S %Y %Z`` format); and
    finally the summary ``<ok> ok, <failed> failed, <bytes> bytes
    transferred`` followed by one ``  FAIL <url> (<reason>)`` line per
    failed URL.  Persists ``<var_path>/<stage>-urls.txt`` (all queued URLs,
    one per line, sorted) and ``<var_path>/<stage>-log.txt`` (one
    ``OK <url> <bytes>`` or ``FAIL <url> <reason>`` line per URL).  Every
    URL displayed or written here is in the credential-stripped form
    (``userinfo@`` removed); the fetcher itself receives the full URL.
    Each URL lands at ``dest_root / sanitise_uri(url, _tilde, _plus)``,
    with ``_tilde`` / ``_plus`` read via ``get_var``.

    The workers pull URLs from a shared queue (work-stealing, spec §5.3).
    FetchError / FetchNotFound become a failed entry and the worker
    continues; a fatal AptMirrorError (e.g. a bad scheme) propagates out of
    download_stage.  A stage with no URLs returns an empty StageResult and
    prints and writes nothing.
    """
    total = len(urls)
    if total == 0:
        return StageResult(ok=0, failed=[], bytes=0)
    raw = get_var(variables, "nthreads")
    try:
        nthreads = int(raw)
    except ValueError as error:
        raise AptMirrorError(f"apt-mirror: invalid nthreads '{raw}'") from error
    if nthreads < 1:
        raise AptMirrorError(f"apt-mirror: invalid nthreads '{raw}' (must be >= 1)")
    n_workers = min(nthreads, total)
    tilde = _config_on(get_var(variables, "_tilde"))
    plus = _config_on(get_var(variables, "_plus"))
    print(f"Downloading {total} files using {n_workers} threads...")
    print(f"Begin time: {time.strftime(_TIME_FORMAT, time.localtime())}")

    url_queue: queue.Queue[str] = queue.Queue()
    for url in urls:
        url_queue.put(url)
    lock = threading.Lock()
    done = 0
    results: dict[str, tuple[str, int | str]] = {}

    def worker() -> None:
        """Fetch URLs off the shared queue until it is drained."""
        nonlocal done
        while True:
            try:
                url = url_queue.get_nowait()
            except queue.Empty:
                return
            display = _strip_userinfo(url)
            try:
                written = fetcher.fetch(url, dest_root / sanitise_uri(url, tilde, plus))
            except FetchError as error:
                entry = ("FAIL", error.reason)
            else:
                entry = ("OK", written)
            with lock:
                results[display] = entry
                done += 1
                print(f"[{done}/{total}]")

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for future in (pool.submit(worker) for _ in range(n_workers)):
            future.result()
    print(f"End time: {time.strftime(_TIME_FORMAT, time.localtime())}")

    ok = 0
    byte_count = 0
    failed: list[tuple[str, str]] = []
    for display, (status, value) in results.items():
        if status == "OK":
            ok += 1
            byte_count += value
        else:
            failed.append((display, str(value)))
    failed.sort()

    var_path.mkdir(parents=True, exist_ok=True)
    (var_path / f"{stage}-urls.txt").write_text(
        "".join(f"{display}\n" for display in sorted(results)), encoding="utf-8"
    )
    log_lines = [
        f"{status} {display} {value}"
        for display, (status, value) in sorted(results.items())
    ]
    (var_path / f"{stage}-log.txt").write_text(
        "".join(f"{line}\n" for line in log_lines), encoding="utf-8"
    )
    print(f"{ok} ok, {len(failed)} failed, {byte_count} bytes transferred")
    for display, reason in failed:
        print(f"  FAIL {display} ({reason})")
    return StageResult(ok, failed, byte_count)


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


# ---------------------------------------------------------------------------
# Release parsing and index-URL generation (spec §5.6)
# ---------------------------------------------------------------------------

#: Hash sections recognised in a Release file, strongest first (spec §5.6.3).
HASH_STRENGTH: tuple[str, ...] = ("SHA512", "SHA256", "SHA1", "MD5Sum")

# Compressed extensions a Release lists index files under.  ``KNOWN_EXTENSIONS``
# is additionally the order the no-Release well-known fallback tries them in
# (spec §5.6.1): .gz, .xz, .bz2, .lzma, then uncompressed.
_RELEASE_EXT_RE = r"\.(?:gz|bz2|xz|lzma)"
KNOWN_EXTENSIONS: tuple[str, ...] = (".gz", ".xz", ".bz2", ".lzma", "")

#: A hash-section header line, exactly one of the four names followed by ":".
_HASH_HEADER_RE = re.compile(r"^(?P<hash>" + "|".join(HASH_STRENGTH) + r"):$")
#: A hash-section continuation line: leading whitespace plus its remainder.
_HASH_LINE_RE = re.compile(r"^ +(.*)$")


@dataclass
class HashEntry:
    """One ``<hashsum> <size> <filename>`` line of a Release hash section."""

    hashname: str
    hashsum: str
    size: int
    filename: str


@dataclass
class ReleaseInfo:
    """Parsed content of a Release or InRelease file (spec §5.6.2).

    ``entries`` maps a filename to every hash section it is listed in (one
    HashEntry per section, in file order); ``by_hash`` is True only for the
    exact field line ``Acquire-By-Hash: yes``; ``warnings`` holds one line
    per malformed checksum continuation.
    """

    by_hash: bool
    entries: dict[str, list[HashEntry]]
    warnings: list[str]


def parse_release(path: Path) -> ReleaseInfo:
    """Parse a Release or InRelease file into a ReleaseInfo.

    Recognises the ``SHA512:``, ``SHA256:``, ``SHA1:`` and ``MD5Sum:``
    group headers, each followed by ``<hashsum> <size> <filename>``
    continuation lines (3 whitespace-separated fields, spec §5.6.2).  A
    continuation line that does not split into exactly three fields (or
    whose size is not a whole number) appends the warning
    ``Malformed checksum line "<line>" in <path>`` and parsing continues —
    malformed content never raises.  ``by_hash`` is True only for the exact
    line ``Acquire-By-Hash: yes``; any other value (``no``, ``YES``, ...)
    means canonical downloads, like the original.
    """
    info = ReleaseInfo(by_hash=False, entries={}, warnings=[])
    current_hash: str | None = None
    for raw_line in path.read_text(encoding="utf-8").split("\n"):
        line = raw_line.rstrip("\r")
        if current_hash is not None:
            match = _HASH_LINE_RE.match(line)
            if match is not None:
                content = match.group(1)
                parts = content.split()
                entry = None
                if len(parts) == 3:
                    try:
                        size = int(parts[1])
                    except ValueError:
                        size = None
                    else:
                        entry = HashEntry(current_hash, parts[0], size, parts[2])
                if entry is None:
                    info.warnings.append(
                        f'Malformed checksum line "{content}" in {path}'
                    )
                else:
                    info.entries.setdefault(entry.filename, []).append(entry)
                continue
            current_hash = None
        header = _HASH_HEADER_RE.match(line)
        if header is not None:
            current_hash = header.group("hash")
        elif line == "Acquire-By-Hash: yes":
            info.by_hash = True
    return info


@dataclass
class IndexUrl:
    """One index file queued for the stage-2 (index) download phase.

    ``filename`` is the path joined onto its base URL: for a
    Release-driven URL it is exactly the Release entry, relative to the
    dist root (e.g. ``main/binary-amd64/Packages.gz``); for the no-Release
    well-known set it is relative to the repo URI (e.g.
    ``dists/stable/main/binary-amd64/Packages.gz``).  ``by_hash`` is the
    lower-cased name of the file's strongest advertised hash when an
    Acquire-By-Hash download applies, else None (a canonical download);
    ``by_hashsum`` is the matching digest.
    """

    filename: str
    size: int
    by_hash: str | None
    by_hashsum: str | None

    @property
    def by_hash_path(self) -> str:
        """The path part to fetch: the by-hash location, or the canonical one.

        Exactly ``posixpath.dirname(filename) + "/by-hash/" + by_hash + "/"
        + by_hashsum`` (spec §5.6.3) for a by-hash download, plain
        ``filename`` otherwise — so the absolute URL is the base URL
        (``dist_base`` for a Release-driven URL) plus this path.
        """
        if self.by_hash is None or self.by_hashsum is None:
            return self.filename
        return (
            f"{posixpath.dirname(self.filename)}/by-hash/"
            f"{self.by_hash}/{self.by_hashsum}"
        )


def dist_base(spec: Repo) -> str:
    """The absolute base URL of a repo's distribution tree (spec §5.6.1).

    ``<uri>/dists/<distribution>/`` for a normal repository and
    ``<uri>/<distribution>/`` for a flat one (``components`` is None),
    normalised with remove_double_slashes so a URI carrying a trailing
    slash does not double it.
    """
    if spec.components is None:
        return remove_double_slashes(f"{spec.uri}/{spec.distribution}/")
    return remove_double_slashes(f"{spec.uri}/dists/{spec.distribution}/")


def _alternation(names: list[str]) -> str:
    """A non-capturing ``(?:name1|name2)`` regex alternation over ``names``."""
    return "(?:" + "|".join(re.escape(name) for name in names) + ")"


def _index_patterns(spec: Repo) -> list[re.Pattern[str]]:
    """Whole-filename patterns selecting spec's index files (spec §5.6.2).

    Built from the original's pattern sets, anchored and whole-string
    matched (re.fullmatch): per-component and dist-root Contents,
    ``binary-<arch|all>/Packages`` + ``Release``, cnf, dep11, i18n for
    binary repos; ``source/Sources``, ``source/Release`` and
    ``Contents-source`` for source repos; flat-repo variants mirror only
    the single root ``Packages``/``Sources``.  The compression extension
    is optional: a Release may list an uncompressed index filename
    (``.../Packages``), and the stage-2/3 pipeline handles those like any
    other skel index (spec §5.7 opens the uncompressed file as-is).
    """
    ext = r"(?:" + _RELEASE_EXT_RE + ")?"
    if spec.components is None:
        names = ("Sources" + ext,) if spec.type == "deb-src" else ("Packages" + ext,)
        return [re.compile(name) for name in names]
    if spec.type == "deb-src":
        comp = _alternation(spec.components)
        patterns = (
            f"{comp}/source/Sources{ext}",
            f"{comp}/source/Release",
            f"{comp}/Contents-source{ext}",
        )
    else:
        arch = _alternation([part for part in spec.arch.split(",") if part] + ["all"])
        comp = _alternation(spec.components)
        patterns = (
            f"Contents-{arch}{ext}",
            f"{comp}/Contents-{arch}{ext}",
            f"{comp}/(?:debian-installer/)?binary-{arch}/Packages{ext}",
            f"{comp}/(?:debian-installer/)?binary-{arch}/Release",
            f"{comp}/cnf/Commands-{arch}{ext}",
            f"{comp}/dep11/Components-{arch}.*{ext}",
            f"{comp}/dep11/icons-.*{ext}",
            f"{comp}/i18n/Translation-.*{ext}",
        )
    return [re.compile(pattern) for pattern in patterns]


def _strongest_advertised(entries: list[HashEntry]) -> HashEntry | None:
    """The entry under the strongest hash section the file itself advertises.

    None when the file advertises no supported section at all.
    """
    by_name: dict[str, HashEntry] = {}
    for entry in entries:
        by_name.setdefault(entry.hashname, entry)
    for name in HASH_STRENGTH:
        entry = by_name.get(name)
        if entry is not None:
            return entry
    return None


def index_urls_for(spec: Repo, info: ReleaseInfo) -> list[IndexUrl]:
    """The stage-2 index URLs to fetch for ``spec`` (spec §5.6.2, §5.6.3).

    Filters ``info.entries``' filenames against the pattern set for the
    repo's type, architecture and components and returns one IndexUrl per
    matching filename, deduplicated by (filename, by_hash).  When the
    Release requests ``Acquire-By-Hash: yes`` each file is fetched via the
    strongest hash *it itself* advertises (spec §7.1 fix: files listed
    only under a weaker section are still mirrored, not silently dropped);
    a filename with no advertised entries — and every file when the
    request is absent or not exactly ``yes`` — falls back to the canonical
    path (``by_hash is None``).
    """
    patterns = _index_patterns(spec)
    urls: list[IndexUrl] = []
    seen: set[tuple[str, str | None]] = set()
    for filename, entries in info.entries.items():
        if not any(pattern.fullmatch(filename) for pattern in patterns):
            continue
        strongest = _strongest_advertised(entries)
        if info.by_hash and strongest is not None:
            url = IndexUrl(
                filename=filename,
                size=strongest.size,
                by_hash=strongest.hashname.lower(),
                by_hashsum=strongest.hashsum,
            )
        else:
            url = IndexUrl(
                filename=filename,
                size=strongest.size if strongest is not None else 0,
                by_hash=None,
                by_hashsum=None,
            )
        key = (url.filename, url.by_hash)
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def _known_base_paths(spec: Repo) -> list[str]:
    """Well-known index base paths (no extension) for ``spec``, deduped.

    Relative to the repo URI, mirroring the original's no-Release loop:
    per-component plus dist-root Contents for a normal binary repo,
    per-component ``source/Sources`` and ``Contents-source`` for a source
    repo (under ``dists/<distribution>`` — the original omitted that
    segment for source lines and never mirrored them, spec §7.1 fix #1),
    and flat variants with no ``dists/`` segment at all.
    """
    dist = spec.distribution
    if spec.type == "deb-src":
        if spec.components is None:
            return [f"{dist}/Sources"]
        bases = [f"dists/{dist}/{comp}/source/Sources" for comp in spec.components]
        bases += [f"dists/{dist}/{comp}/Contents-source" for comp in spec.components]
    else:
        if spec.components is None:
            return [
                f"{dist}/Packages",
                f"{dist}/Contents-{spec.arch}",
                f"{dist}/Contents-all",
            ]
        bases = []
        for comp in spec.components:
            bases += [
                f"dists/{dist}/{comp}/binary-{spec.arch}/Packages",
                f"dists/{dist}/{comp}/binary-all/Packages",
                f"dists/{dist}/{comp}/Contents-{spec.arch}",
                f"dists/{dist}/{comp}/Contents-all",
                f"dists/{dist}/Contents-{spec.arch}",
                f"dists/{dist}/Contents-all",
            ]
    return list(dict.fromkeys(bases))


def known_index_urls(spec: Repo) -> list[IndexUrl]:
    """The unsigned-repo fallback index set (spec §5.6.1, §7.1 fix #1).

    Used when neither InRelease nor Release could be downloaded: the
    well-known index paths, each once per extension in the order ``.gz
    .xz .bz2 .lzma`` and then uncompressed, all canonical
    (``by_hash is None``).  Filenames are repo-URI-relative; a normal
    source repo keeps the ``dists/<distribution>`` segment and a flat
    source repo is simply ``<distribution>/Sources``.
    """
    urls: list[IndexUrl] = []
    seen: set[str] = set()
    for base in _known_base_paths(spec):
        for extension in KNOWN_EXTENSIONS:
            filename = base + extension
            if filename in seen:
                continue
            seen.add(filename)
            urls.append(
                IndexUrl(filename=filename, size=0, by_hash=None, by_hashsum=None)
            )
    return urls


# ---------------------------------------------------------------------------
# Index (Packages/Sources) processing (spec §5.7)
# ---------------------------------------------------------------------------

#: A field line of an index block: a column-0 ``Name:`` followed by the
#: raw remainder of the line (the value may itself contain colons).
_INDEX_FIELD_RE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+):(?P<value>.*)$")


@dataclass
class PackageRec:
    """One archive file named by a Packages or Sources index entry.

    ``mirror_rel`` is the path relative to the mirror tree root (no
    leading slash), already run through remove_double_slashes;
    ``url_rel`` is relative to the repo root, so its download URL is
    ``<uri>/`` + ``url_rel`` (the ``Filename:`` value for a Packages
    entry, ``<directory>/<filename>`` for a Sources one).  ``md5`` /
    ``sha1`` / ``sha256`` are None when the index field is absent.
    """

    mirror_rel: str
    size: int
    md5: str | None
    sha1: str | None
    sha256: str | None
    url_rel: str


def _read_index(path: Path) -> str:
    """Read the full text of index file ``path``, decompressing by suffix.

    ``.gz`` is decompressed with ``gzip``, ``.bz2`` with ``bz2``, and
    ``.lzma`` (or ``.xz``) with ``lzma``; any other suffix is read as
    plain text.  All decompression is in-process — the original's
    shell-out to external decompressors is not reproduced (spec §5.7).
    """
    suffix = path.suffix
    if suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    if suffix == ".bz2":
        with bz2.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    if suffix in (".lzma", ".xz"):
        with lzma.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8")


def iter_index_blocks(path: Path) -> Iterator[dict[str, str]]:
    """Yield the blocks of index file ``path`` as field-name dicts.

    The file is first decompressed by its suffix (see _read_index).
    Blocks are the blank-line-separated chunks of the text; each becomes
    an insertion-ordered dict mapping every field name to its value.
    An indented continuation line and a repeated column-0 field name both
    extend the previous field's value, joined with ``\\n``; every value
    is stripped of surrounding whitespace — the net effect of the
    original's ``split(/^([\\w\\-]+:)/m)`` plus ``chomp`` plus
    ``remove_spaces``.  A colon inside a value (``Homepage: http://x/``)
    does not start a new field: only a column-0 ``Name:`` does.
    """
    block: dict[str, str] = {}
    current: str | None = None
    for raw_line in _read_index(path).split("\n"):
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            if block:
                yield block
                block = {}
                current = None
            continue
        match = _INDEX_FIELD_RE.match(line)
        if match is not None:
            current = match["name"]
            piece = match["value"].strip()
        elif current is None:
            continue
        else:
            piece = stripped
        if not piece:
            continue
        if block.get(current):
            block[current] = f"{block[current]}\n{piece}"
        else:
            block[current] = piece
    if block:
        yield block


def need_update(mirror_file: Path, size: int) -> bool:
    """Whether ``mirror_file`` must (re)download, and prune a corrupt one.

    True when the file is missing; False when it exists with exactly
    ``size`` bytes; and True when it exists with a different size, in
    which case the corrupt file is deleted first so the next download
    starts from scratch (spec §5.7).  The comparison is against the stat
    size (``st.st_size``, i.e. stat.S_SIZE) — the file is never read.
    """
    try:
        st_size = mirror_file.stat().st_size
    except OSError:
        return True
    if st_size == size:
        return False
    mirror_file.unlink(missing_ok=True)
    return True


def _index_size(raw: str | None) -> int:
    """An index ``Size:`` value as an int (absent or non-numeric -> 0)."""
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _packages_record(block: dict[str, str], tree_prefix: str) -> PackageRec:
    """The PackageRec of one ``Filename:`` (Packages index) block."""
    filename = block["Filename"]
    return PackageRec(
        mirror_rel=remove_double_slashes(f"{tree_prefix}/{filename}"),
        size=_index_size(block.get("Size")),
        md5=block.get("MD5sum"),
        sha1=block.get("SHA1"),
        sha256=block.get("SHA256"),
        url_rel=filename,
    )


def _sources_records(block: dict[str, str], tree_prefix: str) -> list[PackageRec]:
    """The PackageRecs of one Sources block, one per ``File:`` line.

    Each line must split into exactly three ``<hash> <size> <filename>``
    fields and the block must carry a ``Directory:`` (a leading slash is
    normalised away); anything else raises the pinned
    "apt-mirror: invalid Sources format" error.
    """
    directory = block.get("Directory")
    if directory is None:
        raise AptMirrorError("apt-mirror: invalid Sources format")
    directory = directory.lstrip("/")
    files = block.get("File") or block.get("Files")
    records: list[PackageRec] = []
    for line in (files or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 3:
            raise AptMirrorError("apt-mirror: invalid Sources format")
        digest, size_raw, filename = parts
        try:
            size = int(size_raw)
        except ValueError:
            raise AptMirrorError("apt-mirror: invalid Sources format") from None
        url_rel = f"{directory}/{filename}"
        records.append(
            PackageRec(
                mirror_rel=remove_double_slashes(f"{tree_prefix}/{url_rel}"),
                size=size,
                md5=digest,
                sha1=None,
                sha256=None,
                url_rel=url_rel,
            )
        )
    return records


def parse_index(path: Path, tree_prefix: str) -> list[PackageRec]:
    """Parse a Packages/Sources index file into archive records (spec §5.7).

    ``tree_prefix`` is the repo's path in the mirror tree (sanitise_uri of
    the repo URI).  A block with ``Filename:`` yields one Packages record
    (digests from the ``MD5sum:`` / ``SHA1:`` / ``SHA256:`` fields when
    present else None, ``size`` from ``Size:`` or 0 when the field is
    absent, ``url_rel`` the ``Filename:`` value itself); a block with
    ``File:`` (or ``Files:``) yields one Sources record per line.  Blocks
    with neither field are ignored (e.g. a stray metadata-only block).
    Raises AptMirrorError for malformed Sources content (see
    _sources_records).
    """
    records: list[PackageRec] = []
    for block in iter_index_blocks(path):
        if "Filename" in block:
            records.append(_packages_record(block, tree_prefix))
        elif "File" in block or "Files" in block:
            records.extend(_sources_records(block, tree_prefix))
    return records


# ---------------------------------------------------------------------------
# Skel-to-mirror copy, cleanup, locking, postmirror, and main (spec §5.8-5.11)
# ---------------------------------------------------------------------------


def _quoted_path(path: str) -> str:
    """A path single-quoted for POSIX sh, with ``'`` escaped as ``'\\''``.

    Ports the original's ``quoted_path`` so the generated cleanup script
    survives any character in the mirror paths.
    """
    return "'" + path.replace("'", "'\\''") + "'"


def copy_file(from_p: Path, to_p: Path, unlink_repl: bool) -> None:
    """Copy (preferring a hardlink) ``from_p`` to ``to_p`` (spec §5.8).

    Skips entirely when ``from_p`` is missing (parents are not created);
    creates the destination's parent directories; when the destination
    already matches the source's ``(st_size, st_mtime_ns)``, skips (the
    common unchanged case); when ``unlink_repl`` (the ``unlink`` config
    option) is set and the destination's content differs, deletes it
    first; hardlinks when possible and falls back to a copy for the
    cross-device case; and finally restores the source's atime/mtime onto
    the destination.  A failed copy is a warning, not a fatal error (like
    the original ``copy_file``).
    """
    if not from_p.is_file():
        return
    to_p.parent.mkdir(parents=True, exist_ok=True)
    from_stat = from_p.stat()
    if to_p.is_file():
        to_stat = to_p.stat()
        if (from_stat.st_size, from_stat.st_mtime_ns) == (
            to_stat.st_size,
            to_stat.st_mtime_ns,
        ):
            return
        if unlink_repl and not filecmp.cmp(str(from_p), str(to_p), shallow=False):
            to_p.unlink(missing_ok=True)
    try:
        try:
            os.link(from_p, to_p)
        except OSError:
            shutil.copy2(from_p, to_p)
    except OSError as error:
        logger.warning("apt-mirror: can't copy %s to %s: %s", from_p, to_p, error)
        return
    os.utime(to_p, ns=(from_stat.st_atime_ns, from_stat.st_mtime_ns))


@dataclass
class Unneeded:
    """The files/directories the cleanup removes (spec §5.9).

    ``files`` and ``dirs`` hold mirror-tree-relative POSIX paths (files in
    walk order; directories bottom-up, so a parent is listed only once its
    children are gone); ``nbytes`` totals the removed files' on-disk size
    in 512-byte block units.
    """

    files: list[str]
    dirs: list[str]
    nbytes: int


def collect_unneeded(
    mirror_root: Path,
    clean_dirs: list[str],
    skipclean: set[str],
    tilde: bool,
    plus: bool,
) -> Unneeded:
    """Walk each clean directory and collect what the cleanup removes.

    ``clean_dirs`` are mirror-root-relative trees (the config ``clean``
    URIs, sanitised).  A file is needed when its tree-relative path, after
    the ``_tilde``/``_plus`` normalisation (``~`` -> ``%7E``, ``+`` ->
    ``%2B``), is in ``skipclean``; a directory is needed when it (or any
    path under it) is needed — **symlinks are always considered live**, so
    a directory containing only symlinks is kept.  Non-needed files are
    listed for removal, a directory is listed only once every path under
    it is removable (bottom-up), and removed-file sizes are summed in
    512-byte block units (``st_blocks * 512``).  A missing or
    non-directory (or symlinked) clean root is skipped silently.
    """

    def key(rel: str) -> str:
        key = rel
        if tilde:
            key = key.replace("~", "%7E")
        if plus:
            key = key.replace("+", "%2B")
        return key

    files: list[str] = []
    dirs: list[str] = []
    nbytes = 0

    def process_file(rel: str) -> bool:
        nonlocal nbytes
        if key(rel) in skipclean:
            return True
        files.append(sanitise_uri(rel, tilde=tilde, plus=plus))
        with contextlib.suppress(OSError):
            nbytes += (mirror_root / rel).stat().st_blocks * 512
        return False

    def process_directory(rel: str) -> bool:
        if key(rel) in skipclean:
            return True
        needed = False
        try:
            entries = sorted(os.scandir(mirror_root / rel), key=lambda e: e.name)
        except OSError as error:
            raise AptMirrorError(f"apt-mirror: can't opendir {rel}") from error
        for entry in entries:
            sub = f"{rel}/{entry.name}"
            if entry.is_symlink():
                # Symlinks are always considered live (original
                # process_symlink) and are never followed.
                needed = True
            elif entry.is_dir(follow_symlinks=False):
                # Always recurse — the original's ``|=`` never
                # short-circuits, so a needed sibling must not skip the
                # walk of the remaining entries.
                if process_directory(sub):
                    needed = True
            elif entry.is_file(follow_symlinks=False) and process_file(sub):
                needed = True
        if not needed:
            dirs.append(rel)
        return needed

    for clean_dir in clean_dirs:
        path = mirror_root / clean_dir
        if path.is_dir() and not path.is_symlink():
            process_directory(clean_dir)
    return Unneeded(files=files, dirs=dirs, nbytes=nbytes)


def write_cleanscript(
    path: Path,
    mirror_path: str,
    files: list[str],
    dirs: list[str],
) -> None:
    """Generate the POSIX sh cleanup script (spec §5.9, original 1107-1145).

    Emits ``#!/bin/sh``, ``set -e``, ``cd '<mirror path>'``, one
    ``rm -f '<file>'`` line per file with the original's ``[NN%]``/``.``
    progress cadence — guarded: no percentage is ever emitted for an
    empty list (the original's divide-by-zero fix, spec §7) and a
    terminal ``[100%]`` marks the end of each non-empty section — then
    ``rm -r`` guarded by ``test -d`` per directory.  The header's size
    string is computed the way the original does it: from the on-disk
    (512-byte-block) usage of the listed files under ``mirror_path``.
    All paths are single-quoted with ``'`` escaped as ``'\\''`` (see
    _quoted_path).  The script is finished with ``chmod +x``.
    """
    nbytes = 0
    for name in files:
        try:
            nbytes += (Path(mirror_path) / name).stat().st_blocks * 512
        except OSError:
            continue
    size_output = format_bytes(nbytes)

    lines: list[str] = [
        "#!/bin/sh",
        "set -e",
        "",
        f"cd {_quoted_path(mirror_path)}",
        "",
        f"echo 'Removing {len(files)} unnecessary files [{size_output}]...'",
    ]
    total = len(files)
    for i, name in enumerate(files):
        lines.append(f"rm -f {_quoted_path(name)}")
        if total:
            if i % 500 == 0:
                lines.append(f"echo -n '[{100 * i // total}%]'")
            if i % 10 == 0:
                lines.append("echo -n .")
    if total:
        lines.append("echo -n '[100%]'")
    lines += ["echo 'done.'", "echo", ""]

    total = len(dirs)
    lines.append(f"echo 'Removing {total} unnecessary directories...'")
    for i, name in enumerate(dirs):
        quoted = _quoted_path(name)
        lines.append(f"if test -d {quoted}; then rm -r {quoted}; fi")
        if total:
            if i % 50 == 0:
                lines.append(f"echo -n '[{100 * i // total}%]'")
            lines.append("echo -n .")
    if total:
        lines.append("echo -n '[100%]'")
    lines += ["echo 'done.'", "echo"]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | 0o111)


@contextmanager
def locked(var_path: Path) -> Iterator[None]:
    """Hold an exclusive non-blocking ``flock`` on
    ``<var_path>/apt-mirror.lock`` (spec §5.2).

    On acquisition failure raises AptMirrorError with the original's
    exact message (the lock file is left in place); on success the lock
    is released and the lock file removed on exit — so a stale
    (holderless) lock file left by a crash is simply re-acquired and does
    not survive the next clean exit.
    """
    lock_file = var_path / "apt-mirror.lock"
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        raise AptMirrorError("apt-mirror is already running, exiting") from error
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        lock_file.unlink(missing_ok=True)


def run_postmirror(variables: dict[str, str]) -> None:
    """Run the postmirror script with the cwd set to ``mirror_path``.

    Prints the three fixed original messages; runs the script directly
    when it is executable, otherwise via ``/bin/sh`` (spec §5.10 — the
    original still chdirs to ``mirror_path`` and user scripts may rely on
    it).  A non-zero child exit status (or an exec failure) is a warning
    only and never changes the process exit code.
    """
    script = get_var(variables, "postmirror_script")
    mirror_dir = Path(get_var(variables, "mirror_path"))
    print("Running the Post Mirror script ...")
    print(f"({script})\n")
    command = [script] if os.access(script, os.X_OK) else ["/bin/sh", script]
    return_code: int | None = None
    try:
        result = subprocess.run(command, cwd=mirror_dir)
        return_code = result.returncode
    except OSError as error:
        logger.warning(
            "apt-mirror: can't run postmirror script '%s': %s", script, error
        )
    if return_code is not None and return_code != 0:
        logger.warning(
            "apt-mirror: postmirror script exited with status %d", return_code
        )
    print(
        "\nPost Mirror script has completed. "
        "See above output for any possible errors.\n"
    )


#: The per-distribution release files requested in stage 1 (spec §5.5).
_RELEASE_NAMES = ("InRelease", "Release", "Release.gpg")

#: Index file extensions tried when opening a skel index, in the original's
#: ``process_index`` order (``xz gz bz2 lzma``), the last entry being the
#: uncompressed file itself (spec §5.7).
_INDEX_EXTENSIONS = (".xz", ".gz", ".bz2", ".lzma", "")

#: Compression/decoding errors that mark a skel index as corrupt and
#: eligible for a one-shot re-fetch (spec §7.1 fix #4).
_INDEX_PARSE_ERRORS: tuple[type[BaseException], ...] = (
    OSError,  # gzip/bz2/lzma surface read failures here
    EOFError,
    UnicodeDecodeError,
    zlib.error,
    lzma.LZMAError,
)


def main(argv: list[str] | None = None) -> int:
    """Run one full mirror pass (spec §5); return 0 on success.

    ``argv`` defaults to ``sys.argv[1:]``.  ``--version`` prints
    ``apt-mirror (python3) 1.0.0`` and returns 0.  Any AptMirrorError
    (config, directory, or lock failure) is printed to stderr and yields
    a 1; per-URL download failures are never fatal.
    """
    parser = argparse.ArgumentParser(
        prog="apt-mirror.py",
        description="Mirror apt repositories (Python reimplementation of "
        "the Perl apt-mirror).",
    )
    parser.add_argument(
        "configfile",
        nargs="?",
        default="/etc/apt/mirror.list",
        help="mirror.list config file (default: /etc/apt/mirror.list)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version string and exit",
    )
    args = parser.parse_args(argv)
    if args.version:
        print("apt-mirror (python3) 1.0.0")
        return 0
    try:
        return _mirror_run(args.configfile)
    except AptMirrorError as error:
        print(str(error), file=sys.stderr)
        return 1


def _mirror_run(configfile: str) -> int:
    """The full stage pipeline (spec §5): returns 0 on success.

    Stage order (the original's): create the three directories -> lock ->
    release -> index -> archive -> skel-to-mirror copy -> clean ->
    postmirror.  AptMirrorError propagates to main's handler; per-URL
    failures stay within their stage.
    """
    config = parse_config(configfile)
    variables = config.variables
    tilde = _config_on(get_var(variables, "_tilde"))
    plus = _config_on(get_var(variables, "_plus"))
    mirror_path = Path(get_var(variables, "mirror_path"))
    skel_path = Path(get_var(variables, "skel_path"))
    var_path = Path(get_var(variables, "var_path"))
    cleanscript_path = Path(get_var(variables, "cleanscript"))
    unlink_repl = _config_on(get_var(variables, "unlink"))

    # The three needed directories (spec §5.2), created before anything
    # else is touched.
    for directory in (mirror_path, skel_path, var_path):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise AptMirrorError(
                f"apt-mirror: can't create {directory} directory"
            ) from error

    fetcher = Fetcher(variables)
    specs = config.binaries + config.sources

    # Shared per-run state.  skipclean starts with the config skip-clean
    # entries and gains every queued file (canonical **and** by-hash tree
    # spelling) plus every archive record's tree path.
    skipclean: set[str] = set(config.skipclean)
    hashsum_to_canonical: dict[str, list[str]] = {}
    canonical_to_hashsums: dict[str, list[str]] = {}
    release_urls: list[str] = []
    index_urls: list[str] = []
    index_urls_seen: set[str] = set()

    with locked(var_path):
        # ----------------------- Stage 1: release (spec §5.5) ---------
        def queue_release_urls() -> None:
            """Queue InRelease/Release/Release.gpg for every spec,
            registering each in skipclean (original add_url_to_download)."""
            seen: set[str] = set()
            for spec in specs:
                for name in _RELEASE_NAMES:
                    url = remove_double_slashes(dist_base(spec) + name)
                    if url in seen:
                        continue
                    seen.add(url)
                    release_urls.append(url)
                    skipclean.add(sanitise_uri(url, tilde, plus))

        queue_release_urls()
        download_stage(
            "release",
            sorted(release_urls),
            skel_path,
            fetcher,
            variables,
            var_path,
        )

        # ------------------------ Stage 2: index (spec §5.6) ----------
        def valid_release_info(path: Path) -> ReleaseInfo | None:
            """The ReleaseInfo of a non-empty, parseable Release file, or
            None when the copy is missing, empty, or unparseable (spec
            §7.1 fix #4 — a truncated Release must not survive the
            validity check).  parse_release never raises: it collects
            warnings and simply stops when the fragment runs out of
            checksum sections, so a truncated fragment (e.g. 10 leftover
            bytes) parses to zero entries — and a release that names no
            file at all is corrupt, not valid."""
            if not path.is_file():
                return None
            try:
                if path.stat().st_size == 0:
                    return None
                info = parse_release(path)
            except (OSError, UnicodeDecodeError, ValueError):
                return None
            if not info.entries:
                return None
            return info

        def spec_release_info(spec: Repo) -> ReleaseInfo | None:
            """The usable InRelease (else Release) of ``spec``.

            A local copy that exists but is empty or fails parse_release
            is deleted and re-fetched once.  Every warning carried by a
            usable ReleaseInfo (Task 5's malformed-continuation lines,
            spec §5.6) is emitted to stderr, verbatim, right after the
            parse that produced it — both the initial and the
            re-fetch paths land on the same return.  When neither file
            yields a parseable Release the exact original warning is
            printed (to stderr) and None is returned so the caller falls
            back to the well-known index set."""
            for name in ("InRelease", "Release"):
                url = dist_base(spec) + name
                path = skel_path / sanitise_uri(url, tilde, plus)
                info = valid_release_info(path)
                if info is None and path.exists():
                    path.unlink(missing_ok=True)
                    with contextlib.suppress(FetchError):
                        fetcher.fetch(url, path)
                    info = valid_release_info(path)
                if info is not None:
                    for warning in info.warnings:
                        print(warning, file=sys.stderr)
                    return info
            print(
                "Failed to find InRelease or Release in "
                f"{skel_path}/{sanitise_uri(dist_base(spec), tilde, plus)}",
                file=sys.stderr,
            )
            return None

        def queue_index_urls(spec: Repo) -> None:
            """Queue ``spec``'s index files for the skel stage, registering
            every queued file (canonical and by-hash tree paths) in
            skipclean and populating the two by-hash maps (spec §5.6.4)."""
            info = spec_release_info(spec)
            if info is None:
                indices = known_index_urls(spec)
                # Fallback filenames are repo-URI-relative: no dist/
                # prepending (controller clarification 1).
                base_url = spec.uri
            else:
                indices = index_urls_for(spec, info)
                # Release-driven filenames are dist-root-relative.
                base_url = dist_base(spec)
            for idx in indices:
                url = remove_double_slashes(base_url + "/" + idx.by_hash_path)
                canonical_tree = sanitise_uri(
                    remove_double_slashes(base_url + "/" + idx.filename),
                    tilde,
                    plus,
                )
                skipclean.add(canonical_tree)
                if idx.by_hash is not None and idx.by_hashsum is not None:
                    hash_tree = sanitise_uri(url, tilde, plus)
                    skipclean.add(hash_tree)
                    hashsum_to_canonical.setdefault(hash_tree, []).append(
                        canonical_tree
                    )
                    canonical_to_hashsums.setdefault(canonical_tree, []).append(
                        hash_tree
                    )
                if url not in index_urls_seen:
                    index_urls_seen.add(url)
                    index_urls.append(url)

        marks: list[str] = []
        for spec in specs:
            # One M per config entry, in the original's output order
            # (binaries first, then sources).
            marks.append("M")
            queue_index_urls(spec)
        print(f"Processing metadata files from releases [{''.join(marks)}]")
        print()
        download_stage(
            "index", sorted(index_urls), skel_path, fetcher, variables, var_path
        )

        # By-hash index files were stored under their by-hash path; the
        # original fans them out to their canonical skel paths right after
        # the stage (download_urls, original 339-348 — the cwd is the skel
        # there), so the archive stage can open the canonical index files.
        for hash_tree, canonicals in hashsum_to_canonical.items():
            for canonical in canonicals:
                copy_file(skel_path / hash_tree, skel_path / canonical, unlink_repl)

        # ----------------------- Stage 3: archive (spec §5.7) ---------
        all_lines: list[str] = []
        new_lines: list[str] = []
        md5_lines: list[str] = []
        sha1_lines: list[str] = []
        sha256_lines: list[str] = []
        archive_urls: dict[str, int] = {}

        def read_index(spec: Repo, path: Path, url: str, tree_prefix: str):
            """Parse one skel index file, deleting and re-fetching a
            corrupt copy once (spec §7.1 fix #4).  A malformed Sources
            format is a hard error (original die); every other parse
            failure is a warning (the index is treated as absent)."""
            try:
                return parse_index(path, tree_prefix)
            except AptMirrorError:
                raise
            except _INDEX_PARSE_ERRORS:
                path.unlink(missing_ok=True)
                try:
                    fetcher.fetch(url, path)
                except FetchError:
                    logger.warning(
                        "apt-mirror: can't open index %s in process_index", path
                    )
                    return []
                try:
                    return parse_index(path, tree_prefix)
                except AptMirrorError:
                    raise
                except _INDEX_PARSE_ERRORS:
                    logger.warning(
                        "apt-mirror: can't open index %s in process_index", path
                    )
                    return []

        def process_index_spec(spec: Repo, index_rel: str, optional: bool) -> None:
            """The original's process_index: open ``spec``'s skel index at
            ``index_rel`` (trying each compressed extension, then the
            uncompressed file) and do the ALL/NEW/MD5/SHA1/SHA256
            bookkeeping plus the archive URL queue for every record."""
            tree_prefix = sanitise_uri(spec.uri, tilde, plus)
            skel_base = skel_path / tree_prefix
            for extension in _INDEX_EXTENSIONS:
                rel = f"{index_rel}{extension}"
                path = skel_base / rel
                if not path.is_file():
                    continue
                for rec in read_index(spec, path, f"{spec.uri}/{rel}", tree_prefix):
                    # One source of truth per record: every bookkeeping
                    # list and the skipclean set see the very same
                    # PackageRec.
                    skipclean.add(rec.mirror_rel)
                    all_lines.append(rec.mirror_rel)
                    if rec.md5 is not None:
                        md5_lines.append(f"{rec.md5}  {rec.mirror_rel}")
                    if rec.sha1 is not None:
                        sha1_lines.append(f"{rec.sha1}  {rec.mirror_rel}")
                    if rec.sha256 is not None:
                        sha256_lines.append(f"{rec.sha256}  {rec.mirror_rel}")
                    if need_update(mirror_path / rec.mirror_rel, rec.size):
                        # Full (possibly userinfo-carrying) URL for the
                        # download queue; var/NEW is a persisted file and
                        # must carry the credential-stripped form only
                        # (spec §7.1 fix #2).
                        download_url = remove_double_slashes(
                            f"{spec.uri}/{rec.url_rel}"
                        )
                        new_lines.append(_strip_userinfo(download_url))
                        archive_urls[download_url] = rec.size
                return
            if not optional:
                logger.warning(
                    "apt-mirror: can't open index %s/%s in process_index",
                    tree_prefix,
                    index_rel,
                )

        marks = []
        for spec in config.sources:
            # One S per source spec.
            marks.append("S")
            if spec.components is not None:
                for comp in spec.components:
                    process_index_spec(
                        spec,
                        f"dists/{spec.distribution}/{comp}/source/Sources",
                        False,
                    )
            else:
                process_index_spec(spec, f"{spec.distribution}/Sources", False)
        for spec in config.binaries:
            # One P per binary spec.
            marks.append("P")
            if spec.components is not None:
                for comp in spec.components:
                    d = spec.distribution
                    process_index_spec(
                        spec,
                        f"dists/{d}/{comp}/binary-{spec.arch}/Packages",
                        False,
                    )
                    process_index_spec(
                        spec, f"dists/{d}/{comp}/binary-all/Packages", True
                    )
                    process_index_spec(
                        spec,
                        f"dists/{d}/{comp}/debian-installer/"
                        f"binary-{spec.arch}/Packages",
                        True,
                    )
                    process_index_spec(
                        spec,
                        f"dists/{d}/{comp}/debian-installer/binary-all/Packages",
                        True,
                    )
            else:
                process_index_spec(spec, f"{spec.distribution}/Packages", False)
        print(f"Processing indexes: [{''.join(marks)}]")
        print()

        # The five intermediate files, truncated fresh at the start of the
        # stage (the original opens them before any record is read); an
        # empty stage still leaves them in place.
        for name, lines in (
            ("ALL", all_lines),
            ("NEW", new_lines),
            ("MD5", md5_lines),
            ("SHA1", sha1_lines),
            ("SHA256", sha256_lines),
        ):
            (var_path / name).write_text(
                "".join(f"{line}\n" for line in lines), encoding="utf-8"
            )

        need_bytes = sum(archive_urls.values())
        print(f"{format_bytes(need_bytes)} will be downloaded into archive.")
        download_stage(
            "archive",
            sorted(archive_urls),
            mirror_path,
            fetcher,
            variables,
            var_path,
        )

        # --------------------- Skel -> mirror copy (spec §5.8) --------
        for url in release_urls + index_urls:
            sanitized = sanitise_uri(url, tilde, plus)
            copy_file(skel_path / sanitized, mirror_path / sanitized, unlink_repl)
            # A by-hash download fans out to its canonical path, and (as
            # the original does) to every other by-hash path mapped to
            # that canonical file.
            for canonical in hashsum_to_canonical.get(sanitized, []):
                copy_file(
                    mirror_path / sanitized,
                    mirror_path / canonical,
                    unlink_repl,
                )
                for other_hash in canonical_to_hashsums.get(canonical, []):
                    copy_file(
                        mirror_path / sanitized,
                        mirror_path / other_hash,
                        unlink_repl,
                    )

        # --------------------------- Cleaning (spec §5.9) -------------
        unneeded = collect_unneeded(
            mirror_path, sorted(config.clean), skipclean, tilde, plus
        )
        # The original opens (truncates) the cleanscript regardless of
        # mode; in _autoclean mode it stays empty.
        try:
            cleanscript_path.parent.mkdir(parents=True, exist_ok=True)
            cleanscript_path.touch()
        except OSError:
            pass
        if _config_on(get_var(variables, "_autoclean")):
            print(
                f"{format_bytes(unneeded.nbytes)} in "
                f"{len(unneeded.files)} files and {len(unneeded.dirs)} "
                f"directories will be freed...",
                end="",
            )
            for name in unneeded.files:
                (mirror_path / name).unlink(missing_ok=True)
            for name in unneeded.dirs:
                with contextlib.suppress(OSError):
                    (mirror_path / name).rmdir()
        else:
            print(
                f"{format_bytes(unneeded.nbytes)} in "
                f"{len(unneeded.files)} files and {len(unneeded.dirs)} "
                f"directories can be freed.\n"
            )
            print(f"Run {cleanscript_path} for this purpose.\n")
            write_cleanscript(
                cleanscript_path, str(mirror_path), unneeded.files, unneeded.dirs
            )

        # ------------------------- Postmirror (spec §5.10) ------------
        if _config_on(get_var(variables, "run_postmirror")):
            script_path = Path(get_var(variables, "postmirror_script"))
            if script_path.is_file():
                run_postmirror(variables)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
