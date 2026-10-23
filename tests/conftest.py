"""Shared fixtures for the apt-mirror test suite.

The tool is a single file (``apt-mirror.py``) whose name is not a valid
identifier, so it is loaded from its path rather than imported normally.
``FixtureServer`` is the shared in-process HTTP server that the network
tests in the suite talk to (127.0.0.1 only).
"""

import base64
import contextlib
import email.utils
import gzip
import hashlib
import importlib.util
import posixpath
import re
import socket
import ssl
import subprocess
import sys
import threading
from datetime import UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Self, cast

import pytest


def load_aptmirror() -> ModuleType:
    """Load ``apt-mirror.py`` from the repository root as a module."""
    spec = importlib.util.spec_from_file_location(
        "aptmirror", Path(__file__).parent.parent / "apt-mirror.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def am() -> ModuleType:
    """The loaded apt-mirror module, shared by all unit tests."""
    return load_aptmirror()


_RANGE_RE = re.compile(r"^bytes=(\d+)-$")


class _FixtureRequestHandler(BaseHTTPRequestHandler):
    """Serve one connection from the in-memory state of the owning server."""

    def log_message(self, fmt: str, *args: Any) -> None:
        """Keep the test output free of per-request access logs."""

    def do_GET(self) -> None:
        """Log the request, then serve it from the server's state."""
        server = cast(FixtureServer, self.server)
        headers = {name.lower(): value for name, value in self.headers.items()}
        entry: dict[str, object] = {
            "method": self.command,
            "path": self.path,
            "headers": headers,
        }
        with server.lock:
            server.request_log.append(entry)
        self._handle_get(server, self.path, headers, entry)

    def _reply(
        self,
        entry: dict[str, object],
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        """Send a complete response and record its status on ``entry``."""
        entry["status"] = status
        self.send_response(status)
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        if status == 304:
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle_get(
        self,
        server: "FixtureServer",
        path: str,
        headers: dict[str, str],
        entry: dict[str, object],
    ) -> None:
        """Run the fixture behaviours for one GET, in a fixed order.

        Order: authentication (401), flaky failure (configured code),
        redirect (3xx with Location), missing file (404), drop
        truncation, If-Modified-Since (304), Range (206), then the plain
        200.
        """
        if server.required_auth is not None:
            expected_user, expected_password = server.required_auth
            expected = "Basic " + base64.b64encode(
                f"{expected_user}:{expected_password}".encode()
            ).decode("ascii")
            if headers.get("authorization") != expected:
                self._reply(
                    entry,
                    401,
                    body=b"authentication required",
                    headers={"WWW-Authenticate": 'Basic realm="fixture"'},
                )
                return
        with server.lock:
            flaky = server._flaky.get(path)
            if flaky is not None:
                remaining, code = flaky
                if remaining > 1:
                    server._flaky[path] = (remaining - 1, code)
                else:
                    del server._flaky[path]
            else:
                code = None
        if code is not None:
            self._reply(entry, code, body=b"fixture failure")
            return
        redirect = server._redirects.get(path)
        if redirect is not None:
            code, location = redirect
            extra = {"Location": location} if location is not None else None
            self._reply(entry, code, headers=extra)
            return
        file = server._files.get(path)
        if file is None:
            self._reply(entry, 404, body=b"not found")
            return
        content, last_modified = file
        modified = (
            email.utils.formatdate(timeval=last_modified, usegmt=True)
            if last_modified is not None
            else None
        )
        if path in server._drops:
            self._drop(entry, server, path, content, modified)
            return
        if modified is not None and self._not_modified(
            headers.get("if-modified-since"), last_modified
        ):
            self._reply(entry, 304, headers={"Last-Modified": modified})
            return
        extra: dict[str, str] = {"Last-Modified": modified} if modified else {}
        match = _RANGE_RE.match(headers.get("range") or "")
        if match:
            start = int(match.group(1))
            if start < len(content):
                range_end = len(content) - 1
                self._reply(
                    entry,
                    206,
                    body=content[start:],
                    headers={
                        "Content-Range": f"bytes {start}-{range_end}/{len(content)}",
                        **extra,
                    },
                )
                return
        self._reply(entry, 200, body=content, headers=extra)

    def _not_modified(
        self, if_modified_since: str | None, last_modified: float
    ) -> bool:
        """Return True when the If-Modified-Since value covers the file."""
        if if_modified_since is None:
            return False
        try:
            stamp = email.utils.parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError, IndexError):
            return False
        if stamp.tzinfo is None:  # a "-0000"/GMT stamp means UTC
            stamp = stamp.replace(tzinfo=UTC)
        return last_modified <= stamp.timestamp()

    def _drop(
        self,
        entry: dict[str, object],
        server: "FixtureServer",
        path: str,
        content: bytes,
        modified: str | None,
    ) -> None:
        """Send the configured number of bytes of ``content``, then hang up.

        Declares the full Content-Length and then closes the write side of
        the socket, so the client observes a truncated body (short read).
        Any Range request is ignored, like a dropping middle box.
        """
        n_bytes = server._drops[path]
        entry["status"] = 200
        self.send_response(200)
        if modified is not None:
            self.send_header("Last-Modified", modified)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content[:n_bytes])
        self.wfile.flush()
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_WR)


class FixtureServer(ThreadingHTTPServer):
    """A threaded HTTP server on 127.0.0.1 for offline fetch-layer tests.

    Serves an in-memory file table and records every request it receives —
    ``method``, ``path``, ``headers`` (lower-cased keys) and the ``status``
    answered — in ``request_log``.  Per-path behaviours are composable:
    ``require_auth`` (whole server), ``flaky``, ``redirect`` and
    ``drop_after``.  Use as
    a context manager for per-test isolation:

        with FixtureServer() as srv:
            srv.add("/a", b"data")
            ...  # fetch against srv.url("/a")  ...
    """

    daemon_threads = True

    def __init__(self, tls: ssl.SSLContext | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _FixtureRequestHandler)
        self._files: dict[str, tuple[bytes, float | None]] = {}
        self._flaky: dict[str, tuple[int, int]] = {}
        self._redirects: dict[str, tuple[int, str | None]] = {}
        self._drops: dict[str, int] = {}
        self.required_auth: tuple[str, str] | None = None
        self.request_log: list[dict[str, object]] = []
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._tls = tls

    def __enter__(self) -> Self:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.server_close()

    @property
    def port(self) -> int:
        """The ephemeral TCP port the server is listening on."""
        return self.server_address[1]

    @property
    def tls(self) -> ssl.SSLContext | None:
        """The server-side TLS context, or None when serving plain HTTP."""
        return self._tls

    def url(self, path: str) -> str:
        """The absolute URL (http or https, matching the TLS mode) serving
        ``path``."""
        scheme = "https" if self._tls is not None else "http"
        return f"{scheme}://127.0.0.1:{self.port}{path}"

    def get_request(self) -> tuple[ssl.SSLSocket | socket.socket, tuple]:
        """Accept and wrap one connection (server-side TLS when configured).

        A handshake that fails (e.g. a client that does not trust this
        certificate) raises TimeoutError so serve_forever's accept loop
        swallows it and keeps serving, instead of dying on the OSError.
        """
        conn, addr = self.socket.accept()
        if self._tls is not None:
            try:
                conn = self._tls.wrap_socket(conn, server_side=True)
            except ssl.SSLError:
                conn.close()
                raise TimeoutError from None
        return conn, addr

    def remove(self, path: str) -> None:
        """Stop serving ``path``; later requests to it are answered 404.

        Used to exercise server-side file deletion between runs (Task 9).
        """
        with self.lock:
            self._files.pop(path, None)
        self._drops.pop(path, None)

    def handle_error(self, request: object, client_address: tuple) -> None:
        """Keep per-connection server-side errors out of the test output.

        A client that aborts a TLS handshake or a download mid-stream
        legitimately resets the connection; the rig asserts on observed
        behaviour, and the default traceback dump would add noise.
        """

    def add(
        self, path: str, content: bytes, last_modified: float | None = None
    ) -> None:
        """Serve ``content`` at ``path``; re-adding resets any drop for it.

        ``last_modified`` is a Unix epoch; when set the server honours
        If-Modified-Since (304) and sends a Last-Modified header.
        """
        self._files[path] = (content, last_modified)
        self._drops.pop(path, None)

    def redirect(self, path: str, location: str | None, code: int = 302) -> None:
        """Answer requests to ``path`` with a 3xx Location for ``location``.

        ``location`` is sent verbatim (absolute or relative to the
        request); ``None`` means the 3xx carries no Location header at
        all and must not be followed.
        """
        self._redirects[path] = (code, location)

    def require_auth(self, username: str, password: str) -> None:
        """Reject every request whose Basic header does not match, with 401."""
        self.required_auth = (username, password)

    def flaky(self, path: str, fail_times: int, code: int = 500) -> None:
        """Make the first ``fail_times`` requests to ``path`` answer ``code``."""
        self._flaky[path] = (fail_times, code)

    def drop_after(self, path: str, n_bytes: int) -> None:
        """Truncate every response to ``path`` after ``n_bytes`` and hang up."""
        self._drops[path] = n_bytes

    def count(self, prefix: str) -> int:
        """Number of logged requests whose path starts with ``prefix``."""
        with self.lock:
            return sum(
                1 for entry in self.request_log if str(entry["path"]).startswith(prefix)
            )


# ---------------------------------------------------------------------------
# End-to-end fixture repo (Tasks 8-9, spec §6 test rig)
# ---------------------------------------------------------------------------

#: The repository root (the directory holding ``apt-mirror.py``); the cwd
#: every ``run_mirror`` subprocess is started in.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The fixed Last-Modified served for every fixture file by default.  It is a
#: date in the past, so an If-Modified-Since re-check whose local mtime is a
#: real (later) wall-clock download time sees the server copy as unmodified
#: and gets a 304 — the idempotency scenarios rely on exactly that.
_FIXTURE_LAST_MODIFIED = 1_750_000_000.0

_HASH_FUNCS = {
    "SHA512": hashlib.sha512,
    "SHA256": hashlib.sha256,
    "SHA1": hashlib.sha1,
    "MD5Sum": hashlib.md5,
}


def _write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path``, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fake_pool_bytes(label: str, size: int) -> bytes:
    """Deterministic pool-file bytes of exactly ``size`` bytes."""
    head = f"FAKEARCHIVE {label} {size}\n".encode("ascii")
    assert size > len(head)
    body = bytearray()
    counter = 0
    while len(body) < size - len(head):
        body += f"{counter:08d}\n".encode("ascii")
        counter += 1
    return head + bytes(body[: size - len(head)])


def _gzip_bytes(data: bytes) -> bytes:
    """Gzip ``data`` deterministically (mtime pinned to 0)."""
    return gzip.compress(data, mtime=0)


def _deb_block(name: str, version: str, arch: str, filename: str, data: bytes) -> str:
    """One Packages-index block for an archive ``data`` at ``filename``."""
    return (
        f"Package: {name}\n"
        f"Version: {version}\n"
        f"Architecture: {arch}\n"
        "Maintainer: Fixture <fixture@example.invalid>\n"
        f"Filename: {filename}\n"
        f"Size: {len(data)}\n"
        f"MD5sum: {hashlib.md5(data).hexdigest()}\n"
        f"SHA1: {hashlib.sha1(data).hexdigest()}\n"
        f"SHA256: {hashlib.sha256(data).hexdigest()}\n"
        "Description: fixture archive generated for the test rig\n"
    )


def _sources_block(
    package: str, version: str, directory: str, files: list[tuple[str, bytes]]
) -> str:
    """One Sources-index block; each ``File:`` line carries the file's real
    MD5, real size, and filename (relative to ``directory``)."""
    lines = [
        f"Package: {package}",
        f"Binary: {package}",
        f"Version: {version}",
        "Architecture: all",
        "Maintainer: Fixture <fixture@example.invalid>",
        f"Directory: {directory}",
        "Files:",
    ]
    for filename, data in files:
        lines.append(f" {hashlib.md5(data).hexdigest()} {len(data)} {filename}")
    return "\n".join(lines) + "\n"


def _release_text(
    suite: str,
    components: str,
    entries: dict[str, bytes],
    by_hash: bool,
    sha1_only: frozenset[str] | None = None,
) -> str:
    """A Release file listing every file of ``entries`` (dist-root-relative
    name -> the exact bytes served) under all four hash sections, each with
    the real digest and real size computed with hashlib.

    ``sha1_only`` names the files listed **only** under the ``SHA1:``
    section (omitted from SHA512/SHA256/MD5Sum): the weak-hash fixture for
    the spec §7.1 #2 regression (Task 9).  An empty ``components`` skips
    the Components header entirely (flat repositories).
    """
    lines = [
        "Origin: Fixture",
        "Label: Fixture",
        f"Suite: {suite}",
        f"Codename: {suite}",
        "Architectures: amd64 all",
    ]
    if components:
        lines.append(f"Components: {components}")
    lines.append("Description: fixture repository generated for the test rig")
    if by_hash:
        lines.append("Acquire-By-Hash: yes")
    for hashname, hash_func in _HASH_FUNCS.items():
        lines.append(f"{hashname}:")
        for filename in sorted(entries):
            if sha1_only is not None and filename in sha1_only and hashname != "SHA1":
                continue
            data = entries[filename]
            digest = hash_func(data).hexdigest()
            lines.append(f"  {digest} {len(data)} {filename}")
    return "\n".join(lines) + "\n"


def _inrelease_text(release: str) -> str:
    """An InRelease: the Release text plus a fake (never verified) signature."""
    return release + (
        "\n-----BEGIN PGP SIGNATURE-----\n"
        "iQFA fixture signature block; intentionally never verified by the\n"
        "tool, which only parses checksum sections out of this file.\n"
        "-----END PGP SIGNATURE-----\n"
    )


def _build_main_repo(
    root: Path, *, by_hash: bool, variant: int, weak_file: str | None = None
) -> None:
    """One dists-layout repo at ``<root>/repo``: distribution ``dist``,
    components main+contrib, arch amd64, Acquire-By-Hash when ``by_hash``,
    and a pool holding the referenced archives.

    ``weak_file`` (a dist-root-relative index name, with ``by_hash`` on)
    builds the spec §7.1 #2 regression fixture: the file is listed only
    under the Release's ``SHA1:`` section and its by-hash copy is
    published at ``<dir>/by-hash/sha1/<digest>`` instead of under sha512.
    """
    base = root / "repo"
    dist = base / "dists" / "dist"
    step = 1024  # each variant grows every .deb by this many bytes
    pools: dict[str, bytes] = {
        "pool/main/b/bar/bar_1.0-1_amd64.deb": _fake_pool_bytes(
            "bar_1.0-1_amd64.deb", 5000 + variant * step
        ),
        "pool/main/f/foo/foo_1.0-1_amd64.deb": _fake_pool_bytes(
            "foo_1.0-1_amd64.deb", 6000 + variant * step
        ),
        "pool/all/h/hello/hello_2.0-1_all.deb": _fake_pool_bytes(
            "hello_2.0-1_all.deb", 4000 + variant * step
        ),
        "pool/main/f/foo/foo_1.0-1.dsc": _fake_pool_bytes("foo_1.0-1.dsc", 700),
        "pool/main/f/foo/foo_1.0-1.diff.gz": _fake_pool_bytes(
            "foo_1.0-1.diff.gz", 1500
        ),
    }
    for rel, data in pools.items():
        _write(base / rel, data)
    bar = pools["pool/main/b/bar/bar_1.0-1_amd64.deb"]
    foo = pools["pool/main/f/foo/foo_1.0-1_amd64.deb"]
    hello = pools["pool/all/h/hello/hello_2.0-1_all.deb"]
    dsc = pools["pool/main/f/foo/foo_1.0-1.dsc"]
    diff = pools["pool/main/f/foo/foo_1.0-1.diff.gz"]
    indexes: dict[str, bytes] = {
        "main/binary-amd64/Packages.gz": _gzip_bytes(
            (
                _deb_block(
                    "bar", "1.0-1", "amd64", "pool/main/b/bar/bar_1.0-1_amd64.deb", bar
                )
                # Index blocks are blank-line separated.
                + "\n"
                + _deb_block(
                    "foo", "1.0-1", "amd64", "pool/main/f/foo/foo_1.0-1_amd64.deb", foo
                )
            ).encode("ascii")
        ),
        # Deliberately uncompressed (exercises suffix-less index handling).
        "main/binary-all/Packages": _deb_block(
            "hello", "2.0-1", "all", "pool/all/h/hello/hello_2.0-1_all.deb", hello
        ).encode("ascii"),
        "main/Contents-amd64.gz": _gzip_bytes(b"/usr/bin/bar bar\n/usr/bin/foo foo\n"),
        "main/i18n/Translation-en.gz": _gzip_bytes(
            b"Package: foo\nVersion: 1.0-1\nDescription-en: fixture package\n"
        ),
        "main/source/Sources.gz": _gzip_bytes(
            _sources_block(
                "foo",
                "1.0-1",
                "pool/main/f/foo",
                [("foo_1.0-1.dsc", dsc), ("foo_1.0-1.diff.gz", diff)],
            ).encode("ascii")
        ),
    }
    weak_set = frozenset({weak_file}) if weak_file is not None else None
    algo_fns = {"sha1": hashlib.sha1, "sha512": hashlib.sha512}
    for rel, data in indexes.items():
        _write(dist / rel, data)
        if by_hash:
            # Exactly the path IndexUrl.by_hash_path fetches: the strongest
            # hash the file *itself* advertises in the Release.  Every file
            # but the optional ``weak_file`` lists all four sections, so
            # that strongest is sha512; the weak file lists only SHA1, so
            # its by-hash copy lives under sha1/ (spec §7.1 #2 fixture).
            weak = weak_file is not None and rel == weak_file
            algo = "sha1" if weak else "sha512"
            hexdigest = algo_fns[algo](data).hexdigest()
            _write(dist / posixpath.dirname(rel) / "by-hash" / algo / hexdigest, data)
    release = _release_text("dist", "main contrib", indexes, by_hash, weak_set)
    _write(dist / "Release", release.encode("ascii"))
    _write(dist / "InRelease", _inrelease_text(release).encode("ascii"))
    # There is no Release.gpg: its fetches 404 and must be tolerated.


def _build_insecure_repo(root: Path, *, variant: int) -> None:
    """A Release-only (unsigned, never Acquire-By-Hash) repo at
    ``<root>/insecure``: distribution ``insecure``, one tiny Packages."""
    base = root / "insecure"
    dist = base / "dists" / "insecure"
    img = _fake_pool_bytes("img_1.0-1_amd64.deb", 4500 + variant * 1024)
    _write(base / "pool" / "main" / "i" / "img" / "img_1.0-1_amd64.deb", img)
    packages = _deb_block(
        "img", "1.0-1", "amd64", "pool/main/i/img/img_1.0-1_amd64.deb", img
    ).encode("ascii")
    _write(dist / "main" / "binary-amd64" / "Packages", packages)
    _write(
        dist / "Release",
        _release_text(
            "insecure", "main", {"main/binary-amd64/Packages": packages}, False
        ).encode("ascii"),
    )
    # No InRelease and no Release.gpg: the stage-1 InRelease fetch 404s and
    # the stage-2 release lookup must fall back to this Release.


def _build_nosrc_repo(root: Path) -> None:
    """A source-only, unsigned repo at ``<root>/nosrc``: it carries only raw
    ``dists/nosrc/main/source/Sources*`` — no Release at all, exercising the
    well-known unsigned-source fallback."""
    base = root / "nosrc"
    dsc = _fake_pool_bytes("nosrc_1.0-1.dsc", 650)
    _write(base / "pool" / "main" / "n" / "nosrc" / "nosrc_1.0-1.dsc", dsc)
    sources = _sources_block(
        "nosrc", "1.0-1", "pool/main/n/nosrc", [("nosrc_1.0-1.dsc", dsc)]
    ).encode("ascii")
    _write(
        base / "dists" / "nosrc" / "main" / "source" / "Sources.gz",
        _gzip_bytes(sources),
    )
    # Nothing else: no Release/InRelease/Release.gpg, no binary indexes.


def build_flat_repo(root: Path, *, dist: str, release: bool, variant: int = 0) -> None:
    """A flat repository (no ``dists/`` segment) at ``<root>/flat``
    (spec §5.1: a deb line with no components).

    Layout: ``<dist>/Packages`` (uncompressed, one .deb) and the pool file
    at the repo root — so index URLs are ``<uri>/<dist>/...`` and archive
    URLs ``<uri>/pool/...``.  No InRelease and no Release.gpg ever;
    ``release`` additionally writes a plain (unsigned, non-by-hash)
    ``<dist>/Release`` with the real digest/size of Packages.
    """
    base = root / "flat"
    pool_rel = "pool/f/fpkg/fpkg_1.0-1_amd64.deb"
    pkg = _fake_pool_bytes("fpkg_1.0-1_amd64.deb", 3500 + variant * 1024)
    _write(base / pool_rel, pkg)
    packages = _deb_block("fpkg", "1.0-1", "amd64", pool_rel, pkg).encode("ascii")
    _write(base / dist / "Packages", packages)
    if release:
        _write(
            base / dist / "Release",
            _release_text(dist, "", {"Packages": packages}, False).encode("ascii"),
        )


class FixtureRepoUrls(dict):
    """The role -> repo base URL map returned by build_fixture_repo.

    Besides mapping roles (``"main"``, ``"insecure"``, ``"nosrc"``) to their
    ``http://127.0.0.1:<port>/<subpath>`` base URLs it carries the
    ``server`` (the FixtureServer serving the repos, for request counting
    and in-place rebuilds) and ``root`` (the on-disk tree, for reading the
    exact bytes that are on the wire).
    """

    def __init__(self) -> None:
        super().__init__()
        self.root: Path | None = None
        self.server: FixtureServer | None = None


def serve_subtree(
    server: FixtureServer,
    root: Path,
    sub: str,
    last_modified: float | None = None,
) -> None:
    """Add every file under ``root / sub`` to ``server`` (paths keyed
    relative to ``root``) with ``last_modified`` pinned so
    If-Modified-Since re-checks are deterministic."""
    stamp = _FIXTURE_LAST_MODIFIED if last_modified is None else last_modified
    for file_path in sorted((root / sub).rglob("*")):
        if file_path.is_file():
            rel = file_path.relative_to(root).as_posix()
            server.add(f"/{rel}", file_path.read_bytes(), last_modified=stamp)


def build_fixture_repo(
    root: Path,
    *,
    by_hash: bool = True,
    unsigned_src_repo: bool = True,
    insecure_repo: bool = True,
    flat_repo: bool = False,
    weak_hash_file: str | None = None,
    variant: int = 0,
    last_modified: float | None = None,
    server: FixtureServer | None = None,
) -> FixtureRepoUrls:
    """Generate synthetic Debian-style apt repos under ``root`` and serve
    them from a FixtureServer on 127.0.0.1.

    Layout (every generated index/Release lists the real sizes and real
    digests of the generated bytes):

    - ``main``    — ``<root>/repo``, dists layout, distribution ``dist``,
      components main+contrib, arch amd64; Release with all four hash
      sections and ``Acquire-By-Hash: yes`` when ``by_hash``, InRelease =
      Release plus a fake signature, no Release.gpg; indexes
      ``main/binary-amd64/Packages.gz`` (2 .debs),
      ``main/binary-all/Packages`` (uncompressed, 1 all-arch .deb),
      ``main/Contents-amd64.gz``, ``main/i18n/Translation-en.gz`` and
      ``main/source/Sources.gz`` (1 source, 2 ``File:`` entries -> .dsc +
      .diff.gz), with by-hash copies wherever ``by_hash`` is on;
      ``weak_hash_file`` (a ``main``-repo index name) instead lists that
      one file under the Release's ``SHA1:`` section only and publishes
      its by-hash copy under ``sha1/`` (spec §7.1 #2 regression fixture,
      Task 9);
    - ``insecure`` — ``<root>/insecure`` (only when ``insecure_repo``):
      Release only (no InRelease) with one tiny Packages;
    - ``nosrc``   — ``<root>/nosrc`` (only when ``unsigned_src_repo``):
      only raw ``dists/nosrc/main/source/Sources.gz`` plus its pool file;
    - ``flat``    — ``<root>/flat`` (only when ``flat_repo``): a flat
      repository — ``flat/flat/Packages`` (uncompressed) plus its pool
      file, no Release of any kind (see build_flat_repo).

    Every file is added to ``server`` (or to a freshly started server kept
    in ``FixtureRepoUrls.server``) with ``last_modified`` pinned to
    ``last_modified`` (default: a fixed past date — see
    _FIXTURE_LAST_MODIFIED) so If-Modified-Since behaviour is
    deterministic.  ``variant`` > 0 regrows the .deb files (same paths,
    bigger content) for simulating a server-side update; ``server``
    re-adds every generated file onto an existing server (same port) —
    combined with a fresh, later ``last_modified`` that forces
    re-fetches.  Returns the role -> base URL map, e.g.
    ``{"main": "http://127.0.0.1:<port>/repo", ...}``.
    """
    root.mkdir(parents=True, exist_ok=True)
    roles: dict[str, str] = {"main": "repo"}
    _build_main_repo(root, by_hash=by_hash, variant=variant, weak_file=weak_hash_file)
    if insecure_repo:
        _build_insecure_repo(root, variant=variant)
        roles["insecure"] = "insecure"
    if unsigned_src_repo:
        _build_nosrc_repo(root)
        roles["nosrc"] = "nosrc"
    if flat_repo:
        build_flat_repo(root, dist="flat", release=False, variant=variant)
        roles["flat"] = "flat"
    if server is None:
        server = FixtureServer()
        # Outlive this function: the test closes it via FixtureRepoUrls.
        server.__enter__()
    for sub in roles.values():
        serve_subtree(server, root, sub, last_modified)
    urls = FixtureRepoUrls()
    urls.root = root
    urls.server = server
    for role, sub in roles.items():
        urls[role] = f"http://127.0.0.1:{server.port}/{sub}"
    return urls


@pytest.fixture
def fixture_repo(tmp_path: Path) -> FixtureRepoUrls:
    """A build_fixture_repo with all defaults, its server closed after the test."""
    urls = build_fixture_repo(tmp_path / "fixture")
    try:
        yield urls
    finally:
        assert urls.server is not None
        urls.server.__exit__(None, None, None)


#: The deb/deb-src line (distribution, components) written by
#: ``write_config`` for each fixture role; an unknown role gets a bare
#: ``deb <uri> <role> main``.
_ROLE_DEB_LINES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "main": (("deb", "dist", "main contrib"), ("deb-src", "dist", "main contrib")),
    "insecure": (("deb", "insecure", "main"),),
    "nosrc": (("deb-src", "nosrc", "main"),),
    # The flat role writes a component-less deb line (flat repository,
    # spec §5.1): an empty components string suppresses the field.
    "flat": (("deb", "flat", ""),),
}


def write_config(
    base: Path,
    urls: dict[str, str],
    *,
    autoclean: str = "0",
    extra: list[str] | None = None,
) -> Path:
    """Write ``base/mirror.list`` mirroring the fixture roles in ``urls``.

    The config pins ``base_path -> base``, ``nthreads 2``, ``limit_rate
    100m`` and ``run_postmirror 0``; writes one deb (and, for the main
    role, also a deb-src) line per role plus a ``clean`` line per base URL;
    and installs an executable ``postmirror.sh`` under ``base/var`` that
    records its working directory in ``base/var/pwd.txt`` (the Task 9
    postmirror check; harmless while run_postmirror is off).  ``extra``
    config lines are appended last so they may override earlier values.
    Returns the config path.
    """
    base.mkdir(parents=True, exist_ok=True)
    (base / "var").mkdir(parents=True, exist_ok=True)
    lines = [
        "# generated by write_config (test rig)",
        f"set base_path {base}",
        "set defaultarch amd64",
        "set nthreads 2",
        "set limit_rate 100m",
        "set run_postmirror 0",
        f"set _autoclean {autoclean}",
    ]
    for role, url in urls.items():
        for kind, dist, comps in _ROLE_DEB_LINES.get(role, (("deb", role, "main"),)):
            # A flat line carries no components (spec §5.1).
            if comps:
                lines.append(f"{kind} {url} {dist} {comps}")
            else:
                lines.append(f"{kind} {url} {dist}")
        lines.append(f"clean {url}")
    if extra:
        lines.extend(extra)
    config = base / "mirror.list"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script = base / "var" / "postmirror.sh"
    script.write_text(
        f"#!/bin/sh\npwd > {base / 'var' / 'pwd.txt'}\n", encoding="utf-8"
    )
    script.chmod(0o755)
    return config


def run_mirror(config: Path) -> subprocess.CompletedProcess:
    """Run the real tool as a subprocess against ``config`` and wait.

    Uses the current interpreter and the repository root as the working
    directory, so the bare ``apt-mirror.py`` path resolves; raises
    subprocess.TimeoutExpired after 120 s.
    """
    return subprocess.run(
        [sys.executable, "apt-mirror.py", str(config)],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )


#: The fixture Basic-auth credentials the authed_server fixture pins
#: (Task 9.9, spec §5.3: credentials must never leak).
AUTH_USER = "u"
AUTH_PASSWORD = "s3cr3t"


@pytest.fixture
def https_server() -> FixtureServer:
    """A TLS FixtureServer (server-side certificate from a trustme CA that
    covers 127.0.0.1 but is not in the system trust store).

    The tool's default (verifying) TLS context therefore refuses it, which
    exercises the spec §5.3 TLS-failure path; ``no_check_certificate 1``
    accepts it.  ``build_flat_repo`` + ``serve_subtree`` supply the
    repository served over the TLS socket.
    """
    import trustme

    ca = trustme.CA()
    server_cert = ca.issue_cert("127.0.0.1", "localhost")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_cert.configure_cert(context)
    server = FixtureServer(tls=context)
    server.__enter__()
    try:
        yield server
    finally:
        server.__exit__(None, None, None)


@pytest.fixture
def authed_server() -> FixtureServer:
    """A FixtureServer that answers 401 to every request unless it carries
    the fixture Basic credentials (``require_auth``, Task 3) — the
    credential-leak scenario server (Task 9.9)."""
    server = FixtureServer()
    server.__enter__()
    server.require_auth(AUTH_USER, AUTH_PASSWORD)
    try:
        yield server
    finally:
        server.__exit__(None, None, None)
