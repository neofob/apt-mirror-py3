"""End-to-end tests: the real tool, run as a subprocess, against fixture
repos generated under 127.0.0.1 (spec §6 scenarios 1-4, 6 and 9).

Every scenario builds its repo with ``build_fixture_repo`` (tests/conftest.py),
writes a config with ``write_config``, and drives ``python3 apt-mirror.py``
through ``run_mirror``.  Servers are on 127.0.0.1 only: no external network.
"""

import hashlib
import subprocess
import sys
import time

from .conftest import REPO_ROOT, build_fixture_repo, run_mirror, write_config

#: Pool archives of the "main" fixture repo, by name and tree-relative path.
MAIN_DEBS = (
    "pool/all/h/hello/hello_2.0-1_all.deb",
    "pool/main/b/bar/bar_1.0-1_amd64.deb",
    "pool/main/f/foo/foo_1.0-1_amd64.deb",
)


def _host(url: str) -> str:
    """The host[:port] component of a fixture repo base URL."""
    return url.split("://", 1)[1].split("/", 1)[0]


def _port(url: str) -> int:
    """The port component of a fixture repo base URL."""
    return int(_host(url).rsplit(":", 1)[1])


def test_first_run_expected_tree(fixture_repo, tmp_path) -> None:
    """First run: release files, hardlinked by-hash+canonical indexes, all
    pool files, and the var/ bookkeeping files (brief step 1.1)."""
    urls = fixture_repo
    base = tmp_path / "spool"
    config = write_config(base, {"main": urls["main"], "nosrc": urls["nosrc"]})
    result = run_mirror(config)
    assert result.returncode == 0, result.stdout + result.stderr

    host = _host(urls["main"])
    mirror = base / "mirror"
    dist = mirror / host / "repo" / "dists" / "dist"
    # Release + InRelease present, no Release.gpg (404 tolerated).
    assert (dist / "Release").is_file()
    assert (dist / "InRelease").is_file()
    assert not (dist / "Release.gpg").exists()
    # All three pool .debs (incl. the all-arch one) and the source pool files.
    repo_root = mirror / host / "repo"
    pool = repo_root / "pool"
    for rel in MAIN_DEBS:
        assert (repo_root / rel).is_file(), rel
    assert not (pool / "contrib").exists()
    assert (pool / "main" / "f" / "foo" / "foo_1.0-1.dsc").is_file()
    assert (pool / "main" / "f" / "foo" / "foo_1.0-1.diff.gz").is_file()
    # By-hash and canonical index paths are hardlinked (same st_ino).
    pkgs = (
        urls.root / "repo" / "dists" / "dist" / "main" / "binary-amd64" / "Packages.gz"
    )
    digest512 = hashlib.sha512(pkgs.read_bytes()).hexdigest()
    canonical = dist / "main" / "binary-amd64" / "Packages.gz"
    by_hash = dist / "main" / "binary-amd64" / "by-hash" / "sha512" / digest512
    assert canonical.is_file()
    assert by_hash.is_file()
    assert canonical.stat().st_ino == by_hash.stat().st_ino
    # The i18n translation index landed too.
    assert (dist / "main" / "i18n" / "Translation-en.gz").is_file()
    # The unsigned source repo's index and pool file.
    nosrc = mirror / host / "nosrc"
    assert (nosrc / "dists" / "nosrc" / "main" / "source" / "Sources.gz").is_file()
    assert (nosrc / "pool" / "main" / "n" / "nosrc" / "nosrc_1.0-1.dsc").is_file()
    # var/ALL carries the three pool paths.
    all_text = (base / "var" / "ALL").read_text()
    for rel in MAIN_DEBS:
        assert f"{host}/repo/{rel}" in all_text
    # var/NEW is non-empty on a first run: full, credential-stripped URLs.
    new_lines = (base / "var" / "NEW").read_text().splitlines()
    assert new_lines
    assert (
        f"http://127.0.0.1:{_port(urls['main'])}/repo/pool/main/f/foo/"
        "foo_1.0-1_amd64.deb"
    ) in new_lines
    # var/MD5 lines are md5sums of the .deb bytes actually on disk.
    md5_text = (base / "var" / "MD5").read_text().splitlines()
    for p in sorted(pool.rglob("*.deb")):
        digest = hashlib.md5(p.read_bytes()).hexdigest()
        assert f"{digest}  {p.relative_to(mirror).as_posix()}" in md5_text


def test_second_run_is_idempotent(fixture_repo, tmp_path) -> None:
    """Second run: exit 0, empty var/NEW, untouched .deb bytes and mtimes,
    and no new requests to the pool (brief step 1.2)."""
    urls = fixture_repo
    base = tmp_path / "spool"
    config = write_config(base, {"main": urls["main"], "nosrc": urls["nosrc"]})
    assert run_mirror(config).returncode == 0

    srv = urls.server
    assert srv is not None
    # Pinned probe: the second run must leave the /pool/ request count
    # unchanged (the archive stage runs with an empty URL list).  The
    # per-repo probes below make the same guarantee non-vacuous for the
    # real pool paths of this fixture.
    pool_count = srv.count("/pool/")
    repo_pool_count = srv.count("/repo/pool/")
    assert repo_pool_count > 0
    nosrc_pool_count = srv.count("/nosrc/pool/")
    host = _host(urls["main"])
    mirror = base / "mirror" / host / "repo" / "pool"
    debs = sorted(mirror.rglob("*.deb"))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in debs}

    result = run_mirror(config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (base / "var" / "NEW").read_text() == ""
    for p, (data, mtime_ns) in before.items():
        assert p.read_bytes() == data, p
        assert p.stat().st_mtime_ns == mtime_ns, p
    assert srv.count("/pool/") == pool_count
    assert srv.count("/repo/pool/") == repo_pool_count
    assert srv.count("/nosrc/pool/") == nosrc_pool_count


def test_truncated_deb_is_restored(fixture_repo, tmp_path) -> None:
    """A corrupt (wrong-size) pool .deb is deleted and re-downloaded byte
    identically, and listed in var/NEW (brief step 1.3)."""
    urls = fixture_repo
    base = tmp_path / "spool"
    config = write_config(base, {"main": urls["main"], "nosrc": urls["nosrc"]})
    assert run_mirror(config).returncode == 0

    host = _host(urls["main"])
    target = (
        base
        / "mirror"
        / host
        / "repo"
        / "pool"
        / "main"
        / "f"
        / "foo"
        / "foo_1.0-1_amd64.deb"
    )
    expected = (
        urls.root / "repo" / "pool" / "main" / "f" / "foo" / "foo_1.0-1_amd64.deb"
    ).read_bytes()
    target.write_bytes(expected[: len(expected) // 2])  # wrong size, corrupt

    result = run_mirror(config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_bytes() == expected
    new_lines = (base / "var" / "NEW").read_text().splitlines()
    assert (
        f"http://127.0.0.1:{_port(urls['main'])}/repo/pool/main/f/foo/"
        "foo_1.0-1_amd64.deb"
    ) in new_lines


def test_changed_deb_is_refreshed(fixture_repo, tmp_path) -> None:
    """A repo rebuilt server-side with a new .deb (new content and size) has
    its mirror copy updated to the new bytes (brief step 1.4)."""
    urls = fixture_repo
    base = tmp_path / "spool"
    config = write_config(base, {"main": urls["main"]})
    assert run_mirror(config).returncode == 0

    host = _host(urls["main"])
    target = (
        base
        / "mirror"
        / host
        / "repo"
        / "pool"
        / "all"
        / "h"
        / "hello"
        / "hello_2.0-1_all.deb"
    )
    old_bytes = target.read_bytes()

    # Server-side update: regenerate the whole repo at the same URL with
    # variant 1 (different, larger .deb files) and a fresh, later
    # Last-Modified so If-Modified-Since re-checks re-fetch Release/index.
    fresh = tmp_path / "fixture-v2"
    build_fixture_repo(
        fresh,
        variant=1,
        last_modified=time.time() + 5.0,
        server=urls.server,
    )
    new_bytes = (
        fresh / "repo" / "pool" / "all" / "h" / "hello" / "hello_2.0-1_all.deb"
    ).read_bytes()
    assert new_bytes != old_bytes
    assert len(new_bytes) != len(old_bytes)

    result = run_mirror(config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_bytes() == new_bytes


def test_crash_recovery_and_release_parse_recovery(fixture_repo, tmp_path) -> None:
    """SIGKILL mid-archive-download leaves orphan .part files; the next
    normal run is exit 0 with the full tree and zero .part anywhere.
    A 10-byte truncated skel Release is detected and re-fetched
    (brief step 1.5)."""
    urls = fixture_repo
    base = tmp_path / "spool"
    config = write_config(
        base,
        {"main": urls["main"], "insecure": urls["insecure"]},
        extra=["set limit_rate 2000"],
    )
    host = _host(urls["main"])
    proc = subprocess.Popen(
        [sys.executable, "apt-mirror.py", str(config)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pool = base / "mirror" / host / "repo" / "pool"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and proc.poll() is None:
        if pool.is_dir():
            break
        time.sleep(0.05)
    assert pool.is_dir(), f"run exited before the pool dir: {proc.communicate()}"
    proc.kill()
    out, err = proc.communicate()
    assert proc.returncode != 0
    # The interrupted transfers left orphaned .part files behind.
    assert list(base.rglob("*.part")), f"no orphan .part files: {out} {err}"

    result = run_mirror(config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(base.rglob("*.part")) == []
    # Full tree correct: every pool archive byte-identical to the fixture.
    mirror = base / "mirror"
    root = urls.root
    for rel in (
        *MAIN_DEBS,
        "pool/main/f/foo/foo_1.0-1.dsc",
        "pool/main/f/foo/foo_1.0-1.diff.gz",
    ):
        local = (mirror / host / "repo" / rel).read_bytes()
        assert local == (root / "repo" / rel).read_bytes(), rel
    img = (
        mirror
        / host
        / "insecure"
        / "pool"
        / "main"
        / "i"
        / "img"
        / "img_1.0-1_amd64.deb"
    )
    assert (
        img.read_bytes()
        == (
            root / "insecure" / "pool" / "main" / "i" / "img" / "img_1.0-1_amd64.deb"
        ).read_bytes()
    )

    # Parse recovery: truncate the Release-only repo's skel Release to a
    # 10-byte fragment; the next run must re-fetch it (a zero-entry
    # fragment is a failed parse) so the mirrored Release is complete again.
    skel_release = base / "skel" / host / "insecure" / "dists" / "insecure" / "Release"
    skel_release.write_text("0123456789")  # 10 bytes, never valid
    result = run_mirror(config)
    assert result.returncode == 0, result.stdout + result.stderr
    release = base / "mirror" / host / "insecure" / "dists" / "insecure" / "Release"
    assert (
        release.read_bytes()
        == (root / "insecure" / "dists" / "insecure" / "Release").read_bytes()
    )
    packages = (
        base
        / "mirror"
        / host
        / "insecure"
        / "dists"
        / "insecure"
        / "main"
        / "binary-amd64"
        / "Packages"
    )
    assert packages.is_file()


def test_missing_config_is_fatal(tmp_path) -> None:
    """A non-existent config path is a hard error with the pinned stderr
    message (brief step 1.6)."""
    result = run_mirror(tmp_path / "no-such-config.list")
    assert result.returncode != 0
    assert "apt-mirror: invalid config file specified" in result.stderr
