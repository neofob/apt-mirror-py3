"""Tests for Release/InRelease parsing and index-URL generation (Task 5).

These are filesystem-only: a realistic multi-section Release is hand-written
into ``tmp_path`` and fed to ``parse_release``.  No network access is needed.

The pins come from the task brief (step 1) and, where the brief is silent,
from the original Perl ``find_metadata_in_release`` plus the spec §5.6 /
§7.1 behaviour:

- ``parse_release`` groups the four hash sections and aggregates, per
  filename, every section it appears in; a continuation line with a
  different field count is a warning, never an exception.
- ``Acquire-By-Hash: yes`` enables by-hash only on the *exact* value.
- ``index_urls_for`` filters the Release entries by the §5.6 pattern sets
  and downloads each file via the strongest hash *that file itself*
  advertises (spec §7.1 bug fix #2); a file with no advertised hash falls
  back to the canonical path.
- ``known_index_urls`` is the no-Release well-known-set fallback: source
  lines include the ``/dists/`` segment (spec §7.1 bug fix #1) and a flat
  source repo uses ``<dist>/Sources`` with neither ``dists/`` nor
  ``source/``.
"""

import logging

import pytest

# Distinct, well-formed digests for each of the four sections.
MD5 = "0" * 32
SHA1_MAIN = "1" * 40
SHA1_CONTRIB = "4" * 40
SHA256_MAIN = "2" * 64
SHA512_MAIN = "3" * 128

BINARY_SPEC_KW = {"uri": "http://example.com/debian", "distribution": "stable"}


def make_release(by_hash_line, names, size=100):
    """Return Release text: one SHA256 section listing ``names``."""
    lines = ["Origin: Test", "Suite: stable"]
    if by_hash_line is not None:
        lines.append(by_hash_line)
    lines.append("SHA256:")
    for name in names:
        lines.append(f"  {'5' * 64} {size} {name}")
    return "\n".join(lines) + "\n"


def multi_release(by_hash_line):
    """A Release listing two files across all four hash sections.

    The ``main`` file appears in every section (strongest = SHA512); the
    ``contrib`` file appears only under SHA1 (the per-file-strongest case
    that the original silently dropped).
    """
    lines = [
        "Origin: Test",
        "Suite: stable",
        by_hash_line,
        "MD5Sum:",
        f"  {MD5} 1000 main/binary-amd64/Packages.gz",
        "SHA1:",
        f"  {SHA1_MAIN} 1000 main/binary-amd64/Packages.gz",
        f"  {SHA1_CONTRIB} 2000 contrib/binary-amd64/Packages.gz",
        "SHA256:",
        f"  {SHA256_MAIN} 1000 main/binary-amd64/Packages.gz",
        "SHA512:",
        f"  {SHA512_MAIN} 1000 main/binary-amd64/Packages.gz",
    ]
    return "\n".join(str(x) for x in lines) + "\n"


def write_release(tmp_path, text, name="InRelease"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def binary_spec(am, components=None, **overrides):
    kw = {"type": "deb", "arch": "amd64"}
    kw.update(BINARY_SPEC_KW)
    kw["components"] = components
    kw.update(overrides)
    return am.Repo(**kw)


class TestParseReleaseSections:
    """parse_release groups all four hash sections per filename."""

    def test_all_four_sections_aggregate(self, am, tmp_path) -> None:
        path = write_release(tmp_path, multi_release("Acquire-By-Hash: yes"))
        info = am.parse_release(path)
        main = info.entries["main/binary-amd64/Packages.gz"]
        assert [e.hashname for e in main] == ["MD5Sum", "SHA1", "SHA256", "SHA512"]
        assert {e.size for e in main} == {1000}
        assert main[0].hashsum == MD5

    def test_sha1_only_file_has_single_entry(self, am, tmp_path) -> None:
        path = write_release(tmp_path, multi_release("Acquire-By-Hash: yes"))
        info = am.parse_release(path)
        contrib = info.entries["contrib/binary-amd64/Packages.gz"]
        assert len(contrib) == 1
        assert contrib[0].hashname == "SHA1"
        assert contrib[0].hashsum == SHA1_CONTRIB

    def test_no_hash_header_gives_empty_entries(self, am, tmp_path) -> None:
        path = write_release(tmp_path, "Origin: Test\nAcquire-By-Hash: no\n")
        info = am.parse_release(path)
        assert info.entries == {}
        assert info.warnings == []


class TestParseReleaseByHashFlag:
    """Acquire-By-Hash is honoured on the exact value ``yes`` only."""

    @pytest.mark.parametrize(
        "value, expected",
        [("yes", True), ("no", False), ("YES", False), ("Yes", False), ("yes ", False)],
    )
    def test_exact_value(self, am, tmp_path, value, expected) -> None:
        path = write_release(
            tmp_path,
            make_release(
                f"Acquire-By-Hash: {value}", ["main/binary-amd64/Packages.gz"]
            ),
        )
        assert am.parse_release(path).by_hash is expected

    def test_missing_flag_defaults_false(self, am, tmp_path) -> None:
        path = write_release(
            tmp_path, make_release(None, ["main/binary-amd64/Packages.gz"])
        )
        assert am.parse_release(path).by_hash is False


class TestParseReleaseMalformed:
    """A continuation line with the wrong field count warns and continues."""

    def test_malformed_continuation_warns_and_continues(self, am, tmp_path) -> None:
        text = (
            "Acquire-By-Hash: yes\n"
            "SHA1:\n"
            f"  {SHA1_MAIN} 1000 main/binary-amd64/Packages.gz\n"
            "  deadbeef 123 name extra\n"
            f"  {SHA1_CONTRIB} 2000 contrib/binary-amd64/Packages.gz\n"
        )
        path = write_release(tmp_path, text)
        info = am.parse_release(path)
        assert info.warnings == [
            f'Malformed checksum line "deadbeef 123 name extra" in {path}'
        ]
        # Other entries parsed cleanly.
        assert "main/binary-amd64/Packages.gz" in info.entries
        assert "contrib/binary-amd64/Packages.gz" in info.entries


class TestIndexUrlsByHash:
    """index_urls_for picks each file's own strongest advertised hash."""

    def test_strongest_hash_picked(self, am, tmp_path) -> None:
        path = write_release(tmp_path, multi_release("Acquire-By-Hash: yes"))
        info = am.parse_release(path)
        spec = binary_spec(am, components=["main", "contrib"])
        urls = {u.filename: u for u in am.index_urls_for(spec, info)}
        main = urls["main/binary-amd64/Packages.gz"]
        assert main.by_hash == "sha512"
        assert main.by_hashsum == SHA512_MAIN

    def test_per_file_strongest_sha1_only(self, am, tmp_path) -> None:
        # spec §7.1 fix #2: a file listed only under SHA1 is still mirrored.
        path = write_release(tmp_path, multi_release("Acquire-By-Hash: yes"))
        info = am.parse_release(path)
        spec = binary_spec(am, components=["main", "contrib"])
        urls = {u.filename: u for u in am.index_urls_for(spec, info)}
        contrib = urls["contrib/binary-amd64/Packages.gz"]
        assert contrib.by_hash == "sha1"
        assert contrib.by_hashsum == SHA1_CONTRIB

    def test_by_hash_no_all_canonical(self, am, tmp_path) -> None:
        path = write_release(tmp_path, multi_release("Acquire-By-Hash: no"))
        info = am.parse_release(path)
        spec = binary_spec(am, components=["main", "contrib"])
        urls = am.index_urls_for(spec, info)
        assert urls
        assert all(u.by_hash is None and u.by_hashsum is None for u in urls)

    def test_no_hash_entries_falls_back_to_canonical(self, am) -> None:
        spec = binary_spec(am, components=["main"])
        info = am.ReleaseInfo(
            by_hash=True,
            entries={"main/binary-amd64/Packages.gz": []},
            warnings=[],
        )
        urls = am.index_urls_for(spec, info)
        assert len(urls) == 1
        assert urls[0].by_hash is None
        assert urls[0].by_hashsum is None
        assert urls[0].filename == "main/binary-amd64/Packages.gz"


class TestByHashPath:
    """IndexUrl.by_hash_path is the exact by-hash or canonical path form."""

    def test_by_hash_path_form(self, am, tmp_path) -> None:
        path = write_release(tmp_path, multi_release("Acquire-By-Hash: yes"))
        info = am.parse_release(path)
        spec = binary_spec(am, components=["main", "contrib"])
        urls = {u.filename: u for u in am.index_urls_for(spec, info)}
        main = urls["main/binary-amd64/Packages.gz"]
        assert main.by_hash_path == f"main/binary-amd64/by-hash/sha512/{SHA512_MAIN}"
        base = am.dist_base(spec)
        assert base + main.by_hash_path == (
            f"http://example.com/debian/dists/stable/main/binary-amd64/"
            f"by-hash/sha512/{SHA512_MAIN}"
        )

    def test_canonical_path_is_filename(self, am) -> None:
        spec = binary_spec(am, components=["main"])
        info = am.ReleaseInfo(
            by_hash=False,
            entries={
                "main/binary-amd64/Packages.gz": [
                    am.HashEntry(
                        "SHA256", "5" * 64, 100, "main/binary-amd64/Packages.gz"
                    )
                ]
            },
            warnings=[],
        )
        url = am.index_urls_for(spec, info)[0]
        assert url.by_hash is None
        assert url.by_hash_path == url.filename
        assert am.dist_base(spec) + url.by_hash_path == (
            "http://example.com/debian/dists/stable/main/binary-amd64/Packages.gz"
        )


BINARY_NAMES = [
    "Contents-amd64.gz",
    "Contents-all.gz",
    "main/Contents-amd64.gz",
    "main/Contents-all.gz",
    "main/binary-amd64/Packages.gz",
    "main/binary-all/Packages.gz",
    "main/binary-amd64/Release",
    "main/cnf/Commands-amd64.gz",
    "main/dep11/Components-amd64.yaml.xz",
    "main/dep11/icons-48x48.tar.xz",
    "main/i18n/Translation-en.gz",
    # Excluded by the pattern set:
    "main/Contents-powerpc.gz",
    "main/binary-i386/Packages.gz",
    "main/binary-amd64/Contents-foo",
]

BINARY_INCLUDED = BINARY_NAMES[:-3]
BINARY_EXCLUDED = BINARY_NAMES[-3:]


class TestIndexUrlPatterns:
    """index_urls_for selects exactly the §5.6 pattern set for a repo."""

    def test_binary_included(self, am, tmp_path) -> None:
        path = write_release(
            tmp_path, make_release("Acquire-By-Hash: no", BINARY_NAMES)
        )
        info = am.parse_release(path)
        spec = binary_spec(am, components=["main", "contrib"])
        names = {u.filename for u in am.index_urls_for(spec, info)}
        for name in BINARY_INCLUDED:
            assert name in names, f"expected {name!r} to be included"

    def test_binary_excluded(self, am, tmp_path) -> None:
        path = write_release(
            tmp_path, make_release("Acquire-By-Hash: no", BINARY_NAMES)
        )
        info = am.parse_release(path)
        spec = binary_spec(am, components=["main", "contrib"])
        names = {u.filename for u in am.index_urls_for(spec, info)}
        for name in BINARY_EXCLUDED:
            assert name not in names, f"expected {name!r} to be excluded"

    def test_source_included_and_excluded(self, am, tmp_path) -> None:
        names = [
            "main/source/Sources.gz",
            "main/source/Release",
            "main/Contents-source.gz",
            "main/binary-amd64/Packages.gz",  # excluded for a source repo
        ]
        path = write_release(tmp_path, make_release("Acquire-By-Hash: no", names))
        info = am.parse_release(path)
        spec = am.Repo(
            type="deb-src",
            arch="amd64",
            uri="http://example.com/debian",
            distribution="stable",
            components=["main"],
        )
        urls = {u.filename: u for u in am.index_urls_for(spec, info)}
        assert "main/source/Sources.gz" in urls
        assert "main/source/Release" in urls
        assert "main/Contents-source.gz" in urls
        assert "main/binary-amd64/Packages.gz" not in urls


class TestDistBase:
    """dist_base is <uri>/dists/<dist>/ or flat <uri>/<dist>/, normalised."""

    def test_dists_layout(self, am) -> None:
        spec = binary_spec(am, components=["main"])
        assert am.dist_base(spec) == "http://example.com/debian/dists/stable/"

    def test_flat_layout(self, am) -> None:
        spec = binary_spec(am)
        assert am.dist_base(spec) == "http://example.com/debian/stable/"

    def test_trailing_slash_uri_collapses(self, am) -> None:
        spec = binary_spec(am, components=["main"], uri="http://example.com/debian/")
        assert am.dist_base(spec) == "http://example.com/debian/dists/stable/"


KNOWN_EXTENSIONS = (".gz", ".xz", ".bz2", ".lzma", "")


class TestKnownIndexUrls:
    """known_index_urls is the no-Release well-known-set fallback."""

    def test_binary_all_five_extensions_in_order(self, am) -> None:
        spec = binary_spec(am, components=["main"])
        urls = am.known_index_urls(spec)
        by_file = {u.filename for u in urls}
        base = "dists/stable/main/binary-amd64/Packages"
        for ext in KNOWN_EXTENSIONS:
            assert f"{base}{ext}" in by_file
        # Extension order .gz .xz .bz2 .lzma "" is preserved for the file.
        order = [u.filename for u in urls]
        indices = [order.index(f"{base}{ext}") for ext in KNOWN_EXTENSIONS]
        assert indices == sorted(indices)

    def test_source_includes_dists_segment(self, am) -> None:
        spec = am.Repo(
            type="deb-src",
            arch="amd64",
            uri="http://example.com/debian",
            distribution="stable",
            components=["main"],
        )
        by_file = {u.filename for u in am.known_index_urls(spec)}
        base = "dists/stable/main/source/Sources"
        for ext in KNOWN_EXTENSIONS:
            assert f"{base}{ext}" in by_file

    def test_flat_source_no_dists_no_source(self, am) -> None:
        spec = am.Repo(
            type="deb-src",
            arch="amd64",
            uri="http://example.com/debian",
            distribution="stable",
            components=None,
        )
        urls = am.known_index_urls(spec)
        by_file = {u.filename for u in urls}
        base = "stable/Sources"
        for ext in KNOWN_EXTENSIONS:
            assert f"{base}{ext}" in by_file
        for name in by_file:
            assert "dists/" not in name
            assert "source/" not in name

    def test_all_by_hash_none(self, am) -> None:
        spec = binary_spec(am, components=["main"])
        urls = am.known_index_urls(spec)
        assert urls
        assert all(u.by_hash is None and u.by_hashsum is None for u in urls)


class TestUnsupportedHashSection:
    """A by-hash Release whose file is listed only under an UNSUPPORTED
    section (spec §5.6.3: warn and fall back to canonical downloads)."""

    def test_unsupported_section_still_records_the_file(self, am, tmp_path) -> None:
        text = (
            "Origin: Test\n"
            "Acquire-By-Hash: yes\n"
            "SHA384:\n"
            f"  {'7' * 96} 1000 main/binary-amd64/Packages.gz\n"
        )
        path = write_release(tmp_path, text)
        info = am.parse_release(path)
        entries = info.entries["main/binary-amd64/Packages.gz"]
        assert len(entries) == 1
        assert entries[0].hashname == "SHA384"
        assert entries[0].size == 1000

    def test_unsupported_hash_warns_and_falls_back_to_canonical(
        self, am, tmp_path, caplog
    ) -> None:
        text = (
            "Origin: Test\n"
            "Acquire-By-Hash: yes\n"
            "SHA384:\n"
            f"  {'7' * 96} 1000 main/binary-amd64/Packages.gz\n"
        )
        path = write_release(tmp_path, text)
        info = am.parse_release(path)
        spec = binary_spec(am, components=["main"])
        with caplog.at_level(logging.WARNING, logger="apt-mirror"):
            urls = am.index_urls_for(spec, info)
        assert len(urls) == 1
        # Canonical download: the by-hash fields stay None.
        assert urls[0].filename == "main/binary-amd64/Packages.gz"
        assert urls[0].by_hash is None
        assert urls[0].by_hashsum is None
        # And the warning names the file.
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert any(
            "main/binary-amd64/Packages.gz" in warning for warning in warnings
        ), warnings

    def test_unsupported_hash_no_warning_when_by_hash_off(self, am, tmp_path, caplog):
        # Without Acquire-By-Hash the canonical path is normal: no warning.
        text = (
            f"Origin: Test\nSHA384:\n  {'7' * 96} 1000 main/binary-amd64/Packages.gz\n"
        )
        path = write_release(tmp_path, text)
        info = am.parse_release(path)
        spec = binary_spec(am, components=["main"])
        with caplog.at_level(logging.WARNING, logger="apt-mirror"):
            urls = am.index_urls_for(spec, info)
        assert len(urls) == 1
        assert urls[0].by_hash is None
        assert caplog.records == []


class TestNonUtf8Release:
    """Release bytes that are not valid UTF-8 never raise (they are read
    with errors='replace' and parse like the damaged text they become)."""

    def test_raw_byte_in_release_never_raises(self, am, tmp_path) -> None:
        path = tmp_path / "InRelease"
        path.write_bytes(
            b"Origin: Test\n"
            b"Acquire-By-Hash: yes\n"
            b"SHA512:\n"
            + b" "
            + b"3" * 128
            + b" 1000 main/binary-amd64/Packages.gz"
            + b"\n\xff\xfe junk\n"
        )
        info = am.parse_release(path)  # must not raise
        assert "main/binary-amd64/Packages.gz" in info.entries
        assert info.by_hash is True


class TestKnownBasePathsMultiArch:
    """_known_base_paths iterates every ``arch=a,b`` entry (spec §5.1):
    per-arch Contents/binary, but Contents-all/binary-all exactly once."""

    def test_components_multi_arch(self, am) -> None:
        spec = binary_spec(am, components=["main", "contrib"], arch="amd64,i386")
        bases = set(am._known_base_paths(spec))
        for comp in ("main", "contrib"):
            assert f"dists/stable/{comp}/binary-amd64/Packages" in bases
            assert f"dists/stable/{comp}/binary-i386/Packages" in bases
            assert f"dists/stable/{comp}/binary-all/Packages" in bases
            assert f"dists/stable/{comp}/Contents-amd64" in bases
            assert f"dists/stable/{comp}/Contents-i386" in bases
            assert f"dists/stable/{comp}/Contents-all" in bases
        # Dist-root Contents are not per-component.
        assert "dists/stable/Contents-amd64" in bases
        assert "dists/stable/Contents-i386" in bases
        assert "dists/stable/Contents-all" in bases
        # No raw " amd64,i386" interpolation anywhere.
        for base in bases:
            assert ",i386" not in base and "amd64,i386" not in base

    def test_flat_multi_arch(self, am) -> None:
        spec = binary_spec(am, components=None, arch="amd64,i386")
        bases = set(am._known_base_paths(spec))
        assert bases == {
            "stable/Packages",
            "stable/Contents-amd64",
            "stable/Contents-i386",
            "stable/Contents-all",
        }

    def test_single_arch_order_unchanged(self, am) -> None:
        spec = binary_spec(am, components=["main"])
        assert am._known_base_paths(spec) == [
            "dists/stable/main/binary-amd64/Packages",
            "dists/stable/main/binary-all/Packages",
            "dists/stable/main/Contents-amd64",
            "dists/stable/main/Contents-all",
            "dists/stable/Contents-amd64",
            "dists/stable/Contents-all",
        ]
        flat = binary_spec(am, components=None)
        assert am._known_base_paths(flat) == [
            "stable/Packages",
            "stable/Contents-amd64",
            "stable/Contents-all",
        ]
