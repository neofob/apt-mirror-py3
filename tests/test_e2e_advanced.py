"""End-to-end tests: cleanup, resilience, TLS, security and bug-fix pins.

The real tool (``apt-mirror.py``) is run as a subprocess against fixture
repos served on 127.0.0.1.  These are the "advanced" scenarios completing
the coverage laid down in spec §5.5-5.11 and §7.1:

1.  cleanup / ``_autoclean`` (and the generated clean script)
2.  404 tolerance (a deleted InRelease and Translation index)
3.  ``flock`` locking (a held lock blocks; a stale file does not)
4.  HTTPS over a trustme-TLS server (verifying vs ``no_check_certificate``)
5.  by-hash weak-hash regression (spec §7.1 #2)
6.  unsigned ``deb-src`` well-known fallback URL shape (spec §7.1 #1)
7.  symmetric config-validation errors (spec §7.1 #3)
8.  postmirror script working directory (spec §5.10)
9.  credentials never leak to logs/files (spec §5.3)
10. flat repositories mirror without a ``dists/`` segment (spec §5.1)
11. ``nthreads`` capped at the stage URL count
12. an invalid ``limit_rate`` is a clean config error
13. ``use_proxy 1`` tunnels https (CONNECT + Proxy-Authorization) and
    forwards plain http (absolute-form) through an authenticated local
    proxy (Review C2 — the ``set_tunnel`` TypeError regression)
14. ``deb [arch=a,b]`` mirrors every architecture's pools and indexes
    (Review C1 — the raw multi-arch interpolation regression)
15. a non-UTF-8 config file is a clean, named error (no traceback)
16. secondary by-hash paths are hardlinked for multi-hash-listed files
    (spec §5.6.3 hash-variant dedupe)

Every server is on 127.0.0.1 only: no external network.
"""

import base64
import fcntl
import hashlib
import os
import subprocess
from pathlib import Path

from .conftest import (
    AUTH_PASSWORD,
    AUTH_USER,
    FixtureServer,
    LocalProxy,
    build_fixture_repo,
    build_flat_repo,
    run_mirror,
    serve_subtree,
    write_config,
)


def _host(url: str) -> str:
    """The host[:port] component of a (credential-free) base URL."""
    return url.split("://", 1)[1].split("/", 1)[0]


def _files(tree: Path) -> set[str]:
    """The set of tree-relative POSIX paths of every file under ``tree``."""
    return {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}


def _close(urls: object) -> None:
    """Tear down a build_fixture_repo-created server (not a pytest fixture)."""
    server = getattr(urls, "server", None)
    if server is not None:
        server.__exit__(None, None, None)


def _tls_config(base: Path, base_url: str, no_check: str) -> Path:
    """A single flat-repo config mirroring ``base_url`` over TLS into
    ``base`` (the config carries no secrets, so it may live in ``base``)."""
    base.mkdir(parents=True, exist_ok=True)
    lines = [
        f"set base_path {base}",
        "set defaultarch amd64",
        # Enough workers that the (fast, local) retry waves of the
        # verification-failing run stay within the subprocess timeout.
        "set nthreads 16",
        "set limit_rate 100m",
        "set run_postmirror 0",
        "set _autoclean 0",
        f"set no_check_certificate {no_check}",
        f"deb {base_url} flat",
        f"clean {base_url}",
    ]
    config = base / "mirror.list"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config


def test_autoclean_frees_only_planted_content(tmp_path: Path) -> None:
    """Scenario 1: ``_autoclean 1`` deletes exactly the unneeded files
    (live content, including by-hash paths, intact) printing
    "will be freed"; ``_autoclean 0`` instead writes an executable
    clean.sh whose ``rm -f`` lines cover exactly the planted paths, and
    executing it with ``sh`` frees the same content (spec §5.9)."""
    urls = build_fixture_repo(
        tmp_path / "fixture", insecure_repo=False, unsigned_src_repo=False
    )
    try:
        base = tmp_path / "spool"
        mirror = base / "mirror"
        tree = mirror / _host(urls["main"]) / "repo"

        # Baseline first run: a complete mirror.
        config = write_config(base, {"main": urls["main"]}, autoclean="0")
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        live = _files(mirror)
        assert live  # the mirror tree was actually populated

        def plant() -> None:
            (tree / "junk.txt").write_text("junk\n", encoding="utf-8")
            (tree / "emptydir").mkdir()
            (tree / "emptydir" / "leaf.txt").write_text("leaf\n", encoding="utf-8")

        # Pass 1: _autoclean 1 frees in place; live content stays intact.
        plant()
        config = write_config(base, {"main": urls["main"]}, autoclean="1")
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "will be freed" in result.stdout
        assert not (tree / "junk.txt").exists()
        assert not (tree / "emptydir").exists()
        missing = live - _files(mirror)
        assert not missing, f"autoclean removed live files: {missing}"
        # The original chmod +x's the script in BOTH modes (parity): in
        # _autoclean mode it is left empty but still executable (it is
        # regenerated and unused on the next non-autoclean run).
        clean_sh = base / "var" / "clean.sh"
        assert clean_sh.is_file()
        assert os.access(clean_sh, os.X_OK)
        assert clean_sh.read_text(encoding="utf-8") == ""

        # Pass 2: _autoclean 0 writes an executable clean.sh instead.
        plant()
        config = write_config(base, {"main": urls["main"]}, autoclean="0")
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        clean_sh = base / "var" / "clean.sh"
        assert clean_sh.is_file()
        assert os.access(clean_sh, os.X_OK)
        rm_paths: set[str] = set()
        for line in clean_sh.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("rm -f "):
                rm_paths.add(line.removeprefix("rm -f ").strip().strip("'"))
        tree_rel = f"{_host(urls['main'])}/repo"
        assert rm_paths == {
            f"{tree_rel}/junk.txt",
            f"{tree_rel}/emptydir/leaf.txt",
        }, rm_paths
        assert not (rm_paths & live), "clean.sh would remove live content"

        # Execute the generated script: same deletion result, "done." out.
        sh_out = subprocess.run(
            ["sh", str(clean_sh)],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert sh_out.returncode == 0, sh_out.stdout + sh_out.stderr
        assert "done." in sh_out.stdout
        assert not (tree / "junk.txt").exists()
        assert not (tree / "emptydir").exists()
    finally:
        _close(urls)


def test_missing_inrelease_and_index_are_404_tolerant(tmp_path: Path) -> None:
    """Scenario 2: deleting the dist InRelease and one Translation index
    server-side leaves the run at exit 0 with warnings on stderr;
    everything else is mirrored in full (spec §5.5, §5.3: a 404 must not
    kill a mirror run)."""
    urls = build_fixture_repo(
        tmp_path / "fixture", insecure_repo=False, unsigned_src_repo=False
    )
    srv = urls.server
    assert srv is not None
    try:
        base = tmp_path / "spool"
        tree = base / "mirror" / _host(urls["main"]) / "repo"
        dist = tree / "dists" / "dist"

        # Server-side deletion before the run: the dist InRelease, plus one
        # Translation index in both its served spellings (canonical and the
        # by-hash copy the Acquire-By-Hash repo actually fetches).
        trans = (
            urls.root
            / "repo"
            / "dists"
            / "dist"
            / "main"
            / "i18n"
            / "Translation-en.gz"
        )
        digest = hashlib.sha512(trans.read_bytes()).hexdigest()
        srv.remove("/repo/dists/dist/InRelease")
        srv.remove("/repo/dists/dist/main/i18n/Translation-en.gz")
        srv.remove(f"/repo/dists/dist/main/i18n/by-hash/sha512/{digest}")

        config = write_config(base, {"main": urls["main"]})
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "HTTP 404" in result.stderr
        # The missing InRelease is a release-stage failure, recorded, not fatal.
        release_log = (base / "var" / "release-log.txt").read_text(encoding="utf-8")
        assert "/InRelease" in release_log
        assert "FAIL" in release_log

        # Everything else mirrors in full.
        assert (dist / "Release").is_file()
        assert (dist / "main" / "binary-amd64" / "Packages.gz").is_file()
        assert (dist / "main" / "binary-all" / "Packages").is_file()
        assert (dist / "main" / "Contents-amd64.gz").is_file()
        assert (dist / "main" / "source" / "Sources.gz").is_file()
        assert (dist / "Release.gpg").is_file() is False
        for sub in (
            "main/b/bar/bar_1.0-1_amd64.deb",
            "main/f/foo/foo_1.0-1_amd64.deb",
            "all/h/hello/hello_2.0-1_all.deb",
            "main/f/foo/foo_1.0-1.dsc",
            "main/f/foo/foo_1.0-1.diff.gz",
        ):
            assert (tree / "pool" / sub).is_file(), sub
        # The removed Translation index is the one tolerated gap.
        assert not (dist / "main" / "i18n" / "Translation-en.gz").exists()
    finally:
        _close(urls)


def test_flock_blocks_concurrent_run_but_not_stale_file(tmp_path: Path) -> None:
    """Scenario 3: an exclusively-held ``var/apt-mirror.lock`` makes the
    tool exit non-zero with the exact "already running" message; once the
    lock is released but the stale file left in place, the next run
    succeeds (spec §5.2)."""
    urls = build_fixture_repo(
        tmp_path / "fixture",
        by_hash=False,
        insecure_repo=False,
        unsigned_src_repo=False,
    )
    try:
        base = tmp_path / "spool"
        config = write_config(base, {"main": urls["main"]})
        lock_file = base / "var" / "apt-mirror.lock"
        fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # hold the lock in this process
            result = run_mirror(config)
            assert result.returncode != 0
            assert (
                "apt-mirror is already running, exiting" in result.stderr.splitlines()
            )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        # The stale lock file survives the failed run; a following run is
        # free to acquire it.
        assert lock_file.exists()
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        _close(urls)


def test_https_repo_verifying_and_unverified(
    https_server: FixtureServer, tmp_path: Path
) -> None:
    """Scenario 4: a fixture repo served over trustme TLS (CA not in the
    system store).  ``no_check_certificate 0``: the run still exits 0 but
    the repo's files are absent and a TLS failure is warned per URL;
    ``no_check_certificate 1``: the files mirror (spec §5.3, §5.1)."""
    root = tmp_path / "fixture"
    build_flat_repo(root, dist="flat", release=True, variant=0)
    serve_subtree(https_server, root, "flat")
    tls_base = https_server.url("/flat")
    assert tls_base.startswith("https://")
    host = _host(tls_base)
    pkg_bytes = (root / "flat" / "flat" / "Packages").read_bytes()
    deb_bytes = (
        root / "flat" / "pool" / "f" / "fpkg" / "fpkg_1.0-1_amd64.deb"
    ).read_bytes()

    # no_check_certificate 0: the untrusted certificate fails verification;
    # every per-URL TLS failure is warned and the run survives.
    base0 = tmp_path / "spool0"
    result = run_mirror(_tls_config(base0, tls_base, "0"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CERTIFICATE_VERIFY_FAILED" in result.stderr, result.stderr
    tree0 = base0 / "mirror" / host / "flat"
    assert not (tree0 / "flat" / "Packages").exists()
    assert not (tree0 / "flat" / "Release").exists()
    assert not (tree0 / "pool" / "f" / "fpkg" / "fpkg_1.0-1_amd64.deb").exists()

    # no_check_certificate 1: verification off, the exact fixture files land.
    base1 = tmp_path / "spool1"
    result = run_mirror(_tls_config(base1, tls_base, "1"))
    assert result.returncode == 0, result.stdout + result.stderr
    tree1 = base1 / "mirror" / host / "flat"
    assert (tree1 / "flat" / "Packages").read_bytes() == pkg_bytes
    assert (tree1 / "flat" / "Release").is_file()
    assert (
        tree1 / "pool" / "f" / "fpkg" / "fpkg_1.0-1_amd64.deb"
    ).read_bytes() == deb_bytes


def test_weak_hash_by_hash_file_is_not_skipped(tmp_path: Path) -> None:
    """Scenario 5 (spec §7.1 #2): in an Acquire-By-Hash repo a file listed
    only under the weaker ``SHA1:`` section is still downloaded (via its
    own strongest hash, sha1) and mirrored at its canonical path — the
    original recorded but never fetched such files."""
    urls = build_fixture_repo(
        tmp_path / "fixture",
        insecure_repo=False,
        unsigned_src_repo=False,
        weak_hash_file="main/Contents-amd64.gz",
    )
    try:
        # Fixture integrity: the Release lists the weak file only under
        # the SHA1: section (real digest/size, like every other entry).
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in (
            (urls.root / "repo" / "dists" / "dist" / "Release")
            .read_text(encoding="utf-8")
            .splitlines()
        ):
            if line in ("SHA512:", "SHA256:", "SHA1:", "MD5Sum:"):
                current = line[:-1]
            elif current is not None and line.startswith(" "):
                sections.setdefault(current, []).append(line.split()[2])
            else:
                current = None
        # The weak file appears under SHA1: (like every other file) and the
        # other three sections exclude it — i.e. it is listed *only* under
        # the weaker SHA1: section, with a real digest and size.
        assert "main/Contents-amd64.gz" in sections.get("SHA1", [])
        for name in ("SHA512", "SHA256", "MD5Sum"):
            assert "main/Contents-amd64.gz" not in sections.get(name, [])

        base = tmp_path / "spool"
        config = write_config(base, {"main": urls["main"]})
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        dist = base / "mirror" / _host(urls["main"]) / "repo" / "dists" / "dist"
        expected = (
            urls.root / "repo" / "dists" / "dist" / "main" / "Contents-amd64.gz"
        ).read_bytes()
        canonical = dist / "main" / "Contents-amd64.gz"
        assert canonical.is_file()
        assert canonical.read_bytes() == expected
        # The by-hash download lands under sha1/ and fans out to canonical.
        digest = hashlib.sha1(expected).hexdigest()
        by_hash = dist / "main" / "by-hash" / "sha1" / digest
        assert by_hash.is_file()
        assert by_hash.read_bytes() == expected
        # The rest of the repo keeps mirroring through its own (sha512) hash:
        # the same canonical file must not be the *only* by-hash survivor.
        pkgs = (
            urls.root
            / "repo"
            / "dists"
            / "dist"
            / "main"
            / "binary-amd64"
            / "Packages.gz"
        ).read_bytes()
        assert (
            dist
            / "main"
            / "binary-amd64"
            / "by-hash"
            / "sha512"
            / hashlib.sha512(pkgs).hexdigest()
        ).is_file()
    finally:
        _close(urls)


def test_unsigned_source_fallback_urls_carry_dists_prefix(tmp_path: Path) -> None:
    """Scenario 6 (spec §7.1 #1): the no-Release well-known fallback for
    the unsigned ``nosrc`` source repo queues ``dists/``-prefixed URLs —
    the e2e proof that an unsigned source repo is actually mirrored (the
    original dropped the segment and never mirrored it)."""
    urls = build_fixture_repo(
        tmp_path / "fixture",
        by_hash=False,
        insecure_repo=False,
        unsigned_src_repo=True,
    )
    try:
        base = tmp_path / "spool"
        config = write_config(base, {"nosrc": urls["nosrc"]})
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        index_urls = (base / "var" / "index-urls.txt").read_text(encoding="utf-8")
        # The fixed, dists/-relative shape of the queued source URLs:
        assert "/dists/nosrc/main/source/" in index_urls, index_urls
        # The original bug's shape (no dists/ segment) must not appear.
        assert "/nosrc/nosrc/" not in index_urls, index_urls
        # And the repo actually mirrored end to end.
        tree = base / "mirror" / _host(urls["nosrc"]) / "nosrc"
        assert (tree / "dists" / "nosrc" / "main" / "source" / "Sources.gz").is_file()
        assert (tree / "pool" / "main" / "n" / "nosrc" / "nosrc_1.0-1.dsc").is_file()
    finally:
        _close(urls)


def test_set_without_value_is_a_named_error(tmp_path: Path) -> None:
    """Scenario 7a (spec §7.1 #3): a ``set`` line with no value is a hard
    error citing the 1-based line number (the original died with a generic
    'invalid line' message)."""
    config = tmp_path / "noval.list"
    config.write_text(
        f"set base_path {tmp_path / 'spool'}\nset limit_rate\n", encoding="utf-8"
    )
    result = run_mirror(config)
    assert result.returncode != 0
    assert "2: set limit_rate" in result.stderr
    assert "Traceback" not in result.stderr


def test_undefined_variable_reference_is_a_named_error(tmp_path: Path) -> None:
    """Scenario 7b (spec §7.1 #3): a ``$name`` reference to a key that was
    never set is a hard error naming the key (the original silently
    expanded it to an empty string)."""
    config = tmp_path / "undef.list"
    config.write_text(
        f"set base_path {tmp_path / 'spool'}\nset mirror_path $nope\n",
        encoding="utf-8",
    )
    result = run_mirror(config)
    assert result.returncode != 0
    assert "nope" in result.stderr
    assert "line 2" in result.stderr
    assert "Traceback" not in result.stderr


def test_postmirror_runs_with_mirror_cwd(tmp_path: Path) -> None:
    """Scenario 8 (spec §5.10): the postmirror script runs with its working
    directory set to ``mirror_path``; the fixture script pins its pwd to
    ``base/var/pwd.txt``."""
    urls = build_fixture_repo(
        tmp_path / "fixture",
        by_hash=False,
        insecure_repo=False,
        unsigned_src_repo=False,
    )
    try:
        base = tmp_path / "spool"
        config = write_config(
            base, {"main": urls["main"]}, extra=["set run_postmirror 1"]
        )
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Running the Post Mirror script" in result.stdout
        pwd = (base / "var" / "pwd.txt").read_text(encoding="utf-8").strip()
        assert pwd == str(base / "mirror")
    finally:
        _close(urls)


def test_credentials_never_leak_to_logs_or_files(
    authed_server: FixtureServer, tmp_path: Path
) -> None:
    """Scenario 9 (spec §5.3, Review Focus 1, re-verifies the Task 7 fix):
    with origin credentials embedded in the ``deb`` URL and
    ``auth_no_challenge 1``, the run completes and the secret appears in
    neither stdout, stderr, nor any tool-written file under ``base``."""
    urls = build_fixture_repo(
        tmp_path / "fixture",
        by_hash=False,
        insecure_repo=True,
        unsigned_src_repo=False,
        server=authed_server,
    )
    try:
        base = tmp_path / "base"
        base.mkdir(parents=True, exist_ok=True)
        host = _host(urls["insecure"])
        # The config is written OUTSIDE base: it is the run's input, and the
        # walk below scans only the tree the tool writes.
        config = tmp_path / "auth.list"
        cred_url = f"http://{AUTH_USER}:{AUTH_PASSWORD}@{host}/insecure"
        config.write_text(
            f"set base_path {base}\n"
            "set defaultarch amd64\n"
            "set nthreads 2\n"
            "set limit_rate 100m\n"
            "set run_postmirror 0\n"
            "set _autoclean 0\n"
            "set auth_no_challenge 1\n"
            f"deb {cred_url} insecure main\n"
            f"clean http://{host}/insecure\n",
            encoding="utf-8",
        )
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr

        # Auth was transparently satisfied: the files mirror in full.
        tree = base / "mirror" / host / "insecure"
        deb = tree / "pool" / "main" / "i" / "img" / "img_1.0-1_amd64.deb"
        assert (
            deb.read_bytes()
            == (
                urls.root
                / "insecure"
                / "pool"
                / "main"
                / "i"
                / "img"
                / "img_1.0-1_amd64.deb"
            ).read_bytes()
        )
        assert (tree / "dists" / "insecure" / "Release").is_file()

        # The literal secret (and its userinfo spelling) is nowhere:
        secret = AUTH_PASSWORD.encode("ascii")
        userinfo = f"{AUTH_USER}:{AUTH_PASSWORD}@"
        assert secret not in result.stdout.encode("ascii")
        assert userinfo.encode("ascii") not in result.stdout.encode("ascii")
        assert secret not in result.stderr.encode("ascii")
        assert userinfo.encode("ascii") not in result.stderr.encode("ascii")
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            assert secret not in data, f"credential leaked into {path}"
            assert userinfo.encode("ascii") not in data, (
                f"credential leaked into {path}"
            )
    finally:
        _close(urls)


def test_flat_repo_mirrors_without_dists_segment(tmp_path: Path) -> None:
    """Scenario 10 (spec §5.1, Review Focus 4): a component-less (flat)
    ``deb`` line mirrors through the unsigned flat fallback — files land
    under ``<uri>/<distribution>/`` and nothing in the tree carries a
    ``dists/`` segment."""
    urls = build_fixture_repo(
        tmp_path / "fixture",
        by_hash=False,
        insecure_repo=False,
        unsigned_src_repo=False,
        flat_repo=True,
    )
    try:
        base = tmp_path / "spool"
        config = write_config(base, {"flat": urls["flat"]})
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        tree = base / "mirror" / _host(urls["flat"]) / "flat"
        # Index and pool files land relative to the distribution, no dists/.
        assert (tree / "flat" / "Packages").is_file()
        assert (tree / "flat" / "Packages").read_bytes() == (
            urls.root / "flat" / "flat" / "Packages"
        ).read_bytes()
        deb = tree / "pool" / "f" / "fpkg" / "fpkg_1.0-1_amd64.deb"
        assert (
            deb.read_bytes()
            == (
                urls.root / "flat" / "pool" / "f" / "fpkg" / "fpkg_1.0-1_amd64.deb"
            ).read_bytes()
        )
        # No dists/ segment anywhere in the mirrored tree.
        for rel in _files(tree):
            assert "dists" not in Path(rel).parts, rel
    finally:
        _close(urls)


def test_nthreads_capped_at_url_count(tmp_path: Path) -> None:
    """Scenario 11 (spec §5.3): a stage's worker count is
    ``min(nthreads, #urls)`` — with ``nthreads 64`` the 3-URL release stage
    reports "using 3 threads" and the run completes with all files."""
    urls = build_fixture_repo(
        tmp_path / "fixture", by_hash=False, insecure_repo=True, unsigned_src_repo=False
    )
    try:
        base = tmp_path / "spool"
        config = write_config(
            base, {"insecure": urls["insecure"]}, extra=["set nthreads 64"]
        )
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        # The release stage of one repo entry has exactly 3 URLs.
        assert "Downloading 3 files using 3 threads" in result.stdout
        tree = base / "mirror" / _host(urls["insecure"]) / "insecure"
        assert (tree / "dists" / "insecure" / "Release").is_file()
        assert (
            tree / "dists" / "insecure" / "main" / "binary-amd64" / "Packages"
        ).is_file()
        assert (tree / "pool" / "main" / "i" / "img" / "img_1.0-1_amd64.deb").is_file()
    finally:
        _close(urls)


def test_invalid_limit_rate_is_a_clean_error(tmp_path: Path) -> None:
    """Scenario 12 (Review Focus 5): an invalid ``limit_rate`` fails fast
    with a clean config error naming the option — no traceback."""
    config = tmp_path / "badrate.list"
    config.write_text(
        f"set base_path {tmp_path / 'spool'}\nset limit_rate bananas\n",
        encoding="utf-8",
    )
    result = run_mirror(config)
    assert result.returncode != 0
    assert "limit_rate" in result.stderr
    assert "bananas" in result.stderr
    assert "Traceback" not in result.stderr


def _flat_proxy_config(
    base: Path, deb_url: str, proxy_url: str, no_check: str | None
) -> Path:
    """A single flat-repo config routed through the ``LocalProxy``."""
    base.mkdir(parents=True, exist_ok=True)
    lines = [
        f"set base_path {base}",
        "set defaultarch amd64",
        "set nthreads 2",
        "set limit_rate 100m",
        "set run_postmirror 0",
        "set _autoclean 0",
        "set use_proxy 1",
        f"set https_proxy {proxy_url}",
        f"set http_proxy {proxy_url}",
    ]
    if no_check is not None:
        lines.append(f"set no_check_certificate {no_check}")
    lines.append(f"deb {deb_url} flat")
    lines.append(f"clean {deb_url}")
    config = base / "mirror.list"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config


def test_https_proxy_tunnel_sends_proxy_authorization(
    https_server: FixtureServer, tmp_path: Path
) -> None:
    """Scenario 13a (Review C2): with ``use_proxy 1`` an https target must
    CONNECT-tunnel through the proxy sending Proxy-Authorization — the
    old ``set_tunnel(..., proxy_authorization=...)`` call is a TypeError
    on every released CPython and killed the whole run."""
    root = tmp_path / "fixture"
    build_flat_repo(root, dist="flat", release=True, variant=0)
    serve_subtree(https_server, root, "flat")
    deb_url = https_server.url("/flat")
    assert deb_url.startswith("https://")
    with LocalProxy() as proxy:
        base = tmp_path / "spool"
        config = _flat_proxy_config(
            base, deb_url, f"http://proxu:proxp@127.0.0.1:{proxy.port}", "1"
        )
        result = run_mirror(config)
    assert result.returncode == 0, result.stdout + result.stderr
    # Every file of the flat repo mirrored byte-for-byte (all through the
    # tunnel — the origin only ever sees proxied traffic).
    host = _host(deb_url)
    tree = base / "mirror" / host / "flat"
    assert (tree / "flat" / "Packages").read_bytes() == (
        root / "flat" / "flat" / "Packages"
    ).read_bytes()
    assert (tree / "flat" / "Release").is_file()
    deb = tree / "pool" / "f" / "fpkg" / "fpkg_1.0-1_amd64.deb"
    assert (
        deb.read_bytes()
        == (root / "flat" / "pool" / "f" / "fpkg" / "fpkg_1.0-1_amd64.deb").read_bytes()
    )
    # The proxy saw CONNECT requests with the expected Basic token.
    expected = "Basic " + base64.b64encode(b"proxu:proxp").decode("ascii")
    assert proxy.requests, "no request reached the proxy at all"
    assert all("127.0.0.1" in request and ":" in request for request in proxy.requests)
    assert proxy.seen_auth.count(expected) == len(proxy.requests)


def test_http_proxy_absolute_form_sends_proxy_authorization(
    tmp_path: Path,
) -> None:
    """Scenario 13b (Review C2): a plain-http target goes to the proxy as
    an absolute-form request carrying Proxy-Authorization (from the proxy
    URL's userinfo) and the file mirrors end to end."""
    root = tmp_path / "fixture"
    build_flat_repo(root, dist="flat", release=True, variant=0)
    origin = FixtureServer()
    origin.__enter__()
    serve_subtree(origin, root, "flat")
    with LocalProxy() as proxy:
        try:
            base = tmp_path / "spool"
            config = _flat_proxy_config(
                base,
                origin.url("/flat"),
                f"http://proxu:proxp@127.0.0.1:{proxy.port}",
                None,
            )
            result = run_mirror(config)
        finally:
            origin.__exit__(None, None, None)
    assert result.returncode == 0, result.stdout + result.stderr
    host = _host(origin.url("/flat"))
    tree = base / "mirror" / host / "flat"
    assert (tree / "flat" / "Packages").read_bytes() == (
        root / "flat" / "flat" / "Packages"
    ).read_bytes()
    deb = tree / "pool" / "f" / "fpkg" / "fpkg_1.0-1_amd64.deb"
    assert (
        deb.read_bytes()
        == (root / "flat" / "pool" / "f" / "fpkg" / "fpkg_1.0-1_amd64.deb").read_bytes()
    )
    # Every request arrived at the proxy in absolute form and carried the
    # Basic token derived from the proxy URL's userinfo.
    expected = "Basic " + base64.b64encode(b"proxu:proxp").decode("ascii")
    assert proxy.requests, "no request reached the proxy at all"
    assert all(request.startswith(f"http://{host}/flat") for request in proxy.requests)
    assert proxy.seen_auth.count(expected) == len(proxy.requests)


def test_multi_arch_deb_line_mirrors_every_arch(tmp_path: Path) -> None:
    """Scenario 14 (Review C1): ``deb [arch=amd64,i386]`` mirrors BOTH
    architectures' indexes and pool archives — stage 3 used to interpolate
    the raw ``"amd64,i386"`` string into the index paths and skipped every
    arch-specific Packages with a silent warning, so the i386 .deb never
    reached the mirror."""
    urls = build_fixture_repo(
        tmp_path / "fixture",
        insecure_repo=False,
        unsigned_src_repo=False,
        multi_arch=True,
    )
    try:
        base = tmp_path / "spool"
        base.mkdir(parents=True, exist_ok=True)
        deb_line = f"deb [arch=amd64,i386] {urls['main']} dist main contrib"
        config = base / "mirror.list"
        config.write_text(
            "\n".join(
                [
                    f"set base_path {base}",
                    "set defaultarch amd64",
                    "set nthreads 2",
                    "set limit_rate 100m",
                    "set run_postmirror 0",
                    "set _autoclean 0",
                    deb_line,
                    f"clean {urls['main']}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        # Missing-index warnings may only concern the fixture's empty
        # ``contrib`` component (listed in the Release, no files).  None may
        # target main's per-arch indexes — the C1 regression was the raw
        # "amd64,i386" spelling in the index path.
        bad = [
            line
            for line in result.stderr.splitlines()
            if "can't open index" in line and "/main/binary-" in line
        ]
        assert not bad, bad
        assert "amd64,i386" not in result.stderr
        assert "Processing indexes: [P]" in result.stdout
        host = _host(urls["main"])
        tree = base / "mirror" / host / "repo"
        dist = tree / "dists" / "dist" / "main"
        root_main = urls.root / "repo" / "dists" / "dist" / "main"
        # Both arches' Packages indexes are in the tree, byte-identical.
        assert (dist / "binary-amd64" / "Packages.gz").read_bytes() == (
            root_main / "binary-amd64" / "Packages.gz"
        ).read_bytes()
        assert (dist / "binary-i386" / "Packages.gz").read_bytes() == (
            root_main / "binary-i386" / "Packages.gz"
        ).read_bytes()
        assert (dist / "binary-all" / "Packages").is_file()
        # Both arches' pool .debs byte-mirror the fixture.
        assert (
            tree / "pool" / "main" / "b" / "bar" / "bar_1.0-1_amd64.deb"
        ).read_bytes() == (
            urls.root / "repo" / "pool" / "main" / "b" / "bar" / "bar_1.0-1_amd64.deb"
        ).read_bytes()
        assert (
            tree / "pool" / "main" / "m" / "multi" / "multi_1.0-1_i386.deb"
        ).read_bytes() == (
            urls.root
            / "repo"
            / "pool"
            / "main"
            / "m"
            / "multi"
            / "multi_1.0-1_i386.deb"
        ).read_bytes()
    finally:
        _close(urls)


def test_non_utf8_config_is_a_clean_named_error(tmp_path: Path) -> None:
    """Scenario 15 (parked minor): a binary config file is a clean fatal
    error naming the file (no UnicodeDecodeError traceback)."""
    config = tmp_path / "badutf8.list"
    config.write_bytes(b"set base_path x\nset nthreads f\xffoo\n")
    result = run_mirror(config)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert str(config) in result.stderr
    assert "not valid UTF-8" in result.stderr


def test_secondary_by_hash_paths_are_hardlinked(tmp_path: Path) -> None:
    """Scenario 16 (spec §5.6.3): the Perl's secondary canonical to
    other-digest mapping (file_to_hashsums) is preserved — a by-hash file
    listed under TWO OR MORE supported sections (the fixture Release lists
    every file under all four, with real digests) ends up hardlinked at
    the by-hash path of EVERY section it advertises, not just the
    strongest one it is downloaded from."""
    urls = build_fixture_repo(
        tmp_path / "fixture", insecure_repo=False, unsigned_src_repo=False
    )
    try:
        base = tmp_path / "spool"
        config = write_config(base, {"main": urls["main"]})
        result = run_mirror(config)
        assert result.returncode == 0, result.stdout + result.stderr
        pkgs_path = (
            urls.root
            / "repo"
            / "dists"
            / "dist"
            / "main"
            / "binary-amd64"
            / "Packages.gz"
        )
        pkgs = pkgs_path.read_bytes()
        dist = (
            base
            / "mirror"
            / _host(urls["main"])
            / "repo"
            / "dists"
            / "dist"
            / "main"
            / "binary-amd64"
        )
        digests = {
            "sha512": hashlib.sha512(pkgs).hexdigest(),
            "sha256": hashlib.sha256(pkgs).hexdigest(),
            "sha1": hashlib.sha1(pkgs).hexdigest(),
            "md5sum": hashlib.md5(pkgs).hexdigest(),
        }
        anchor = dist / "by-hash" / "sha512" / digests["sha512"]
        assert anchor.is_file()
        assert anchor.read_bytes() == pkgs
        for name, digest in digests.items():
            path = dist / "by-hash" / name / digest
            assert path.is_file(), f"missing secondary by-hash path {name}"
            assert path.read_bytes() == pkgs
            # Hardlinked into the same inode as the downloaded sha512 copy.
            assert path.stat().st_ino == anchor.stat().st_ino, name
    finally:
        _close(urls)
