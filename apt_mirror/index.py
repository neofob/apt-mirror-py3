"""Package index (Packages/Sources) parsing (spec §5.7)."""

import bz2
import gzip
import lzma
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from apt_mirror.errors import AptMirrorError
from apt_mirror.uri import remove_double_slashes

#: A field line of an index block: a column-0 ``Name:`` followed by the
#: raw remainder of the line (the value may itself contain colons).
_INDEX_FIELD_RE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+):(?P<value>.*)$")


@dataclass
class PackageRec:
    """One archive file named by a Packages or Sources index entry.

    ``mirror_rel`` is the path relative to the mirror tree root (no
    leading slash), already run through remove_double_slashes;
    ``url_rel`` is relative to the repo root, so its download URL is
    ``<uri>/`` + ``url_rel`` (the ``Filename:`` value for a Packages
    entry, ``<directory>/<filename>`` for a Sources one).  ``md5`` /
    ``sha1`` / ``sha256`` are None when the index field is absent.
    """

    mirror_rel: str
    size: int
    md5: str | None
    sha1: str | None
    sha256: str | None
    url_rel: str


def _read_index(path: Path) -> str:
    """Read the full text of index file ``path``, decompressing by suffix.

    ``.gz`` is decompressed with ``gzip``, ``.bz2`` with ``bz2``, and
    ``.lzma`` (or ``.xz``) with ``lzma``; any other suffix is read as
    plain text.  All decompression is in-process — the original's
    shell-out to external decompressors is not reproduced (spec §5.7).
    """
    suffix = path.suffix
    if suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    if suffix == ".bz2":
        with bz2.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    if suffix in (".lzma", ".xz"):
        with lzma.open(path, "rt", encoding="utf-8") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8")


def iter_index_blocks(path: Path) -> Iterator[dict[str, str]]:
    """Yield the blocks of index file ``path`` as field-name dicts.

    The file is first decompressed by its suffix (see _read_index).
    Blocks are the blank-line-separated chunks of the text; each becomes
    an insertion-ordered dict mapping every field name to its value.
    An indented continuation line and a repeated column-0 field name both
    extend the previous field's value, joined with ``\\n``; every value
    is stripped of surrounding whitespace — the net effect of the
    original's ``split(/^([\\w\\-]+:)/m)`` plus ``chomp`` plus
    ``remove_spaces``.  A colon inside a value (``Homepage: http://x/``)
    does not start a new field: only a column-0 ``Name:`` does.
    """
    block: dict[str, str] = {}
    current: str | None = None
    for raw_line in _read_index(path).split("\n"):
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            if block:
                yield block
                block = {}
                current = None
            continue
        match = _INDEX_FIELD_RE.match(line)
        if match is not None:
            current = match["name"]
            piece = match["value"].strip()
        elif current is None:
            continue
        else:
            piece = stripped
        if not piece:
            continue
        if block.get(current):
            block[current] = f"{block[current]}\n{piece}"
        else:
            block[current] = piece
    if block:
        yield block


def need_update(mirror_file: Path, size: int) -> bool:
    """Whether ``mirror_file`` must (re)download, and prune a corrupt one.

    True when the file is missing; False when it exists with exactly
    ``size`` bytes; and True when it exists with a different size, in
    which case the corrupt file is deleted first so the next download
    starts from scratch (spec §5.7).  The comparison is against the stat
    size (``st.st_size``, i.e. stat.S_SIZE) — the file is never read.
    """
    try:
        st_size = mirror_file.stat().st_size
    except OSError:
        return True
    if st_size == size:
        return False
    mirror_file.unlink(missing_ok=True)
    return True


def _index_size(raw: str | None) -> int:
    """An index ``Size:`` value as an int (absent or non-numeric -> 0)."""
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _packages_record(block: dict[str, str], tree_prefix: str) -> PackageRec:
    """The PackageRec of one ``Filename:`` (Packages index) block."""
    filename = block["Filename"]
    return PackageRec(
        mirror_rel=remove_double_slashes(f"{tree_prefix}/{filename}"),
        size=_index_size(block.get("Size")),
        md5=block.get("MD5sum"),
        sha1=block.get("SHA1"),
        sha256=block.get("SHA256"),
        url_rel=filename,
    )


def _sources_records(block: dict[str, str], tree_prefix: str) -> list[PackageRec]:
    """The PackageRecs of one Sources block, one per ``File:`` line.

    Each line must split into exactly three ``<hash> <size> <filename>``
    fields and the block must carry a ``Directory:`` (a leading slash is
    normalised away); anything else raises the pinned
    "apt-mirror: invalid Sources format" error.
    """
    directory = block.get("Directory")
    if directory is None:
        raise AptMirrorError("apt-mirror: invalid Sources format")
    directory = directory.lstrip("/")
    files = block.get("File") or block.get("Files")
    records: list[PackageRec] = []
    for line in (files or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 3:
            raise AptMirrorError("apt-mirror: invalid Sources format")
        digest, size_raw, filename = parts
        try:
            size = int(size_raw)
        except ValueError:
            raise AptMirrorError("apt-mirror: invalid Sources format") from None
        url_rel = f"{directory}/{filename}"
        records.append(
            PackageRec(
                mirror_rel=remove_double_slashes(f"{tree_prefix}/{url_rel}"),
                size=size,
                md5=digest,
                sha1=None,
                sha256=None,
                url_rel=url_rel,
            )
        )
    return records


def parse_index(path: Path, tree_prefix: str) -> list[PackageRec]:
    """Parse a Packages/Sources index file into archive records (spec §5.7).

    ``tree_prefix`` is the repo's path in the mirror tree (sanitise_uri of
    the repo URI).  A block with ``Filename:`` yields one Packages record
    (digests from the ``MD5sum:`` / ``SHA1:`` / ``SHA256:`` fields when
    present else None, ``size`` from ``Size:`` or 0 when the field is
    absent, ``url_rel`` the ``Filename:`` value itself); a block with
    ``File:`` (or ``Files:``) yields one Sources record per line.  Blocks
    with neither field are ignored (e.g. a stray metadata-only block).
    Raises AptMirrorError for malformed Sources content (see
    _sources_records).
    """
    records: list[PackageRec] = []
    for block in iter_index_blocks(path):
        if "Filename" in block:
            records.append(_packages_record(block, tree_prefix))
        elif "File" in block or "Files" in block:
            records.extend(_sources_records(block, tree_prefix))
    return records


#: Index file extensions tried when opening a skel index, in the original's
#: ``process_index`` order (``xz gz bz2 lzma``), the last entry being the
#: uncompressed file itself (spec §5.7).
_INDEX_EXTENSIONS = (".xz", ".gz", ".bz2", ".lzma", "")

#: Compression/decoding errors that mark a skel index as corrupt and
#: eligible for a one-shot re-fetch (spec §7.1 fix #4).
_INDEX_PARSE_ERRORS: tuple[type[BaseException], ...] = (
    OSError,  # gzip/bz2/lzma surface read failures here
    EOFError,
    UnicodeDecodeError,
    zlib.error,
    lzma.LZMAError,
)
