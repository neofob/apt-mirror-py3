"""Tests for the threaded stage download orchestration (Task 4).

Every network test talks to the in-process FixtureServer bound to
127.0.0.1 (tests/conftest.py) — the external network is never touched.
"""

import time

from .conftest import FixtureServer


def _variables(am, nthreads: str = "4") -> dict[str, str]:
    """The Task 1 default table with ``nthreads`` overridden.

    ``am`` is the session fixture holding the loaded apt-mirror module
    (its file name is not an identifier, so it is loaded by path).
    """
    return {**am.DEFAULT_CONFIG, "nthreads": nthreads}


def _url_files(
    srv: FixtureServer, count: int
) -> tuple[list[str], int, dict[str, bytes]]:
    """Add ``count`` files of a distinct, growing size to the server.

    Returns the URL list, the total byte count, and a path -> content map.
    """
    urls: list[str] = []
    total = 0
    contents: dict[str, bytes] = {}
    for index in range(count):
        path = f"/pool/file{index}.deb"
        content = f"payload-{index}".encode() * (index + 1)
        srv.add(path, content)
        urls.append(srv.url(path))
        total += len(content)
        contents[path] = content
    return urls, total, contents


class TestAllSucceed:
    """A clean stage: ok/bytes/files/urls-list/log-list all line up."""

    def test_all_succeed(self, am, tmp_path) -> None:
        variables = _variables(am)
        with FixtureServer() as srv:
            port = srv.port
            urls, total, contents = _url_files(srv, 10)
            fetcher = am.Fetcher(variables, backoff_base=0.001)
            result = am.download_stage(
                "unit", urls, tmp_path / "root", fetcher, variables, tmp_path / "var"
            )
        assert result.ok == 10
        assert result.failed == []
        assert result.bytes == total
        for path, content in contents.items():
            dest = tmp_path / "root" / f"127.0.0.1:{port}" / path.lstrip("/")
            assert dest.read_bytes() == content
        urls_file = (tmp_path / "var" / "unit-urls.txt").read_text()
        assert urls_file.splitlines() == sorted(urls)
        log_lines = (tmp_path / "var" / "unit-log.txt").read_text().splitlines()
        assert len(log_lines) == 10
        for line in log_lines:
            assert line.startswith("OK ")
            assert "FAIL" not in line


class TestPartials:
    """One 404 is a failed entry, not an exception for the whole stage."""

    def test_one_404(self, am, tmp_path) -> None:
        variables = _variables(am)
        missing = "/pool/missing.deb"
        with FixtureServer() as srv:
            port = srv.port
            urls, _total, contents = _url_files(srv, 10)
            # Replace the request for /pool/file7.deb with a path the
            # server does not have.
            del contents["/pool/file7.deb"]
            urls[7] = srv.url(missing)
            fetcher = am.Fetcher(variables, backoff_base=0.001)
            result = am.download_stage(
                "unit", urls, tmp_path / "root", fetcher, variables, tmp_path / "var"
            )
        assert result.ok == 9
        assert len(result.failed) == 1
        assert result.failed[0][0] == f"http://127.0.0.1:{port}{missing}"
        assert "404" in result.failed[0][1]
        log_lines = (tmp_path / "var" / "unit-log.txt").read_text().splitlines()
        fail_lines = [line for line in log_lines if line.startswith("FAIL ")]
        assert len(fail_lines) == 1
        assert fail_lines[0] == f"FAIL {srv.url(missing)} " + result.failed[0][1]
        # The nine good files still landed.
        for path, content in contents.items():
            dest = tmp_path / "root" / f"127.0.0.1:{port}" / path.lstrip("/")
            assert dest.read_bytes() == content


class TestThreadCap:
    """M = min(nthreads, #urls), including the >1-URL count."""

    def test_nthreads_exceeds_urls(self, am, tmp_path, capsys) -> None:
        variables = _variables(am, "20")
        with FixtureServer() as srv:
            srv.add("/single.deb", b"x")
            fetcher = am.Fetcher(variables, backoff_base=0.001)
            result = am.download_stage(
                "cap",
                [srv.url("/single.deb")],
                tmp_path / "root",
                fetcher,
                variables,
                tmp_path / "var",
            )
        assert result.ok == 1
        assert "Downloading 1 files using 1 threads..." in capsys.readouterr().out

    def test_nthreads_below_urls(self, am, tmp_path, capsys) -> None:
        variables = _variables(am, "3")
        with FixtureServer() as srv:
            urls, _total, _contents = _url_files(srv, 12)
            fetcher = am.Fetcher(variables, backoff_base=0.001)
            result = am.download_stage(
                "cap", urls, tmp_path / "root", fetcher, variables, tmp_path / "var"
            )
        assert result.ok == 12
        out = capsys.readouterr().out
        assert "Downloading 12 files using 3 threads..." in out
        assert "12 ok, 0 failed, " in out
        assert " bytes transferred" in out


class TestEmptyStage:
    """Zero URLs: empty result, nothing printed, nothing written."""

    def test_empty(self, am, tmp_path, capsys) -> None:
        variables = _variables(am)
        fetcher = am.Fetcher(variables, backoff_base=0.001)
        result = am.download_stage(
            "empty", [], tmp_path / "root", fetcher, variables, tmp_path / "var"
        )
        assert result.ok == 0
        assert result.failed == []
        assert result.bytes == 0
        out, err = capsys.readouterr()
        assert out == ""
        assert err == ""
        assert not (tmp_path / "var" / "empty-urls.txt").exists()
        assert not (tmp_path / "var" / "empty-log.txt").exists()


class TestCredentialLeak:
    """s3cr3t must never appear in stdout/stderr or the stage files."""

    def test_credentials_never_leak(self, am, tmp_path, capsys) -> None:
        variables = _variables(am)
        with FixtureServer() as srv:
            srv.require_auth("u", "s3cr3t")
            srv.add("/pool/a.deb", b"aaa")
            port = srv.port
            good = f"http://u:s3cr3t@127.0.0.1:{port}/pool/a.deb"
            bad = f"http://u:s3cr3t@127.0.0.1:{port}/pool/b.deb"  # 404 after auth
            fetcher = am.Fetcher(variables, backoff_base=0.001)
            result = am.download_stage(
                "stage",
                [good, bad],
                tmp_path / "root",
                fetcher,
                variables,
                tmp_path / "var",
            )
        assert result.ok == 1
        assert len(result.failed) == 1
        assert result.failed[0][0] == f"http://127.0.0.1:{port}/pool/b.deb"
        out, err = capsys.readouterr()
        assert "s3cr3t" not in out
        assert "s3cr3t" not in err
        for stage_file in ("stage-urls.txt", "stage-log.txt"):
            text = (tmp_path / "var" / stage_file).read_text()
            assert "s3cr3t" not in text
            assert "@" not in text  # no embedded userinfo anywhere
        urls_text = (tmp_path / "var" / "stage-urls.txt").read_text()
        assert urls_text.splitlines() == sorted(
            [
                f"http://127.0.0.1:{port}/pool/a.deb",
                f"http://127.0.0.1:{port}/pool/b.deb",
            ]
        )


class TestPerTransferPacing:
    """limit_rate paces each TRANSFER separately (spec §5.3: per transfer,
    matching per-wget-process semantics) — N concurrent transfers each run
    at the full rate, not the aggregate/N rate of one shared limiter."""

    def test_two_transfers_pace_independently(self, am, tmp_path) -> None:
        size = 16 * 1024  # 16 KiB -> 8.192 s of pacing at 2000 B/s each
        variables = {**am.DEFAULT_CONFIG, "nthreads": "2", "limit_rate": "2000"}
        with FixtureServer() as srv:
            for name in ("/two/a.deb", "/two/b.deb"):
                srv.add(name, bytes((i * 13) % 256 for i in range(size)))
            fetcher = am.Fetcher(variables, backoff_base=0.001)
            started = time.monotonic()
            result = am.download_stage(
                "pace",
                [srv.url("/two/a.deb"), srv.url("/two/b.deb")],
                tmp_path / "root",
                fetcher,
                variables,
                tmp_path / "var",
            )
            elapsed = time.monotonic() - started
        assert result.ok == 2
        # Margins (documented, like the TLS/timing pins):
        # - Lower bound 8.0 s: at least the single-largest transfer's
        #   8.192 s of pacing (minus 0.2 s slack) — the rate is honoured.
        # - Upper bound 15.0 s: well under the 16.384 s a shared
        #   aggregate limiter (or two sequential transfers) would take,
        #   and ~6.8 s above the ideal 8.192 s concurrent bound, so a
        #   loaded CI box has wide slack before this becomes flaky.
        assert elapsed >= 8.0, f"too fast: {elapsed:.2f}s"
        assert elapsed < 15.0, f"too slow: {elapsed:.2f}s"
        port = srv.port
        for name in ("/two/a.deb", "/two/b.deb"):
            dest = tmp_path / "root" / f"127.0.0.1:{port}" / name.lstrip("/")
            assert dest.stat().st_size == size
