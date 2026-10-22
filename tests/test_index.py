"""Tests for Packages/Sources index processing (Task 6).

All fixtures are temp-file only: index content is hand-written into
``tmp_path`` and compressed in place with the stdlib modules.  No network
access is needed.

Pins come from the task brief (step 1) and the controller clarifications:

- ``iter_index_blocks`` decompresses by suffix (``.gz``/``.bz2``/``.lzma``,
  plain otherwise); blocks are separated by blank lines; a repeated
  column-0 field name and an indented continuation line both extend the
  previous field's value with ``\\n``; a colon inside a field *value*
  (``Homepage: http://x/``) does not start a new field.
- ``parse_index`` yields one ``PackageRec`` per ``Filename:`` block and per
  ``File:`` line of a Sources block; hashes and size are taken verbatim
  (missing hash -> None, missing ``Size:`` -> 0); a ``Directory:`` value
  with a leading slash is normalised away; a ``File:`` line that does not
  split into exactly three fields — or a Sources block without a
  ``Directory:`` — raises ``AptMirrorError("apt-mirror: invalid Sources
  format")``; blocks with neither ``Filename:`` nor a ``File(s):`` field
  are ignored.
- ``need_update`` is the corrupt-mirror-file detector: missing file ->
  True, same size -> False, different size -> True *and* the file is
  deleted (size compared from the stat, not by reading the file).
"""

import bz2
import gzip
import lzma
from pathlib import Path

import pytest

# The repo's path in the mirror tree, as ``sanitise_uri`` would produce it
# for e.g. http://127.0.0.1:54321/repo.
TREE_PREFIX = "127.0.0.1:54321/repo"

# Distinct, well-formed digests.
MD5_AMD64 = "0123456789abcdef0123456789abcdef"
SHA1_AMD64 = "f" * 40
SHA256_AMD64 = "e" * 64
MD5_ALL = "fedcba9876543210fedcba9876543210"
SHA1_ALL = "d" * 40

# A realistic two-entry Packages index (one amd64 entry with all three
# hashes and ``Size: 47``, one all entry without ``SHA256:``).
PACKAGES_TEXT = (
    "Package: one\n"
    "Architecture: amd64\n"
    "Version: 1.0-1\n"
    "Filename: pool/main/o/one/one_1.0-1_amd64.deb\n"
    "Size: 47\n"
    f"MD5sum: {MD5_AMD64}\n"
    f"SHA1: {SHA1_AMD64}\n"
    f"SHA256: {SHA256_AMD64}\n"
    "Homepage: http://example.com/one\n"
    "\n"
    "Package: two\n"
    "Architecture: all\n"
    "Version: 2.0-1\n"
    "Filename: pool/main/t/two/two_2.0-1_all.deb\n"
    "Size: 34\n"
    f"MD5sum: {MD5_ALL}\n"
    f"SHA1: {SHA1_ALL}\n"
)


def compress(data: bytes, flavour: str) -> bytes:
    """Return ``data`` compressed with the named stdlib module."""
    if flavour == "gz":
        return gzip.compress(data)
    if flavour == "bz2":
        return bz2.compress(data)
    return lzma.compress(data)


def write_index(tmp_path, name: str) -> Path:
    """Return ``tmp_path / name`` (the file is written by the caller)."""
    return tmp_path / name


class TestPackagesGz:
    """A gzipped Packages index yields one record per block."""

    def test_gz_decompressed_and_parsed(self, am, tmp_path) -> None:
        path = write_index(tmp_path, "Packages.gz")
        path.write_bytes(compress(PACKAGES_TEXT.encode("utf-8"), "gz"))

        # The file on disk really is compressed (gzip magic, no plaintext).
        raw = path.read_bytes()
        assert raw[:2] == b"\x1f\x8b"
        assert b"Package: one" not in raw

        records = am.parse_index(path, TREE_PREFIX)
        assert len(records) == 2

        one, two = records
        assert one.url_rel == "pool/main/o/one/one_1.0-1_amd64.deb"
        assert one.mirror_rel == f"{TREE_PREFIX}/pool/main/o/one/one_1.0-1_amd64.deb"
        assert one.size == 47
        assert one.md5 == MD5_AMD64
        assert one.sha1 == SHA1_AMD64
        assert one.sha256 == SHA256_AMD64

        assert two.url_rel == "pool/main/t/two/two_2.0-1_all.deb"
        assert two.mirror_rel == f"{TREE_PREFIX}/pool/main/t/two/two_2.0-1_all.deb"
        assert two.size == 34
        assert two.md5 == MD5_ALL
        assert two.sha1 == SHA1_ALL
        assert two.sha256 is None


class TestCompressionVariants:
    """bz2 and lzma variants of the same content parse identically."""

    @pytest.mark.parametrize("flavour, suffix", [("bz2", ".bz2"), ("lzma", ".lzma")])
    def test_variant_parses_like_gz(self, am, tmp_path, flavour, suffix) -> None:
        gz_path = write_index(tmp_path, "Packages.gz")
        gz_path.write_bytes(compress(PACKAGES_TEXT.encode("utf-8"), "gz"))
        gz_records = am.parse_index(gz_path, TREE_PREFIX)

        path = write_index(tmp_path, f"Packages{suffix}")
        path.write_bytes(compress(PACKAGES_TEXT.encode("utf-8"), flavour))
        assert am.parse_index(path, TREE_PREFIX) == gz_records


class TestSources:
    """Sources blocks: multi-line File:, leading-slash Directory:."""

    def test_sources_records(self, am, tmp_path) -> None:
        text = (
            "Package: alpha\n"
            "Version: 1.0-1\n"
            "Directory: pool/main/a/alpha\n"
            "File: 0b1c2d3e 100 alpha_1.0-1.dsc\n"
            "\n"
            "Package: foo\n"
            "Version: 1.0-1\n"
            "Directory: /pool/main/x/foo\n"
            "File: 5d41402abc4b2a76b9719d911017c592 200 foo_1.0-1.dsc\n"
            "File: 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d 300 foo_1.0-1.debian.tar.xz\n"
        )
        path = write_index(tmp_path, "Sources")
        path.write_text(text, encoding="utf-8")

        records = am.parse_index(path, TREE_PREFIX)
        assert len(records) == 3
        alpha, foo_dsc, foo_tar = records

        assert alpha.url_rel == "pool/main/a/alpha/alpha_1.0-1.dsc"
        assert alpha.mirror_rel == f"{TREE_PREFIX}/pool/main/a/alpha/alpha_1.0-1.dsc"
        assert alpha.size == 100
        assert alpha.md5 == "0b1c2d3e"
        assert alpha.sha1 is None
        assert alpha.sha256 is None

        # Leading slash in Directory: is normalised away.
        assert foo_dsc.url_rel == "pool/main/x/foo/foo_1.0-1.dsc"
        assert foo_dsc.mirror_rel == f"{TREE_PREFIX}/pool/main/x/foo/foo_1.0-1.dsc"
        assert foo_dsc.size == 200
        assert foo_dsc.md5 == "5d41402abc4b2a76b9719d911017c592"

        assert foo_tar.url_rel == "pool/main/x/foo/foo_1.0-1.debian.tar.xz"
        assert foo_tar.size == 300
        assert foo_tar.md5 == "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"

    def test_missing_directory_raises(self, am, tmp_path) -> None:
        text = (
            "Package: foo\n"
            "Version: 1.0-1\n"
            "File: 5d41402abc4b2a76b9719d911017c592 200 foo_1.0-1.dsc\n"
        )
        path = write_index(tmp_path, "Sources")
        path.write_text(text, encoding="utf-8")
        with pytest.raises(am.AptMirrorError) as error:
            am.parse_index(path, TREE_PREFIX)
        assert str(error.value) == "apt-mirror: invalid Sources format"


class TestMalformedFileLine:
    """A File: line with a field count other than three is a hard error."""

    @pytest.mark.parametrize("line", ["abc 123", "a b c d"])
    def test_wrong_field_count_raises(self, am, tmp_path, line) -> None:
        text = (
            f"Package: foo\nVersion: 1.0-1\nDirectory: pool/main/x/foo\nFile: {line}\n"
        )
        path = write_index(tmp_path, "Sources")
        path.write_text(text, encoding="utf-8")
        with pytest.raises(am.AptMirrorError) as error:
            am.parse_index(path, TREE_PREFIX)
        assert str(error.value) == "apt-mirror: invalid Sources format"


class TestNeedUpdate:
    """need_update: absent -> True, same size -> False, wrong size ->
    True and the file is deleted afterwards."""

    def test_absent_file(self, am, tmp_path) -> None:
        assert am.need_update(tmp_path / "missing.deb", 100) is True

    def test_same_size_keeps_file(self, am, tmp_path) -> None:
        path = tmp_path / "ok.deb"
        path.write_bytes(b"x" * 128)
        assert am.need_update(path, 128) is False
        assert path.exists()

    def test_wrong_size_deletes_file(self, am, tmp_path) -> None:
        path = tmp_path / "stale.deb"
        path.write_bytes(b"x" * 64)
        assert am.need_update(path, 640) is True
        assert not path.exists()


class TestBlockSeparation:
    """Blocks are separated by a single blank line; colons in values
    (Homepage: http://x/) do not split a field."""

    def test_plain_blocks_and_colon_in_value(self, am, tmp_path) -> None:
        text = (
            "Package: alpha\n"
            "Filename: pool/main/a/alpha/alpha_1-1_all.deb\n"
            "Size: 40\n"
            "SHA1: " + "c" * 40 + "\n"
            "Homepage: http://x/\n"
            "\n"
            "Package: beta\n"
            "Filename: pool/main/b/beta/beta_1-1_all.deb\n"
            "Size: 41\n"
            f"MD5sum: {MD5_ALL}\n"
            "\n"
            "Title: some stray block\n"
        )
        path = write_index(tmp_path, "Packages")
        path.write_text(text, encoding="utf-8")

        blocks = list(am.iter_index_blocks(path))
        assert len(blocks) == 3  # alpha, beta and the stray Title block
        assert blocks[0]["Homepage"] == "http://x/"
        assert blocks[0]["Package"] == "alpha"

        records = am.parse_index(path, TREE_PREFIX)
        assert len(records) == 2  # the Title-only block is ignored
        alpha, beta = records
        assert alpha.url_rel == "pool/main/a/alpha/alpha_1-1_all.deb"
        assert alpha.md5 is None
        assert alpha.sha1 == "c" * 40
        assert alpha.sha256 is None
        assert beta.md5 == MD5_ALL
        assert beta.sha1 is None
        assert beta.sha256 is None
