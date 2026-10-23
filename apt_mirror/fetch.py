"""In-process HTTP(S) fetch layer: fetcher, rate limiter, and fetch errors (spec §5.3)."""

import base64
import email.utils
import http.client
import logging
import os
import re
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from apt_mirror.config import DEFAULT_CONFIG, _config_on, parse_rate
from apt_mirror.errors import AptMirrorError
from apt_mirror.util import _strip_userinfo

logger = logging.getLogger("apt-mirror")

#: Response bodies are read (and paced) in chunks this large.
_CHUNK_SIZE = 256 * 1024

#: Total per-URL attempts (1 initial + 4 retries) on transient failures.
_MAX_ATTEMPTS = 5

#: Socket timeout, in seconds, applied to every connection.
_SOCKET_TIMEOUT = 30.0


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
    invalid value raises AptMirrorError via parse_rate) sets the per-
    transfer rate — every fetch() call paces its own body reads with a
    fresh RateLimiter (spec §5.3: the limit is per transfer, matching
    per-wget-process semantics); ``no_check_certificate``,
    ``ca_certificate``, ``certificate`` and ``private_key`` shape the TLS
    context; ``use_proxy`` with ``http_proxy`` / ``https_proxy`` /
    ``proxy_user`` / ``proxy_password`` routes transfers through a proxy;
    and ``auth_no_challenge`` controls origin Basic authentication.
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
        # The rate VALUE is parsed once; each fetch() gets its own fresh
        # RateLimiter built from it (spec §5.3: per-transfer pacing).
        self._rate_bps = parse_rate(
            variables.get("limit_rate", DEFAULT_CONFIG["limit_rate"])
        )
        self._auth_no_challenge = _config_on(variables.get("auth_no_challenge") or "0")
        self._tls_context = self._build_tls_context(variables)
        self._proxies = self._build_proxy_table(variables)

    @staticmethod
    def _build_tls_context(variables: dict[str, str]) -> ssl.SSLContext:
        """Build the TLS context for https URLs (spec §5.1).

        ``no_check_certificate`` disables verification entirely (documented
        as insecure); ``ca_certificate`` loads additional CAs on top of the
        default trust store (additive — it does not replace the defaults);
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
        Proxy-Authorization header).  The connection to the proxy itself
        is always plain HTTP; https targets are CONNECT-tunnelled through
        it, so the tunneled payload still travels as TLS to the origin
        (the proxy sees only the encrypted stream).  Userinfo embedded in
        the proxy URL wins over the separate proxy_user / proxy_password
        variables.  An invalid proxy URL is an AptMirrorError that does
        not echo the URL (it may carry secrets).
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
            # The portable way to carry proxy credentials on the CONNECT
            # request: the set_tunnel() headers mapping.  The keyword is
            # the supported one on every released CPython (there is no
            # proxy_authorization parameter — passing one is a TypeError).
            conn.set_tunnel(
                host,
                port,
                None
                if proxy_auth is None
                else {"Proxy-Authorization": proxy_auth.decode("ascii")},
            )
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
        self,
        response: http.client.HTTPResponse,
        part: Path,
        mode: str,
        limiter: RateLimiter,
    ) -> int:
        """Stream the response into ``part``, pacing each chunk with the
        transfer's own RateLimiter (spec §5.3: the limit is per transfer).

        Returns the bytes written this call.  On a truncated body the HTTP
        exception propagates to the caller's retry logic while the partial
        data already written stays in ``part`` for the next resume attempt.
        """
        total = 0
        try:
            with open(part, mode) as handle:
                while chunk := response.read(_CHUNK_SIZE):
                    limiter.pace(len(chunk))
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
        existing non-empty ``dest`` also deletes any stale ``<dest>.part``
        first (a complete file must ride the IMS branch — resuming onto a
        stale partial could persist a corrupt hybrid); a ``<dest>.part``
        without a complete dest triggers ``Range: bytes=N-`` resume (206
        appends, 200 restarts).  A 206 whose Content-Range does not start
        at the partial's end, and a 416 (unsatisfiable Range, e.g. the
        server file shrank) both drop the part and retry the attempt
        cleanly without backoff, bounded by the attempt cap.  On success
        the part is ``os.replace``d onto ``dest``.  Connection errors,
        timeouts, 429, and 5xx raise FetchError after up to _MAX_ATTEMPTS
        attempts with exponential backoff; 404 raises FetchNotFound and
        any other 4xx raises FetchError immediately.
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
        # One fresh limiter per transfer (spec §5.3: the rate limit is per
        # transfer, matching per-wget-process semantics — N concurrent
        # transfers each run at the full rate, not an aggregate N shares).
        limiter = RateLimiter(self._rate_bps)
        attempt = 0
        reason = "no attempt made"
        clean_url = first.clean_url
        while True:
            attempt += 1
            conn: http.client.HTTPConnection | None = None
            no_backoff = False
            try:
                base_headers: dict[str, str] = {"User-Agent": "apt-mirror (python3)"}
                if dest.exists() and dest.stat().st_size:
                    # A complete (non-empty) destination outranks any stale
                    # <dest>.part left by an interrupted re-download: the
                    # part is deleted and the server decides via
                    # If-Modified-Since.  Resuming by Range applies to a
                    # *partial* local file only (spec §5.3) — resuming onto
                    # a changed file could append a new tail to an old
                    # prefix and persist the corrupt hybrid.
                    part.unlink(missing_ok=True)
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
                    written = self._write_body(response, part, "wb", limiter)
                    self._require_complete(written, expected)
                    os.replace(part, dest)
                    return written
                if status == 206:
                    expected = response.length
                    start = self._range_start(response)
                    if start is not None and start != part_size:
                        # The server's range does not start where our
                        # partial ends: appending would corrupt the file.
                        # Discard the partial and retry cleanly — a full
                        # GET without Range, no backoff (the 416 /
                        # mismatch self-heal), bounded by the attempt cap.
                        response.close()
                        part.unlink(missing_ok=True)
                        reason = "HTTP 206 with a mismatched Content-Range"
                        no_backoff = True
                        continue
                    written = self._write_body(response, part, "ab", limiter)
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
                if status == 416:
                    # Our Range was unsatisfiable (e.g. the server file
                    # shrank): the stale partial is hopeless — drop it and
                    # retry the attempt cleanly without backoff (no Range
                    # next: the part is gone).  Bounded by the cap.
                    response.close()
                    part.unlink(missing_ok=True)
                    reason = "HTTP 416 (requested range unavailable)"
                    no_backoff = True
                    continue
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
            if no_backoff:
                logger.warning(
                    "%s: %s (attempt %d of %d); retrying cleanly",
                    clean_url,
                    reason,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                continue
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
