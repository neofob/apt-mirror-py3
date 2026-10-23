#!/usr/bin/env python3
"""apt-mirror for Python: mirror apt repositories in pure stdlib Python 3.

A clean reimplementation of the classic Perl apt-mirror.  It mirrors apt
repositories (release, index and archive files) into a local mirror tree,
manages the skel and mirror directories, and generates a cleanup script.
It has no third-party runtime dependencies and no external tool
requirements: fetching and decompression happen in-process.

Usage:
    apt-mirror.py [configfile]

Without an argument the tool uses /etc/apt/mirror.list.  The config file
is line-oriented:

    set <key> [<value>]        set a config variable (value may be quoted)
    deb <uri> <dist> <comps>   mirror a binary repository
    deb-src <uri> <dist> ...   mirror a source repository
    clean <uri>                directory key the cleanup covers
    skip-clean <uri>           directory key excluded from the cleanup

``#`` comment lines and blank lines are ignored.
"""

import argparse
import bz2
import contextlib
import gzip
import logging
import lzma
import os
import posixpath
import queue
import re
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from apt_mirror.config import (
    DEFAULT_CONFIG,
    MAX_EXPANSION_PASSES,
    Repo,
    _config_on,
    detect_defaultarch,
    get_var,
    parse_config,
    parse_config_line,
    parse_rate,
)
from apt_mirror.errors import AptMirrorError
from apt_mirror.fetch import Fetcher, FetchError, FetchNotFound, RateLimiter
from apt_mirror.release import (
    _RELEASE_NAMES,
    HASH_STRENGTH,
    HashEntry,
    IndexUrl,
    ReleaseInfo,
    _known_base_paths,
    dist_base,
    index_urls_for,
    known_index_urls,
    parse_release,
)
from apt_mirror.uri import remove_double_slashes, sanitise_uri
from apt_mirror.util import (
    _quoted_path,
    _strip_userinfo,
    copy_file,
    format_bytes,
    locked,
    round_number,
)

#: Re-exports from ``apt_mirror`` that keep this module's test interface
#: (tests load it as the ``aptmirror`` module) stable; mirror
#: ``apt_mirror/__init__``.
__all__ = [
    "DEFAULT_CONFIG",
    "MAX_EXPANSION_PASSES",
    "AptMirrorError",
    "FetchError",
    "FetchNotFound",
    "Fetcher",
    "HashEntry",
    "IndexUrl",
    "RateLimiter",
    "ReleaseInfo",
    "Repo",
    "_known_base_paths",
    "copy_file",
    "detect_defaultarch",
    "dist_base",
    "format_bytes",
    "get_var",
    "index_urls_for",
    "known_index_urls",
    "locked",
    "parse_config",
    "parse_config_line",
    "parse_rate",
    "parse_release",
    "remove_double_slashes",
    "round_number",
    "sanitise_uri",
]

logger = logging.getLogger("apt-mirror")


# ---------------------------------------------------------------------------
# Stage download orchestration (spec §5.3 concurrency, §5.11 output)
# ---------------------------------------------------------------------------

#: Wall-clock format for the ``Begin time`` / ``End time`` progress lines.
_TIME_FORMAT = "%a %b %e %H:%M:%S %Y %Z"


@dataclass
class StageResult:
    """Outcome of one download stage.

    ``ok`` counts the URLs that fetched cleanly, ``failed`` holds one
    ``(url, reason)`` pair per URL that could not be fetched, and ``bytes``
    is the total number of bytes written on this call.  URLs and reasons
    are in the credential-free form, safe to display or persist.
    """

    ok: int
    failed: list[tuple[str, str]]
    bytes: int


def download_stage(
    stage: str,
    urls: list[str],
    dest_root: Path,
    fetcher: Fetcher,
    variables: dict[str, str],
    var_path: Path,
) -> StageResult:
    """Download one stage's URLs into ``dest_root`` on a worker pool.

    Prints ``Downloading <N> files using <M> threads...`` with
    ``M = min(nthreads, len(urls))``, then ``Begin time: <ts>``; one
    ``[done/total]`` line per completed URL; then ``End time: <ts>`` (both
    timestamps in the fixed ``%a %b %e %H:%M:%S %Y %Z`` format); and
    finally the summary ``<ok> ok, <failed> failed, <bytes> bytes
    transferred`` followed by one ``  FAIL <url> (<reason>)`` line per
    failed URL.  Persists ``<var_path>/<stage>-urls.txt`` (all queued URLs,
    one per line, sorted) and ``<var_path>/<stage>-log.txt`` (one
    ``OK <url> <bytes>`` or ``FAIL <url> <reason>`` line per URL).  Every
    URL displayed or written here is in the credential-stripped form
    (``userinfo@`` removed); the fetcher itself receives the full URL.
    Each URL lands at ``dest_root / sanitise_uri(url, _tilde, _plus)``,
    with ``_tilde`` / ``_plus`` read via ``get_var``.

    The workers pull URLs from a shared queue (work-stealing, spec §5.3).
    FetchError / FetchNotFound become a failed entry and the worker
    continues; a fatal AptMirrorError (e.g. a bad scheme) propagates out of
    download_stage.  A stage with no URLs returns an empty StageResult and
    prints and writes nothing.
    """
    total = len(urls)
    if total == 0:
        return StageResult(ok=0, failed=[], bytes=0)
    raw = get_var(variables, "nthreads")
    try:
        nthreads = int(raw)
    except ValueError as error:
        raise AptMirrorError(f"apt-mirror: invalid nthreads '{raw}'") from error
    if nthreads < 1:
        raise AptMirrorError(f"apt-mirror: invalid nthreads '{raw}' (must be >= 1)")
    n_workers = min(nthreads, total)
    tilde = _config_on(get_var(variables, "_tilde"))
    plus = _config_on(get_var(variables, "_plus"))
    print(f"Downloading {total} files using {n_workers} threads...")
    print(f"Begin time: {time.strftime(_TIME_FORMAT, time.localtime())}")

    url_queue: queue.Queue[str] = queue.Queue()
    for url in urls:
        url_queue.put(url)
    lock = threading.Lock()
    done = 0
    results: dict[str, tuple[str, int | str]] = {}

    def worker() -> None:
        """Fetch URLs off the shared queue until it is drained."""
        nonlocal done
        while True:
            try:
                url = url_queue.get_nowait()
            except queue.Empty:
                return
            display = _strip_userinfo(url)
            try:
                written = fetcher.fetch(url, dest_root / sanitise_uri(url, tilde, plus))
            except FetchError as error:
                entry = ("FAIL", error.reason)
            else:
                entry = ("OK", written)
            with lock:
                results[display] = entry
                done += 1
                print(f"[{done}/{total}]")

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        # Submit every worker up front: awaiting a future while the
        # generator expression is still being consumed would run the
        # workers one at a time, not concurrently (spec §5.3).
        futures = [pool.submit(worker) for _ in range(n_workers)]
        for future in futures:
            future.result()
    print(f"End time: {time.strftime(_TIME_FORMAT, time.localtime())}")

    ok = 0
    byte_count = 0
    failed: list[tuple[str, str]] = []
    for display, (status, value) in results.items():
        if status == "OK":
            ok += 1
            byte_count += value
        else:
            failed.append((display, str(value)))
    failed.sort()

    var_path.mkdir(parents=True, exist_ok=True)
    (var_path / f"{stage}-urls.txt").write_text(
        "".join(f"{display}\n" for display in sorted(results)), encoding="utf-8"
    )
    log_lines = [
        f"{status} {display} {value}"
        for display, (status, value) in sorted(results.items())
    ]
    (var_path / f"{stage}-log.txt").write_text(
        "".join(f"{line}\n" for line in log_lines), encoding="utf-8"
    )
    print(f"{ok} ok, {len(failed)} failed, {byte_count} bytes transferred")
    for display, reason in failed:
        print(f"  FAIL {display} ({reason})")
    return StageResult(ok, failed, byte_count)


# ---------------------------------------------------------------------------
# Index (Packages/Sources) processing (spec §5.7)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Skel-to-mirror copy, cleanup, locking, postmirror, and main (spec §5.8-5.11)
# ---------------------------------------------------------------------------


@dataclass
class Unneeded:
    """The files/directories the cleanup removes (spec §5.9).

    ``files`` and ``dirs`` hold mirror-tree-relative POSIX paths (files in
    walk order; directories bottom-up, so a parent is listed only once its
    children are gone); ``nbytes`` totals the removed files' on-disk size
    in 512-byte block units.
    """

    files: list[str]
    dirs: list[str]
    nbytes: int


def collect_unneeded(
    mirror_root: Path,
    clean_dirs: list[str],
    skipclean: set[str],
    tilde: bool,
    plus: bool,
) -> Unneeded:
    """Walk each clean directory and collect what the cleanup removes.

    ``clean_dirs`` are mirror-root-relative trees (the config ``clean``
    URIs, sanitised).  A file is needed when its tree-relative path, after
    the ``_tilde``/``_plus`` normalisation (``~`` -> ``%7E``, ``+`` ->
    ``%2B``), is in ``skipclean``; a directory is needed when it (or any
    path under it) is needed — **symlinks are always considered live**, so
    a directory containing only symlinks is kept.  Non-needed files are
    listed for removal, a directory is listed only once every path under
    it is removable (bottom-up), and removed-file sizes are summed in
    512-byte block units (``st_blocks * 512``).  A missing or
    non-directory (or symlinked) clean root is skipped silently.
    """

    def key(rel: str) -> str:
        key = rel
        if tilde:
            key = key.replace("~", "%7E")
        if plus:
            key = key.replace("+", "%2B")
        return key

    files: list[str] = []
    dirs: list[str] = []
    nbytes = 0

    def process_file(rel: str) -> bool:
        nonlocal nbytes
        if key(rel) in skipclean:
            return True
        files.append(sanitise_uri(rel, tilde=tilde, plus=plus))
        with contextlib.suppress(OSError):
            nbytes += (mirror_root / rel).stat().st_blocks * 512
        return False

    def process_directory(rel: str) -> bool:
        if key(rel) in skipclean:
            return True
        needed = False
        try:
            entries = sorted(os.scandir(mirror_root / rel), key=lambda e: e.name)
        except OSError as error:
            raise AptMirrorError(f"apt-mirror: can't opendir {rel}") from error
        for entry in entries:
            sub = f"{rel}/{entry.name}"
            if entry.is_symlink():
                # Symlinks are always considered live (original
                # process_symlink) and are never followed.
                needed = True
            elif entry.is_dir(follow_symlinks=False):
                # Always recurse — the original's ``|=`` never
                # short-circuits, so a needed sibling must not skip the
                # walk of the remaining entries.
                if process_directory(sub):
                    needed = True
            elif entry.is_file(follow_symlinks=False) and process_file(sub):
                needed = True
        if not needed:
            dirs.append(rel)
        return needed

    for clean_dir in clean_dirs:
        path = mirror_root / clean_dir
        if path.is_dir() and not path.is_symlink():
            process_directory(clean_dir)
    return Unneeded(files=files, dirs=dirs, nbytes=nbytes)


def write_cleanscript(
    path: Path,
    mirror_path: str,
    files: list[str],
    dirs: list[str],
) -> None:
    """Generate the POSIX sh cleanup script (spec §5.9, original 1107-1145).

    Emits ``#!/bin/sh``, ``set -e``, ``cd '<mirror path>'``, one
    ``rm -f '<file>'`` line per file with the original's ``[NN%]``/``.``
    progress cadence — guarded: no percentage is ever emitted for an
    empty list (the original's divide-by-zero fix, spec §7) and a
    terminal ``[100%]`` marks the end of each non-empty section — then
    ``rm -r`` guarded by ``test -d`` per directory.  The header's size
    string is computed the way the original does it: from the on-disk
    (512-byte-block) usage of the listed files under ``mirror_path``.
    All paths are single-quoted with ``'`` escaped as ``'\\''`` (see
    _quoted_path).  The script is finished with ``chmod +x``.
    """
    nbytes = 0
    for name in files:
        try:
            nbytes += (Path(mirror_path) / name).stat().st_blocks * 512
        except OSError:
            continue
    size_output = format_bytes(nbytes)

    lines: list[str] = [
        "#!/bin/sh",
        "set -e",
        "",
        f"cd {_quoted_path(mirror_path)}",
        "",
        f"echo 'Removing {len(files)} unnecessary files [{size_output}]...'",
    ]
    total = len(files)
    for i, name in enumerate(files):
        lines.append(f"rm -f {_quoted_path(name)}")
        if total:
            if i % 500 == 0:
                lines.append(f"echo -n '[{100 * i // total}%]'")
            if i % 10 == 0:
                lines.append("echo -n .")
    if total:
        lines.append("echo -n '[100%]'")
    lines += ["echo 'done.'", "echo", ""]

    total = len(dirs)
    lines.append(f"echo 'Removing {total} unnecessary directories...'")
    for i, name in enumerate(dirs):
        quoted = _quoted_path(name)
        lines.append(f"if test -d {quoted}; then rm -r {quoted}; fi")
        if total:
            if i % 50 == 0:
                lines.append(f"echo -n '[{100 * i // total}%]'")
            lines.append("echo -n .")
    if total:
        lines.append("echo -n '[100%]'")
    lines += ["echo 'done.'", "echo"]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | 0o111)


def run_postmirror(variables: dict[str, str]) -> None:
    """Run the postmirror script with the cwd set to ``mirror_path``.

    Prints the three fixed original messages; runs the script directly
    when it is executable, otherwise via ``/bin/sh`` (spec §5.10 — the
    original still chdirs to ``mirror_path`` and user scripts may rely on
    it).  A non-zero child exit status (or an exec failure) is a warning
    only and never changes the process exit code.
    """
    script = get_var(variables, "postmirror_script")
    mirror_dir = Path(get_var(variables, "mirror_path"))
    print("Running the Post Mirror script ...")
    print(f"({script})\n")
    command = [script] if os.access(script, os.X_OK) else ["/bin/sh", script]
    return_code: int | None = None
    try:
        result = subprocess.run(command, check=False, cwd=mirror_dir)
        return_code = result.returncode
    except OSError as error:
        logger.warning(
            "apt-mirror: can't run postmirror script '%s': %s", script, error
        )
    if return_code is not None and return_code != 0:
        logger.warning(
            "apt-mirror: postmirror script exited with status %d", return_code
        )
    print(
        "\nPost Mirror script has completed. "
        "See above output for any possible errors.\n"
    )


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


def main(argv: list[str] | None = None) -> int:
    """Run one full mirror pass (spec §5); return 0 on success.

    ``argv`` defaults to ``sys.argv[1:]``.  ``--version`` prints
    ``apt-mirror (python3) 1.0.0`` and returns 0.  Any AptMirrorError
    (config, directory, or lock failure) is printed to stderr and yields
    a 1; per-URL download failures are never fatal.
    """
    parser = argparse.ArgumentParser(
        prog="apt-mirror.py",
        description="Mirror apt repositories (Python reimplementation of "
        "the Perl apt-mirror).",
    )
    parser.add_argument(
        "configfile",
        nargs="?",
        default="/etc/apt/mirror.list",
        help="mirror.list config file (default: /etc/apt/mirror.list)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version string and exit",
    )
    args = parser.parse_args(argv)
    if args.version:
        print("apt-mirror (python3) 1.0.0")
        return 0
    try:
        return _mirror_run(args.configfile)
    except AptMirrorError as error:
        print(str(error), file=sys.stderr)
        return 1


def _mirror_run(configfile: str) -> int:
    """The full stage pipeline (spec §5): returns 0 on success.

    Stage order (the original's): create the three directories -> lock ->
    release -> index -> archive -> skel-to-mirror copy -> clean ->
    postmirror.  AptMirrorError propagates to main's handler; per-URL
    failures stay within their stage.
    """
    config = parse_config(configfile)
    variables = config.variables
    tilde = _config_on(get_var(variables, "_tilde"))
    plus = _config_on(get_var(variables, "_plus"))
    mirror_path = Path(get_var(variables, "mirror_path"))
    skel_path = Path(get_var(variables, "skel_path"))
    var_path = Path(get_var(variables, "var_path"))
    cleanscript_path = Path(get_var(variables, "cleanscript"))
    unlink_repl = _config_on(get_var(variables, "unlink"))

    # The three needed directories (spec §5.2), created before anything
    # else is touched.
    for directory in (mirror_path, skel_path, var_path):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise AptMirrorError(
                f"apt-mirror: can't create {directory} directory"
            ) from error

    fetcher = Fetcher(variables)
    specs = config.binaries + config.sources

    # Shared per-run state.  skipclean starts with the config skip-clean
    # entries and gains every queued file (canonical **and** by-hash tree
    # spelling) plus every archive record's tree path.
    skipclean: set[str] = set(config.skipclean)
    hashsum_to_canonical: dict[str, list[str]] = {}
    canonical_to_hashsums: dict[str, list[str]] = {}
    release_urls: list[str] = []
    index_urls: list[str] = []
    index_urls_seen: set[str] = set()

    with locked(var_path):
        # ----------------------- Stage 1: release (spec §5.5) ---------
        def queue_release_urls() -> None:
            """Queue InRelease/Release/Release.gpg for every spec,
            registering each in skipclean (original add_url_to_download)."""
            seen: set[str] = set()
            for spec in specs:
                for name in _RELEASE_NAMES:
                    url = remove_double_slashes(dist_base(spec) + name)
                    if url in seen:
                        continue
                    seen.add(url)
                    release_urls.append(url)
                    skipclean.add(sanitise_uri(url, tilde, plus))

        queue_release_urls()
        download_stage(
            "release",
            sorted(release_urls),
            skel_path,
            fetcher,
            variables,
            var_path,
        )

        # ------------------------ Stage 2: index (spec §5.6) ----------
        def valid_release_info(path: Path) -> ReleaseInfo | None:
            """The ReleaseInfo of a non-empty, parseable Release file, or
            None when the copy is missing, empty, or unparseable (spec
            §7.1 fix #4 — a truncated Release must not survive the
            validity check).  parse_release never raises: it collects
            warnings and simply stops when the fragment runs out of
            checksum sections, so a truncated fragment (e.g. 10 leftover
            bytes) parses to zero entries — and a release that names no
            file at all is corrupt, not valid."""
            if not path.is_file():
                return None
            try:
                if path.stat().st_size == 0:
                    return None
                info = parse_release(path)
            except (OSError, UnicodeDecodeError, ValueError):
                return None
            if not info.entries:
                return None
            return info

        def spec_release_info(spec: Repo) -> ReleaseInfo | None:
            """The usable InRelease (else Release) of ``spec``.

            A local copy that exists but is empty or fails parse_release
            is deleted and re-fetched once.  Every warning carried by a
            usable ReleaseInfo (Task 5's malformed-continuation lines,
            spec §5.6) is emitted to stderr, verbatim, right after the
            parse that produced it — both the initial and the
            re-fetch paths land on the same return.  When neither file
            yields a parseable Release the exact original warning is
            printed (to stderr) and None is returned so the caller falls
            back to the well-known index set."""
            for name in ("InRelease", "Release"):
                url = dist_base(spec) + name
                path = skel_path / sanitise_uri(url, tilde, plus)
                info = valid_release_info(path)
                if info is None and path.exists():
                    path.unlink(missing_ok=True)
                    with contextlib.suppress(FetchError):
                        fetcher.fetch(url, path)
                    info = valid_release_info(path)
                if info is not None:
                    for warning in info.warnings:
                        print(warning, file=sys.stderr)
                    return info
            print(
                "Failed to find InRelease or Release in "
                f"{skel_path}/{sanitise_uri(dist_base(spec), tilde, plus)}",
                file=sys.stderr,
            )
            return None

        def queue_index_urls(spec: Repo) -> None:
            """Queue ``spec``'s index files for the skel stage, registering
            every queued file (canonical and by-hash tree paths) in
            skipclean and populating the two by-hash maps (spec §5.6.4)."""
            info = spec_release_info(spec)
            if info is None:
                indices = known_index_urls(spec)
                # Fallback filenames are repo-URI-relative: no dist/
                # prepending (controller clarification 1).
                base_url = spec.uri
            else:
                indices = index_urls_for(spec, info)
                # Release-driven filenames are dist-root-relative.
                base_url = dist_base(spec)
            for idx in indices:
                url = remove_double_slashes(base_url + "/" + idx.by_hash_path)
                canonical_tree = sanitise_uri(
                    remove_double_slashes(base_url + "/" + idx.filename),
                    tilde,
                    plus,
                )
                skipclean.add(canonical_tree)
                if idx.by_hash is not None and idx.by_hashsum is not None:
                    hash_tree = sanitise_uri(url, tilde, plus)
                    skipclean.add(hash_tree)
                    hashsum_to_canonical.setdefault(hash_tree, []).append(
                        canonical_tree
                    )
                    canon_hashes = canonical_to_hashsums.setdefault(canonical_tree, [])
                    canon_hashes.append(hash_tree)
                    # The original's file_to_hashsums maps a file to the
                    # by-hash path of EVERY supported section it advertises,
                    # so the skel->mirror stage hardlinks the one download
                    # to all of them, not just the strongest fetched.
                    if info is not None:
                        for entry in info.entries.get(idx.filename, []):
                            if entry.hashname not in HASH_STRENGTH:
                                continue
                            name = entry.hashname.lower()
                            if name == idx.by_hash:
                                continue
                            other_path = (
                                f"{posixpath.dirname(idx.filename)}/by-hash/"
                                f"{name}/{entry.hashsum}"
                            )
                            other_tree = sanitise_uri(
                                remove_double_slashes(base_url + "/" + other_path),
                                tilde,
                                plus,
                            )
                            if other_tree in canon_hashes:
                                continue
                            skipclean.add(other_tree)
                            canon_hashes.append(other_tree)
                if url not in index_urls_seen:
                    index_urls_seen.add(url)
                    index_urls.append(url)

        marks: list[str] = []
        for spec in specs:
            # One M per config entry, in the original's output order
            # (binaries first, then sources).
            marks.append("M")
            queue_index_urls(spec)
        print(f"Processing metadata files from releases [{''.join(marks)}]")
        print()
        download_stage(
            "index", sorted(index_urls), skel_path, fetcher, variables, var_path
        )

        # By-hash index files were stored under their by-hash path; the
        # original fans them out to their canonical skel paths right after
        # the stage (download_urls, original 339-348 — the cwd is the skel
        # there), so the archive stage can open the canonical index files.
        for hash_tree, canonicals in hashsum_to_canonical.items():
            for canonical in canonicals:
                copy_file(skel_path / hash_tree, skel_path / canonical, unlink_repl)

        # ----------------------- Stage 3: archive (spec §5.7) ---------
        all_lines: list[str] = []
        new_lines: list[str] = []
        md5_lines: list[str] = []
        sha1_lines: list[str] = []
        sha256_lines: list[str] = []
        archive_urls: dict[str, int] = {}

        def read_index(spec: Repo, path: Path, url: str, tree_prefix: str):
            """Parse one skel index file, deleting and re-fetching a
            corrupt copy once (spec §7.1 fix #4).  A malformed Sources
            format is a hard error (original die); every other parse
            failure is a warning (the index is treated as absent)."""
            try:
                return parse_index(path, tree_prefix)
            except AptMirrorError:
                raise
            except _INDEX_PARSE_ERRORS:
                path.unlink(missing_ok=True)
                try:
                    fetcher.fetch(url, path)
                except FetchError:
                    logger.warning(
                        "apt-mirror: can't open index %s in process_index", path
                    )
                    return []
                try:
                    return parse_index(path, tree_prefix)
                except AptMirrorError:
                    raise
                except _INDEX_PARSE_ERRORS:
                    logger.warning(
                        "apt-mirror: can't open index %s in process_index", path
                    )
                    return []

        def process_index_spec(spec: Repo, index_rel: str, optional: bool) -> None:
            """The original's process_index: open ``spec``'s skel index at
            ``index_rel`` (trying each compressed extension, then the
            uncompressed file) and do the ALL/NEW/MD5/SHA1/SHA256
            bookkeeping plus the archive URL queue for every record."""
            tree_prefix = sanitise_uri(spec.uri, tilde, plus)
            skel_base = skel_path / tree_prefix
            for extension in _INDEX_EXTENSIONS:
                rel = f"{index_rel}{extension}"
                path = skel_base / rel
                if not path.is_file():
                    continue
                for rec in read_index(spec, path, f"{spec.uri}/{rel}", tree_prefix):
                    # One source of truth per record: every bookkeeping
                    # list and the skipclean set see the very same
                    # PackageRec.
                    skipclean.add(rec.mirror_rel)
                    all_lines.append(rec.mirror_rel)
                    if rec.md5 is not None:
                        md5_lines.append(f"{rec.md5}  {rec.mirror_rel}")
                    if rec.sha1 is not None:
                        sha1_lines.append(f"{rec.sha1}  {rec.mirror_rel}")
                    if rec.sha256 is not None:
                        sha256_lines.append(f"{rec.sha256}  {rec.mirror_rel}")
                    if need_update(mirror_path / rec.mirror_rel, rec.size):
                        # Full (possibly userinfo-carrying) URL for the
                        # download queue; var/NEW is a persisted file and
                        # must carry the credential-stripped form only
                        # (spec §7.1 fix #2).
                        download_url = remove_double_slashes(
                            f"{spec.uri}/{rec.url_rel}"
                        )
                        new_lines.append(_strip_userinfo(download_url))
                        archive_urls[download_url] = rec.size
                return
            if not optional:
                logger.warning(
                    "apt-mirror: can't open index %s/%s in process_index",
                    tree_prefix,
                    index_rel,
                )

        marks = []
        for spec in config.sources:
            # One S per source spec.
            marks.append("S")
            if spec.components is not None:
                for comp in spec.components:
                    process_index_spec(
                        spec,
                        f"dists/{spec.distribution}/{comp}/source/Sources",
                        False,
                    )
            else:
                process_index_spec(spec, f"{spec.distribution}/Sources", False)
        for spec in config.binaries:
            # One P per binary spec.
            marks.append("P")
            if spec.components is not None:
                # A comma-separated arch list is honoured (spec §5.1):
                # every arch gets its binary-<arch> index, while the
                # binary-all / debian-installer/binary-all indexes are
                # processed exactly once per component.
                archs = [arch for arch in spec.arch.split(",") if arch]
                for comp in spec.components:
                    d = spec.distribution
                    for arch in archs:
                        process_index_spec(
                            spec,
                            f"dists/{d}/{comp}/binary-{arch}/Packages",
                            False,
                        )
                    process_index_spec(
                        spec, f"dists/{d}/{comp}/binary-all/Packages", True
                    )
                    for arch in archs:
                        process_index_spec(
                            spec,
                            f"dists/{d}/{comp}/debian-installer/binary-{arch}/Packages",
                            True,
                        )
                    process_index_spec(
                        spec,
                        f"dists/{d}/{comp}/debian-installer/binary-all/Packages",
                        True,
                    )
            else:
                process_index_spec(spec, f"{spec.distribution}/Packages", False)
        print(f"Processing indexes: [{''.join(marks)}]")
        print()

        # The five intermediate files, truncated fresh at the start of the
        # stage (the original opens them before any record is read); an
        # empty stage still leaves them in place.
        for name, lines in (
            ("ALL", all_lines),
            ("NEW", new_lines),
            ("MD5", md5_lines),
            ("SHA1", sha1_lines),
            ("SHA256", sha256_lines),
        ):
            (var_path / name).write_text(
                "".join(f"{line}\n" for line in lines), encoding="utf-8"
            )

        need_bytes = sum(archive_urls.values())
        print(f"{format_bytes(need_bytes)} will be downloaded into archive.")
        download_stage(
            "archive",
            sorted(archive_urls),
            mirror_path,
            fetcher,
            variables,
            var_path,
        )

        # --------------------- Skel -> mirror copy (spec §5.8) --------
        for url in release_urls + index_urls:
            sanitized = sanitise_uri(url, tilde, plus)
            copy_file(skel_path / sanitized, mirror_path / sanitized, unlink_repl)
            # A by-hash download fans out to its canonical path, and (as
            # the original does) to every other by-hash path mapped to
            # that canonical file.
            for canonical in hashsum_to_canonical.get(sanitized, []):
                copy_file(
                    mirror_path / sanitized,
                    mirror_path / canonical,
                    unlink_repl,
                )
                for other_hash in canonical_to_hashsums.get(canonical, []):
                    copy_file(
                        mirror_path / sanitized,
                        mirror_path / other_hash,
                        unlink_repl,
                    )

        # --------------------------- Cleaning (spec §5.9) -------------
        unneeded = collect_unneeded(
            mirror_path, sorted(config.clean), skipclean, tilde, plus
        )
        # The original opens (truncates) the cleanscript regardless of
        # mode; in _autoclean mode it stays empty.  Like the original's
        # chmod +x (both modes), the script is executable either way — in
        # _autoclean mode it is simply left empty (it is regenerated and
        # used on the next non-autoclean run).
        try:
            cleanscript_path.parent.mkdir(parents=True, exist_ok=True)
            with cleanscript_path.open("w", encoding="utf-8"):
                pass
            cleanscript_path.chmod(cleanscript_path.stat().st_mode | 0o111)
        except OSError:
            pass
        if _config_on(get_var(variables, "_autoclean")):
            print(
                f"{format_bytes(unneeded.nbytes)} in "
                f"{len(unneeded.files)} files and {len(unneeded.dirs)} "
                f"directories will be freed...",
                end="",
            )
            for name in unneeded.files:
                (mirror_path / name).unlink(missing_ok=True)
            for name in unneeded.dirs:
                with contextlib.suppress(OSError):
                    (mirror_path / name).rmdir()
        else:
            print(
                f"{format_bytes(unneeded.nbytes)} in "
                f"{len(unneeded.files)} files and {len(unneeded.dirs)} "
                f"directories can be freed.\n"
            )
            print(f"Run {cleanscript_path} for this purpose.\n")
            write_cleanscript(
                cleanscript_path, str(mirror_path), unneeded.files, unneeded.dirs
            )

        # ------------------------- Postmirror (spec §5.10) ------------
        if _config_on(get_var(variables, "run_postmirror")):
            script_path = Path(get_var(variables, "postmirror_script"))
            if script_path.is_file():
                run_postmirror(variables)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
