"""Shared fixtures for the apt-mirror test suite.

The tool is a single file (``apt-mirror.py``) whose name is not a valid
identifier, so it is loaded from its path rather than imported normally.
``FixtureServer`` is the shared in-process HTTP server that the network
tests in the suite talk to (127.0.0.1 only).
"""

import base64
import contextlib
import email.utils
import importlib.util
import re
import socket
import threading
from datetime import UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, cast

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
        missing file (404), drop truncation, If-Modified-Since (304),
        Range (206), then the plain 200.
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
    ``require_auth`` (whole server), ``flaky`` and ``drop_after``.  Use as
    a context manager for per-test isolation:

        with FixtureServer() as srv:
            srv.add("/a", b"data")
            ...  # fetch against srv.url("/a")  ...
    """

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FixtureRequestHandler)
        self._files: dict[str, tuple[bytes, float | None]] = {}
        self._flaky: dict[str, tuple[int, int]] = {}
        self._drops: dict[str, int] = {}
        self.required_auth: tuple[str, str] | None = None
        self.request_log: list[dict[str, object]] = []
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FixtureServer":
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

    def url(self, path: str) -> str:
        """The absolute http://127.0.0.1 URL that serves ``path``."""
        return f"http://127.0.0.1:{self.port}{path}"

    def add(
        self, path: str, content: bytes, last_modified: float | None = None
    ) -> None:
        """Serve ``content`` at ``path``; re-adding resets any drop for it.

        ``last_modified`` is a Unix epoch; when set the server honours
        If-Modified-Since (304) and sends a Last-Modified header.
        """
        self._files[path] = (content, last_modified)
        self._drops.pop(path, None)

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
