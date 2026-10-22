"""Tests for cleanup, skel-to-mirror copy, locking, and the clean script (Task 7).

All fixtures are temp-file only (filesystem-only, no network).  Pins come
from the task brief (step 1) and the controller clarifications:

- ``collect_unneeded`` walks each clean dir recursively (sorted, so results
  are deterministic), marks every file whose (``_tilde``/``_plus``-normalised)
  tree-relative path is in skipclean as needed, always keeps symlinks (so a
  directory holding only symlinks survives), sums removed-file sizes in
  512-byte block units (a 2048-byte file on a 4 KiB-block filesystem
  accounts as 4096), and lists directories bottom-up.
- ``write_cleanscript`` emits POSIX sh (shebang, ``set -e``, single-quoted
  ``cd``/paths with ``'`` escaped as ``'\\''``), the original's
  ``[NN%]``/``.`` progress cadence with a divide-by-zero guard for empty
  lists (an empty script still passes ``sh -n`` and reads
  ``Removing 0 unnecessary files``), a ``[100%]`` echo per non-empty
  section, ``rm -r`` guarded by ``test -d``, and the executable bit.
- ``copy_file`` preserves the source mtime, no-ops when (st_size,
  st_mtime_ns) already match (stable hardlink inode), and honours
  ``unlink_repl`` (delete-then-write for differing content).
- ``locked`` takes an exclusive non-blocking flock on
  ``<var_path>/apt-mirror.lock``, raises with the exact original message on
  conflict, acquires a stale (holderless) lock file, and removes the lock
  file on exit.
"""

import fcntl
import gzip
import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import FixtureServer


def _write(path: Path, content: bytes) -> None:
    """Create ``path``'s parents and write ``content`` to it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _sh_syntax_ok(script: Path) -> None:
    """Assert the generated script passes ``sh -n``."""
    result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, f"sh -n failed: {result.stderr}"


# ---------------------------------------------------------------------------
# collect_unneeded
# ---------------------------------------------------------------------------


class TestCollectUnneeded:
    """The §5.9 tree walk: skipclean lookup, symlinks, block accounting."""

    def test_tree_symlink_and_block_accounting(self, am, tmp_path) -> None:
        mirror = tmp_path / "mirror"
        # a/b/file1  -> needed via skipclean
        # a/c/junk   -> 2048 bytes, unneeded -> 4096 in 512-byte blocks
        # a/d        -> empty directory
        # a/e/link   -> symlink (a/e keeps it because symlinks are needed)
        _write(mirror / "a" / "b" / "file1", b"needed")
        _write(mirror / "a" / "c" / "junk", b"j" * 2048)
        (mirror / "a" / "d").mkdir(parents=True)
        (mirror / "a" / "e").mkdir(parents=True)
        (mirror / "a" / "e" / "link").symlink_to("b/file1")

        unneeded = am.collect_unneeded(
            mirror, ["a"], {"a/b/file1"}, tilde=False, plus=False
        )
        assert unneeded.files == ["a/c/junk"]
        assert unneeded.dirs == ["a/c", "a/d"]  # bottom-up
        assert unneeded.nbytes == 4096  # 2048 bytes -> 8 x 512-byte blocks
        # The symlink and its parent directory must survive.
        assert "a/e" not in unneeded.dirs
        assert not any(name.startswith("a/link") for name in unneeded.files)

    def test_tilde_plus_normalisation_before_lookup(self, am, tmp_path) -> None:
        # Disk paths use the literal ``~`` spelling; the skipclean entries
        # were built with the _tilde encoding (``%7E``) exactly as parsing a
        # config `skip-clean` line would produce under _tilde=1.
        mirror = tmp_path / "mirror"
        _write(mirror / "h" / "~name" / "x.bin", b"data")
        _write(mirror / "h" / "plus+name" / "y.bin", b"data")
        skipclean = {"h/%7Ename/x.bin", "h/plus%2Bname/y.bin"}

        unneeded = am.collect_unneeded(mirror, ["h"], skipclean, tilde=True, plus=True)
        # Both files match after the ~ -> %7E and + -> %2B normalisation.
        assert unneeded.files == []
        assert unneeded.dirs == []
        assert unneeded.nbytes == 0

        # With the encoding off the same skipclean set no longer matches
        # (files in sorted walk order: '+' (0x2B) sorts before '~' (0x7E)).
        unneeded = am.collect_unneeded(
            mirror, ["h"], skipclean, tilde=False, plus=False
        )
        assert unneeded.files == ["h/plus+name/y.bin", "h/~name/x.bin"]

    def test_disk_path_already_percent_encoded(self, am, tmp_path) -> None:
        # A tree written with the encoded form matches a skipclean entry
        # built with _tilde encoding, with _tilde on or off.
        mirror = tmp_path / "mirror"
        _write(mirror / "host" / "%7Ename" / "z.bin", b"data")
        skipclean = {"host/%7Ename/z.bin"}
        for tilde in (True, False):
            unneeded = am.collect_unneeded(
                mirror, ["host"], skipclean, tilde=tilde, plus=False
            )
            assert unneeded.files == []

    def test_missing_clean_root_is_skipped(self, am, tmp_path) -> None:
        mirror = tmp_path / "mirror"
        mirror.mkdir()
        unneeded = am.collect_unneeded(mirror, ["nope"], set(), tilde=False, plus=False)
        assert unneeded.files == []
        assert unneeded.dirs == []
        assert unneeded.nbytes == 0


# ---------------------------------------------------------------------------
# write_cleanscript
# ---------------------------------------------------------------------------


class TestWriteCleanscript:
    """The generated POSIX sh cleanup script (spec §5.9)."""

    def test_empty_lists_no_divide_by_zero(self, am, tmp_path) -> None:
        mirror = tmp_path / "mirror"
        mirror.mkdir()
        script = tmp_path / "clean.sh"
        am.write_cleanscript(script, str(mirror), [], [])
        assert script.exists()
        text = script.read_text(encoding="utf-8")
        assert "Removing 0 unnecessary files" in text
        assert "Removing 0 unnecessary directories" in text
        assert "[0%]" not in text  # no progress cadence at all when empty
        _sh_syntax_ok(script)

    def test_three_files_two_dirs(self, am, tmp_path) -> None:
        mirror = tmp_path / "mirror"
        _write(mirror / "f1.bin", b"a" * 100)
        _write(mirror / "f2.bin", b"b" * 100)
        _write(mirror / "f3.bin", b"c" * 100)
        (mirror / "d1" / "sub").mkdir(parents=True)

        script = tmp_path / "clean.sh"
        am.write_cleanscript(
            script, str(mirror), ["f1.bin", "f2.bin", "f3.bin"], ["d1/sub", "d1"]
        )
        text = script.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0] == "#!/bin/sh"
        assert "set -e" in lines
        assert f"cd '{mirror}'" in lines
        rm_lines = [line for line in lines if line.startswith("rm -f ")]
        assert rm_lines == [
            "rm -f 'f1.bin'",
            "rm -f 'f2.bin'",
            "rm -f 'f3.bin'",
        ]
        assert "echo -n '[100%]'" in lines
        assert "if test -d 'd1/sub'; then rm -r 'd1/sub'; fi" in lines
        assert "if test -d 'd1'; then rm -r 'd1'; fi" in lines
        assert "echo 'Removing 3 unnecessary files [" in text
        assert "echo 'Removing 2 unnecessary directories...'" in lines
        assert script.stat().st_mode & 0o111
        _sh_syntax_ok(script)

    def test_single_quote_round_trip(self, am, tmp_path) -> None:
        mirror = tmp_path / "mirror"
        _write(mirror / "we'ird.bin", b"x")
        script = tmp_path / "clean.sh"
        am.write_cleanscript(script, str(mirror), ["we'ird.bin"], [])
        text = script.read_text(encoding="utf-8")
        # The path renders single-quoted with ' escaped as '\'' so the
        # script still sh-evaluates to the original path.
        assert "rm -f 'we'\\''ird.bin'" in text
        _sh_syntax_ok(script)
        # Prove the quoting round-trips: a tiny script that echoes the
        # quoted token must print the original path.
        check = tmp_path / "check.sh"
        check.write_text("echo -n 'we'\\''ird.bin'\n", encoding="utf-8")
        result = subprocess.run(["sh", str(check)], capture_output=True, text=True)
        assert result.stdout == "we'ird.bin"


# ---------------------------------------------------------------------------
# copy_file
# ---------------------------------------------------------------------------


class TestCopyFile:
    """The §5.8 skel -> mirror copy helper."""

    def test_fresh_copy_preserves_mtime(self, am, tmp_path) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"hello")
        stamp = int(time.time() - 1000)
        os.utime(src, ns=(stamp * 1_000_000_000, stamp * 1_000_000_000))

        dst = tmp_path / "out" / "dst.bin"
        am.copy_file(src, dst, unlink_repl=False)
        assert dst.read_bytes() == b"hello"
        assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns

    def test_equal_file_second_call_is_noop(self, am, tmp_path) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"hello")
        dst = tmp_path / "dst.bin"
        am.copy_file(src, dst, unlink_repl=False)
        assert dst.stat().st_ino == src.stat().st_ino  # hardlinked
        am.copy_file(src, dst, unlink_repl=False)
        # No rewrite: the stable inode proves nothing was unlinked/relinked.
        assert dst.stat().st_ino == src.stat().st_ino
        assert dst.read_bytes() == b"hello"

    def test_unlink_repl_replaces_differing_content(self, am, tmp_path) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"one")
        dst = tmp_path / "dst.bin"
        dst.write_bytes(b"two-two")
        old_ino = dst.stat().st_ino
        am.copy_file(src, dst, unlink_repl=True)
        assert dst.read_bytes() == b"one"
        assert dst.stat().st_ino != old_ino

    def test_no_unlink_uses_link_then_fallback(self, am, tmp_path) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"AAA")
        dst = tmp_path / "dst.bin"
        dst.write_bytes(b"BB")
        am.copy_file(src, dst, unlink_repl=False)
        # The link attempt hits the existing destination and falls back to a
        # copy: content is replaced, but without the pre-unlink semantics.
        assert dst.read_bytes() == b"AAA"
        assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns

    def test_missing_source_is_skipped(self, am, tmp_path) -> None:
        dst = tmp_path / "new" / "dst.bin"
        am.copy_file(tmp_path / "nope.bin", dst, unlink_repl=False)
        assert not dst.exists()
        assert not (tmp_path / "new").exists()  # parents are not created


# ---------------------------------------------------------------------------
# locked
# ---------------------------------------------------------------------------


class TestLocked:
    """The §5.2 exclusive non-blocking flock on <var_path>/apt-mirror.lock."""

    def test_conflict_raises_exact_message(self, am, tmp_path) -> None:
        var = tmp_path / "var"
        var.mkdir()
        lock_path = var / "apt-mirror.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(am.AptMirrorError) as excinfo, am.locked(var):
                pass  # pragma: no cover - reached only if lock is free
            assert str(excinfo.value) == "apt-mirror is already running, exiting"
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        # The failed attempt leaves the (stale) lock file in place.
        assert lock_path.exists()

    def test_stale_lock_is_acquirable_and_removed_on_exit(self, am, tmp_path) -> None:
        var = tmp_path / "var"
        var.mkdir()
        lock_path = var / "apt-mirror.lock"
        # Simulate a crashed holder: an on-disk lock file with no live lock.
        lock_path.write_bytes(b"stale")
        with am.locked(var):
            assert lock_path.exists()  # held while the body runs
        assert not lock_path.exists()  # removed on exit


# ---------------------------------------------------------------------------
# main(): Release warning emission + var/NEW credential stripping
# ---------------------------------------------------------------------------


class TestMainReleaseWarningsAndCredentialStrip:
    """One in-process main() run over a single canonical binary repo whose
    Release carries a malformed checksum continuation, served under a
    userinfo-embedded config URI.

    Pins the 2026-10-22 review fixes: spec §5.6 step 2 (each
    ``ReleaseInfo.warnings`` entry is printed to stderr, verbatim, as Task 5
    builds it) and spec §7.1 fix #2 (a persisted file never carries
    ``user:pass@`` — var/NEW lines strip the userinfo while the fetcher
    still receives the full URL).
    """

    def test_warns_malformed_line_and_strips_new_credentials(
        self, am, tmp_path, capsys
    ) -> None:
        def build(srv: FixtureServer) -> None:
            deb = b"deb-payload-" * 10
            packages = (
                "Package: foo\n"
                "Architecture: amd64\n"
                "Version: 1.0-1\n"
                "Filename: pool/main/f/foo/foo_1.0-1_amd64.deb\n"
                f"Size: {len(deb)}\n"
                f"MD5sum: {hashlib.md5(deb).hexdigest()}\n"
                f"SHA1: {hashlib.sha1(deb).hexdigest()}\n"
                f"SHA256: {hashlib.sha256(deb).hexdigest()}\n"
                "\n"
            ).encode()
            packages_gz = gzip.compress(packages, mtime=0)
            bad_content = "deadbeefcafe 1234"  # 2 fields, no filename
            release = (
                "Origin: review\nSuite: stable\nCodename: review\n"
                "SHA256:\n"
                f"  {hashlib.sha256(packages_gz).hexdigest()} "
                f"{len(packages_gz)} main/binary-amd64/Packages.gz\n"
                f"  {bad_content}\n"
            ).encode()
            srv.add("/repo/dists/stable/Release", release)
            srv.add("/repo/dists/stable/main/binary-amd64/Packages.gz", packages_gz)
            srv.add("/repo/pool/main/f/foo/foo_1.0-1_amd64.deb", deb)

        with FixtureServer() as srv:
            build(srv)
            port = srv.port
            spool = tmp_path / "spool"
            config = tmp_path / "mirror.list"
            # The URI embeds userinfo; the fixture does not require auth —
            # the fetch simply carries (and is not denied by) the header.
            config.write_text(
                f"set base_path {spool}\n"
                "set nthreads 2\n"
                f"deb http://u:p@127.0.0.1:{port}/repo stable main\n"
            )
            rc = am.main([str(config)])
            captured = capsys.readouterr()

        assert rc == 0
        host = f"127.0.0.1:{port}"
        var = spool / "var"
        # mirror-relative (sanitised, credential-free) on skel-tree layout.
        assert (var / "ALL").read_text().splitlines() == [
            f"{host}/repo/pool/main/f/foo/foo_1.0-1_amd64.deb"
        ]
        # NEW: exactly the full repo-root URL, userinfo-stripped.
        new_lines = (var / "NEW").read_text().splitlines()
        assert new_lines == [f"http://{host}/repo/pool/main/f/foo/foo_1.0-1_amd64.deb"]
        assert "u:p@" not in (var / "NEW").read_text()
        # Spec §5.6 step 2: the malformed-continuation warning on stderr,
        # exactly as ReleaseInfo.warnings builds it (message names the
        # skel-local Release path).
        release_path = spool / "skel" / host / "repo" / "dists" / "stable" / "Release"
        expected = f'Malformed checksum line "deadbeefcafe 1234" in {release_path}'
        assert expected in captured.err
