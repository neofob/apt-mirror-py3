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

import base64
import email.utils
import http.client
import logging
import os
import platform
import queue
import re
import ssl
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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
