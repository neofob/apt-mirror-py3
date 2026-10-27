"""Release metadata parsing and index-URL generation (spec §5.6)."""

import logging
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from apt_mirror.config import Repo
from apt_mirror.uri import remove_double_slashes

logger = logging.getLogger("apt-mirror")


#: The per-distribution release files requested in stage 1 (spec §5.5).
_RELEASE_NAMES = ("InRelease", "Release", "Release.gpg")

#: Hash sections recognised in a Release file, strongest first (spec §5.6.3).
HASH_STRENGTH: tuple[str, ...] = ("SHA512", "SHA256", "SHA1", "MD5Sum")

# Compressed extensions a Release lists index files under.  ``KNOWN_EXTENSIONS``
# is additionally the order the no-Release well-known fallback tries them in
# (spec §5.6.1): .gz, .xz, .bz2, .lzma, then uncompressed.
_RELEASE_EXT_RE = r"\.(?:gz|bz2|xz|lzma)"
KNOWN_EXTENSIONS: tuple[str, ...] = (".gz", ".xz", ".bz2", ".lzma", "")

#: A generic "WORD:" line ending a header: any hash-section name,
#: supported or not (e.g. ``SHA384:``).  A Release may list files only
#: under sections we do not support; recording them (with the raw section
#: name) keeps the filenames visible so index_urls_for can warn and fall
#: back to the canonical location (spec §5.6.3).
_ANY_HASH_HEADER_RE = re.compile(r"^(?P<hash>[A-Za-z0-9]+):$")
#: A hash-section continuation line: leading whitespace plus its remainder.
_HASH_LINE_RE = re.compile(r"^ +(.*)$")


@dataclass
class HashEntry:
    """One ``<hashsum> <size> <filename>`` line of a Release hash section."""

    hashname: str
    hashsum: str
    size: int
    filename: str


@dataclass
class ReleaseInfo:
    """Parsed content of a Release or InRelease file (spec §5.6.2).

    ``entries`` maps a filename to every hash section it is listed in (one
    HashEntry per section, in file order); ``by_hash`` is True only for the
    exact field line ``Acquire-By-Hash: yes``; ``warnings`` holds one line
    per malformed checksum continuation.
    """

    by_hash: bool
    entries: dict[str, list[HashEntry]]
    warnings: list[str]


def parse_release(path: Path) -> ReleaseInfo:
    """Parse a Release or InRelease file into a ReleaseInfo.

    Recognises the ``SHA512:``, ``SHA256:``, ``SHA1:`` and ``MD5Sum:``
    group headers, each followed by ``<hashsum> <size> <filename>``
    continuation lines (3 whitespace-separated fields, spec §5.6.2).  A
    continuation line that does not split into exactly three fields (or
    whose size is not a whole number) appends the warning
    ``Malformed checksum line "<line>" in <path>`` and parsing continues —
    malformed content never raises.  ``by_hash`` is True only for the exact
    line ``Acquire-By-Hash: yes``; any other value (``no``, ``YES``, ...)
    means canonical downloads, like the original.
    """
    info = ReleaseInfo(by_hash=False, entries={}, warnings=[])
    current_hash: str | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        line = raw_line.rstrip("\r")
        if current_hash is not None:
            match = _HASH_LINE_RE.match(line)
            if match is not None:
                content = match.group(1)
                parts = content.split()
                entry: HashEntry | None = None
                if len(parts) == 3:
                    try:
                        size = int(parts[1])
                    except ValueError:
                        pass
                    else:
                        entry = HashEntry(current_hash, parts[0], size, parts[2])
                if entry is None:
                    info.warnings.append(
                        f'Malformed checksum line "{content}" in {path}'
                    )
                else:
                    info.entries.setdefault(entry.filename, []).append(entry)
                continue
            current_hash = None
        header = _ANY_HASH_HEADER_RE.match(line)
        if header is not None:
            current_hash = header.group("hash")
        elif line == "Acquire-By-Hash: yes":
            info.by_hash = True
    return info


@dataclass
class IndexUrl:
    """One index file queued for the stage-2 (index) download phase.

    ``filename`` is the path joined onto its base URL: for a
    Release-driven URL it is exactly the Release entry, relative to the
    dist root (e.g. ``main/binary-amd64/Packages.gz``); for the no-Release
    well-known set it is relative to the repo URI (e.g.
    ``dists/stable/main/binary-amd64/Packages.gz``).  ``by_hash`` is the
    name of the file's strongest advertised hash when an Acquire-By-Hash
    download applies, else None (a canonical download); it is the hash
    section name exactly as written in the Release file (``SHA512`` /
    ``SHA256`` / ``SHA1`` / ``MD5Sum``) and doubles as the ``by-hash/``
    directory segment (spec §5.6.3); ``by_hashsum`` is the matching digest.
    """

    filename: str
    size: int
    by_hash: str | None
    by_hashsum: str | None

    @property
    def by_hash_path(self) -> str:
        """The path part to fetch: the by-hash location, or the canonical one.

        Exactly ``posixpath.dirname(filename) + "/by-hash/" + by_hash + "/"
        + by_hashsum`` (spec §5.6.3) for a by-hash download, plain
        ``filename`` otherwise — so the absolute URL is the base URL
        (``dist_base`` for a Release-driven URL) plus this path.
        """
        if self.by_hash is None or self.by_hashsum is None:
            return self.filename
        return (
            f"{posixpath.dirname(self.filename)}/by-hash/"
            f"{self.by_hash}/{self.by_hashsum}"
        )


def dist_base(spec: Repo) -> str:
    """The absolute base URL of a repo's distribution tree (spec §5.6.1).

    ``<uri>/dists/<distribution>/`` for a normal repository and
    ``<uri>/<distribution>/`` for a flat one (``components`` is None),
    normalised with remove_double_slashes so a URI carrying a trailing
    slash does not double it.
    """
    if spec.components is None:
        return remove_double_slashes(f"{spec.uri}/{spec.distribution}/")
    return remove_double_slashes(f"{spec.uri}/dists/{spec.distribution}/")


def _alternation(names: list[str]) -> str:
    """A non-capturing ``(?:name1|name2)`` regex alternation over ``names``."""
    return "(?:" + "|".join(re.escape(name) for name in names) + ")"


def _index_patterns(spec: Repo) -> list[re.Pattern[str]]:
    """Whole-filename patterns selecting spec's index files (spec §5.6.2).

    Built from the original's pattern sets, anchored and whole-string
    matched (re.fullmatch): per-component and dist-root Contents,
    ``binary-<arch|all>/Packages`` + ``Release``, cnf, dep11, i18n for
    binary repos; ``source/Sources``, ``source/Release`` and
    ``Contents-source`` for source repos; flat-repo variants mirror only
    the single root ``Packages``/``Sources``.  The compression extension
    is optional: a Release may list an uncompressed index filename
    (``.../Packages``), and the stage-2/3 pipeline handles those like any
    other skel index (spec §5.7 opens the uncompressed file as-is).
    """
    ext = r"(?:" + _RELEASE_EXT_RE + ")?"
    if spec.components is None:
        names = ("Sources" + ext,) if spec.type == "deb-src" else ("Packages" + ext,)
        return [re.compile(name) for name in names]
    if spec.type == "deb-src":
        comp = _alternation(spec.components)
        patterns: tuple[str, ...] = (
            f"{comp}/source/Sources{ext}",
            f"{comp}/source/Release",
            f"{comp}/Contents-source{ext}",
        )
    else:
        arch = _alternation([part for part in spec.arch.split(",") if part] + ["all"])
        comp = _alternation(spec.components)
        patterns = (
            f"Contents-{arch}{ext}",
            f"{comp}/Contents-{arch}{ext}",
            f"{comp}/(?:debian-installer/)?binary-{arch}/Packages{ext}",
            f"{comp}/(?:debian-installer/)?binary-{arch}/Release",
            f"{comp}/cnf/Commands-{arch}{ext}",
            f"{comp}/dep11/Components-{arch}.*{ext}",
            f"{comp}/dep11/icons-.*{ext}",
            f"{comp}/i18n/Translation-.*{ext}",
        )
    return [re.compile(pattern) for pattern in patterns]


def _strongest_advertised(entries: list[HashEntry]) -> HashEntry | None:
    """The entry under the strongest hash section the file itself advertises.

    None when the file advertises no supported section at all.
    """
    by_name: dict[str, HashEntry] = {}
    for entry in entries:
        by_name.setdefault(entry.hashname, entry)
    for name in HASH_STRENGTH:
        candidate = by_name.get(name)
        if candidate is not None:
            return candidate
    return None


def index_urls_for(spec: Repo, info: ReleaseInfo) -> list[IndexUrl]:
    """The stage-2 index URLs to fetch for ``spec`` (spec §5.6.2, §5.6.3).

    Filters ``info.entries``' filenames against the pattern set for the
    repo's type, architecture and components and returns one IndexUrl per
    matching filename, deduplicated by (filename, by_hash).  When the
    Release requests ``Acquire-By-Hash: yes`` each file is fetched via the
    strongest hash *it itself* advertises (spec §7.1 fix: files listed
    only under a weaker section are still mirrored, not silently dropped);
    a filename with no advertised entries — and every file when the
    request is absent or not exactly ``yes`` — falls back to the canonical
    path (``by_hash is None``).  A file listed only under sections we do
    not support (e.g. SHA384) likewise falls back to canonical, but
    raises a warning naming the file (spec §5.6.3).
    """
    patterns = _index_patterns(spec)
    urls: list[IndexUrl] = []
    seen: set[tuple[str, str | None]] = set()
    for filename, entries in info.entries.items():
        if not any(pattern.fullmatch(filename) for pattern in patterns):
            continue
        strongest = _strongest_advertised(entries)
        if info.by_hash and strongest is not None:
            url = IndexUrl(
                filename=filename,
                size=strongest.size,
                by_hash=strongest.hashname,
                by_hashsum=strongest.hashsum,
            )
        else:
            if info.by_hash and strongest is None:
                logger.warning(
                    "apt-mirror: no supported hash (SHA512/SHA256/SHA1/MD5Sum)"
                    " for %s; falling back to its canonical location",
                    filename,
                )
            url = IndexUrl(
                filename=filename,
                size=strongest.size if strongest is not None else 0,
                by_hash=None,
                by_hashsum=None,
            )
        key = (url.filename, url.by_hash)
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def _known_base_paths(spec: Repo) -> list[str]:
    """Well-known index base paths (no extension) for ``spec``, deduped.

    Relative to the repo URI, mirroring the original's no-Release loop:
    per-component plus dist-root Contents for a normal binary repo,
    per-component ``source/Sources`` and ``Contents-source`` for a source
    repo (under ``dists/<distribution>`` — the original omitted that
    segment for source lines and never mirrored them, spec §7.1 fix #1),
    and flat variants with no ``dists/`` segment at all.  A comma-
    separated ``arch`` list (spec §5.1: ``arch=a,b`` is honoured) yields
    one ``binary-<arch>/Packages`` / ``Contents-<arch>`` base per arch,
    while ``binary-all`` / ``Contents-all`` appear exactly once.
    """
    dist = spec.distribution
    if spec.type == "deb-src":
        if spec.components is None:
            return [f"{dist}/Sources"]
        bases = [f"dists/{dist}/{comp}/source/Sources" for comp in spec.components]
        bases += [f"dists/{dist}/{comp}/Contents-source" for comp in spec.components]
    else:
        archs = [arch for arch in spec.arch.split(",") if arch]
        if spec.components is None:
            return (
                [f"{dist}/Packages"]
                + [f"{dist}/Contents-{arch}" for arch in archs]
                + [f"{dist}/Contents-all"]
            )
        bases = []
        for comp in spec.components:
            bases += [f"dists/{dist}/{comp}/binary-{arch}/Packages" for arch in archs]
            bases.append(f"dists/{dist}/{comp}/binary-all/Packages")
            bases += [f"dists/{dist}/{comp}/Contents-{arch}" for arch in archs]
            bases.append(f"dists/{dist}/{comp}/Contents-all")
            bases += [f"dists/{dist}/Contents-{arch}" for arch in archs]
            bases.append(f"dists/{dist}/Contents-all")
    return list(dict.fromkeys(bases))


def known_index_urls(spec: Repo) -> list[IndexUrl]:
    """The unsigned-repo fallback index set (spec §5.6.1, §7.1 fix #1).

    Used when neither InRelease nor Release could be downloaded: the
    well-known index paths, each once per extension in the order ``.gz
    .xz .bz2 .lzma`` and then uncompressed, all canonical
    (``by_hash is None``).  Filenames are repo-URI-relative; a normal
    source repo keeps the ``dists/<distribution>`` segment and a flat
    source repo is simply ``<distribution>/Sources``.
    """
    urls: list[IndexUrl] = []
    seen: set[str] = set()
    for base in _known_base_paths(spec):
        for extension in KNOWN_EXTENSIONS:
            filename = base + extension
            if filename in seen:
                continue
            seen.add(filename)
            urls.append(
                IndexUrl(filename=filename, size=0, by_hash=None, by_hashsum=None)
            )
    return urls
