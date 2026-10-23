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
import contextlib
import logging
import os
import posixpath
import subprocess
import sys
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
from apt_mirror.download import StageResult, download_stage
from apt_mirror.errors import AptMirrorError
from apt_mirror.fetch import Fetcher, FetchError, FetchNotFound, RateLimiter
from apt_mirror.index import (
    _INDEX_EXTENSIONS,
    _INDEX_PARSE_ERRORS,
    PackageRec,
    iter_index_blocks,
    need_update,
    parse_index,
)
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
    "PackageRec",
    "RateLimiter",
    "ReleaseInfo",
    "Repo",
    "StageResult",
    "_known_base_paths",
    "copy_file",
    "detect_defaultarch",
    "dist_base",
    "download_stage",
    "format_bytes",
    "get_var",
    "index_urls_for",
    "iter_index_blocks",
    "known_index_urls",
    "locked",
    "need_update",
    "parse_config",
    "parse_config_line",
    "parse_index",
    "parse_rate",
    "parse_release",
    "remove_double_slashes",
    "round_number",
    "sanitise_uri",
]

logger = logging.getLogger("apt-mirror")


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
