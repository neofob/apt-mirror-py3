"""Tests for the HTTP fetch layer and the FixtureServer (Task 3).

Every network test talks to the in-process FixtureServer
(tests/conftest.py) bound to 127.0.0.1 — the external network is never
touched.
"""

import base64
import logging
import time

import pytest

from .conftest import FixtureServer


def _basic(user: str, password: str) -> str:
    """The Authorization header value for ``user:password`` Basic auth."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")


class TestPlainDownload:
    """Plain download: bytes returned, file content, no .part left."""

    def test_plain_download(self, am, tmp_path) -> None:
        with FixtureServer() as srv:
            srv.add("/a.txt", b"hello world")
            fetcher = am.Fetcher({"limit_rate": "100m"})
            written = fetcher.fetch(srv.url("/a.txt"), tmp_path / "a.txt")
            assert written == 11
            assert (tmp_path / "a.txt").read_bytes() == b"hello world"
            assert not (tmp_path / "a.txt.part").exists()


class TestAtomicityAndResume:
    """A truncated download fails without a dest and resumes from .part."""

    def test_truncation_fails_atomically_then_resumes(self, am, tmp_path) -> None:
        content = bytes((i * 7) % 256 for i in range(200))
        with FixtureServer() as srv:
            srv.add("/big.bin", content)
            srv.drop_after("/big.bin", 50)
            fetcher = am.Fetcher({"limit_rate": "100m"}, backoff_base=0.01)
            dest = tmp_path / "big.bin"
            part = tmp_path / "big.bin.part"
            with pytest.raises(am.FetchError):
                fetcher.fetch(srv.url("/big.bin"), dest)
            # Five dropped attempts: no dest, but a 50-byte partial.
            assert not dest.exists()
            assert part.read_bytes() == content[:50]
            assert srv.count("/big.bin") == 5

            # Reset the drop behaviour; the next fetch resumes from byte 50.
            srv.add("/big.bin", content)
            written = fetcher.fetch(srv.url("/big.bin"), dest)
            assert written == 150  # only the portion appended on this call
            assert dest.read_bytes() == content
            assert not part.exists()
            resumed = [
                entry
                for entry in srv.request_log
                if entry["path"] == "/big.bin" and entry["status"] == 206
            ]
            assert len(resumed) == 1
            assert resumed[0]["headers"]["range"] == "bytes=50-"


class TestChangeDetection:
    """If-Modified-Since goes out for an existing dest; 304 writes nothing."""

    def test_unmodified_file_returns_zero(self, am, tmp_path) -> None:
        content = b"stable content"
        with FixtureServer() as srv:
            srv.add("/m.txt", content, last_modified=1000.0)
            fetcher = am.Fetcher({"limit_rate": "100m"})
            dest = tmp_path / "m.txt"
            assert fetcher.fetch(srv.url("/m.txt"), dest) == len(content)
            mtime = dest.stat().st_mtime
            written = fetcher.fetch(srv.url("/m.txt"), dest)
            assert written == 0
            assert dest.stat().st_mtime == mtime
            requests = [e for e in srv.request_log if e["path"] == "/m.txt"]
            assert "if-modified-since" not in requests[0]["headers"]
            assert requests[1]["status"] == 304
            assert "if-modified-since" in requests[1]["headers"]


class TestRetries:
    """5xx retries with backoff until success; 404 never retries."""

    def test_flaky_500_retries_then_succeeds(self, am, tmp_path) -> None:
        with FixtureServer() as srv:
            srv.add("/x", b"okay")
            srv.flaky("/x", 2, 500)
            fetcher = am.Fetcher({"limit_rate": "100m"}, backoff_base=0.01)
            written = fetcher.fetch(srv.url("/x"), tmp_path / "x")
            assert written == 4
            assert (tmp_path / "x").read_bytes() == b"okay"
            assert srv.count("/x") == 3

    def test_404_raises_fetch_not_found_immediately(self, am, tmp_path) -> None:
        with FixtureServer() as srv:
            fetcher = am.Fetcher({"limit_rate": "100m"}, backoff_base=0.01)
            with pytest.raises(am.FetchNotFound) as excinfo:
                fetcher.fetch(srv.url("/missing"), tmp_path / "missing")
            assert "/missing" in excinfo.value.url
            assert srv.count("/missing") == 1  # no retry on 404
            assert not (tmp_path / "missing").exists()
            assert not (tmp_path / "missing.part").exists()


class TestSchemeGuard:
    """Anything but http/https is rejected before any connection."""

    def test_ftp_scheme_rejected(self, am, tmp_path) -> None:
        fetcher = am.Fetcher({"limit_rate": "100m"})
        with pytest.raises(am.AptMirrorError) as excinfo:
            fetcher.fetch("ftp://example.com/x", tmp_path / "x")
        message = str(excinfo.value)
        assert "ftp" in message
        assert "example.com" in message
        assert not (tmp_path / "x").exists()


class TestCredentials:
    """Origin userinfo becomes a Basic header and never leaks."""

    def test_auth_no_challenge_sends_header_upfront(self, am, tmp_path) -> None:
        payload = b"top secret"
        with FixtureServer() as srv:
            srv.add("/sec.txt", payload)
            srv.require_auth("u", "s3cr3t")
            url = srv.url("/sec.txt").replace("://", "://u:s3cr3t@", 1)
            fetcher = am.Fetcher(
                {"limit_rate": "100m", "auth_no_challenge": "1"},
                backoff_base=0.01,
            )
            written = fetcher.fetch(url, tmp_path / "sec.txt")
            assert written == len(payload)
            assert (tmp_path / "sec.txt").read_bytes() == payload
            assert srv.count("/sec.txt") == 1
            assert srv.request_log[0]["headers"]["authorization"] == (
                _basic("u", "s3cr3t")
            )

    def test_401_challenge_then_auth_retry_never_leaks(
        self, am, tmp_path, caplog
    ) -> None:
        payload = b"top secret"
        with FixtureServer() as srv:
            srv.add("/sec.txt", payload)
            srv.require_auth("u", "s3cr3t")
            url = srv.url("/sec.txt").replace("://", "://u:wrongpass@", 1)
            fetcher = am.Fetcher({"limit_rate": "100m"}, backoff_base=0.01)
            with (
                caplog.at_level(logging.DEBUG, logger="apt-mirror"),
                pytest.raises(am.FetchError) as excinfo,
            ):
                fetcher.fetch(url, tmp_path / "sec.txt")
            got = [e for e in srv.request_log if e["path"] == "/sec.txt"]
            assert len(got) == 2
            assert "authorization" not in got[0]["headers"]
            assert got[1]["headers"]["authorization"] == _basic("u", "wrongpass")
            # Credentials must never appear in any public string.
            assert "wrongpass" not in excinfo.value.url
            assert "wrongpass" not in str(excinfo.value)
            assert all("wrongpass" not in r.message for r in caplog.records)


class TestRedirects:
    """Location redirects (301/302/303/307/308) are followed, bounded."""

    def test_redirect_chain_to_second_server(self, am, tmp_path) -> None:
        with FixtureServer() as srv_a, FixtureServer() as srv_b:
            srv_b.add("/real", b"redirected")
            srv_a.redirect("/r", srv_b.url("/real"))
            fetcher = am.Fetcher({"limit_rate": "100m"}, backoff_base=0.01)
            dest = tmp_path / "real"
            written = fetcher.fetch(srv_a.url("/r"), dest)
            assert written == len(b"redirected")
            assert dest.read_bytes() == b"redirected"
            assert srv_a.count("/r") == 1
            assert srv_a.request_log[0]["status"] == 302
            assert srv_b.count("/real") == 1
            assert srv_b.request_log[0]["method"] == "GET"

    def test_cross_origin_redirect_drops_authorization(self, am, tmp_path) -> None:
        with FixtureServer() as srv_a, FixtureServer() as srv_b:
            srv_b.add("/real", b"open target")
            srv_a.redirect("/r", srv_b.url("/real"))
            url = srv_a.url("/r").replace("://", "://u:s3cr3t@", 1)
            fetcher = am.Fetcher(
                {"limit_rate": "100m", "auth_no_challenge": "1"},
                backoff_base=0.01,
            )
            assert fetcher.fetch(url, tmp_path / "real") == len(b"open target")
            assert (tmp_path / "real").read_bytes() == b"open target"
            assert srv_a.request_log[0]["headers"]["authorization"] == _basic(
                "u", "s3cr3t"
            )
            # Cross-origin: the credential must not travel to the other host.
            assert "authorization" not in srv_b.request_log[0]["headers"]

    def test_redirect_cap_stops_a_loop(self, am, tmp_path) -> None:
        with FixtureServer() as srv:
            srv.redirect("/loop", srv.url("/loop"))
            fetcher = am.Fetcher(
                {"limit_rate": "100m"}, backoff_base=0.01, max_redirects=2
            )
            with pytest.raises(am.FetchError) as excinfo:
                fetcher.fetch(srv.url("/loop"), tmp_path / "loop")
            assert "too many redirects" in excinfo.value.reason
            # 1 request + the 2 followed hops, then the cap stops the loop.
            assert srv.count("/loop") == 3

    def test_3xx_without_location_fails(self, am, tmp_path) -> None:
        with FixtureServer() as srv:
            srv.redirect("/dangling", None)
            fetcher = am.Fetcher({"limit_rate": "100m"}, backoff_base=0.01)
            with pytest.raises(am.FetchError) as excinfo:
                fetcher.fetch(srv.url("/dangling"), tmp_path / "dangling")
            assert "location" in excinfo.value.reason.lower()
            assert srv.count("/dangling") == 1


class TestRateLimiter:
    """pace() keeps a steady stream of calls at or below the rate."""

    def test_pacing_holds_average(self, am) -> None:
        limiter = am.RateLimiter(100.0)
        started = time.monotonic()
        limiter.pace(100 * 3)
        limiter.pace(100 * 3)
        limiter.pace(100 * 3)
        # 900 bytes at 100 B/s is 9 s; the brief's bound is loose on purpose.
        assert time.monotonic() - started >= 2.5

    def test_high_rate_costs_almost_nothing(self, am) -> None:
        limiter = am.RateLimiter(1e9)
        started = time.monotonic()
        limiter.pace(10**6)
        assert time.monotonic() - started < 0.2


class TestFetcherConfig:
    """Fetcher validates limit_rate at construction."""

    def test_bad_limit_rate_raises(self, am) -> None:
        with pytest.raises(am.AptMirrorError) as excinfo:
            am.Fetcher({"limit_rate": "fast"})
        assert "fast" in str(excinfo.value)

    def test_missing_limit_rate_falls_back_to_default(self, am, tmp_path) -> None:
        with FixtureServer() as srv:
            srv.add("/d.txt", b"1234")
            fetcher = am.Fetcher({})
            assert fetcher.fetch(srv.url("/d.txt"), tmp_path / "d.txt") == 4
