"""The AptMirror orchestrator: one full mirror pass (spec §5)."""

import contextlib
import logging
import os
import posixpath
import subprocess
import sys
from pathlib import Path

from apt_mirror.clean import collect_unneeded, write_cleanscript
from apt_mirror.config import ParsedConfig, Repo, _config_on, get_var
from apt_mirror.download import download_stage
from apt_mirror.errors import AptMirrorError
from apt_mirror.fetch import Fetcher, FetchError
from apt_mirror.index import (
    _INDEX_EXTENSIONS,
    _INDEX_PARSE_ERRORS,
    PackageRec,
    need_update,
    parse_index,
)
from apt_mirror.release import (
    _RELEASE_NAMES,
    HASH_STRENGTH,
    ReleaseInfo,
    dist_base,
    index_urls_for,
    known_index_urls,
    parse_release,
)
from apt_mirror.uri import remove_double_slashes, sanitise_uri
from apt_mirror.util import _strip_userinfo, copy_file, format_bytes, locked

logger = logging.getLogger("apt-mirror")


class ArchiveBookkeeping:
    """Accumulates the var/ALL, NEW, MD5, SHA1, SHA256 line lists and the
    archive download queue while archive indexes are parsed."""

    def __init__(self, mirror_path: Path) -> None:
        """Start every line list and the urls queue empty; keep
        ``mirror_path`` for the per-record need_update checks."""
        self.mirror_path = mirror_path
        self.all_lines: list[str] = []
        self.new_lines: list[str] = []
        self.md5_lines: list[str] = []
        self.sha1_lines: list[str] = []
        self.sha256_lines: list[str] = []
        self._urls: dict[str, int] = {}

    def add(self, spec: Repo, rec: PackageRec) -> None:
        """One PackageRec added once: mirror_rel → all lines; md5/sha1/
        sha256 records; and, when need_update(mirror_path/mirror_rel,
        size) is true, the credential-stripped NEW line plus the full
        (userinfo-carrying) URL → size in urls (spec §7.1 fix #2)."""
        self.all_lines.append(rec.mirror_rel)
        if rec.md5 is not None:
            self.md5_lines.append(f"{rec.md5}  {rec.mirror_rel}")
        if rec.sha1 is not None:
            self.sha1_lines.append(f"{rec.sha1}  {rec.mirror_rel}")
        if rec.sha256 is not None:
            self.sha256_lines.append(f"{rec.sha256}  {rec.mirror_rel}")
        if need_update(self.mirror_path / rec.mirror_rel, rec.size):
            # Full (possibly userinfo-carrying) URL for the
            # download queue; var/NEW is a persisted file and
            # must carry the credential-stripped form only
            # (spec §7.1 fix #2).
            download_url = remove_double_slashes(f"{spec.uri}/{rec.url_rel}")
            self.new_lines.append(_strip_userinfo(download_url))
            self._urls[download_url] = rec.size

    def write_var_files(self, var_path: Path) -> None:
        """Truncates and writes ALL/NEW/MD5/SHA1/SHA256 under var_path,
        one line-terminated entry each, in that order."""
        for name, lines in (
            ("ALL", self.all_lines),
            ("NEW", self.new_lines),
            ("MD5", self.md5_lines),
            ("SHA1", self.sha1_lines),
            ("SHA256", self.sha256_lines),
        ):
            (var_path / name).write_text(
                "".join(f"{line}\n" for line in lines), encoding="utf-8"
            )

    @property
    def urls(self) -> dict[str, int]:
        """The archive download queue: full (userinfo-carrying) URL → size."""
        return self._urls

    @property
    def need_bytes(self) -> int:
        """The total size of the queued archives (sum of urls.values())."""
        return sum(self.urls.values())


class AptMirror:
    """One full mirror pass driven by a ParsedConfig."""

    def __init__(self, config: ParsedConfig) -> None:
        """Stores config/variables; resolves tilde, plus, unlink_repl
        (via _config_on/get_var); resolves the mirror/skel/var/cleanscript
        Paths; seeds skipclean from config.skipclean and the per-run
        collections (hashsum_to_canonical, canonical_to_hashsums,
        release_urls, index_urls, index_urls_seen); sets fetcher to None.
        No side effects: no mkdir, no lock, no Fetcher."""
        self.config = config
        self.variables = config.variables
        self.tilde = _config_on(get_var(self.variables, "_tilde"))
        self.plus = _config_on(get_var(self.variables, "_plus"))
        self.unlink_repl = _config_on(get_var(self.variables, "unlink"))
        self.mirror_path = Path(get_var(self.variables, "mirror_path"))
        self.skel_path = Path(get_var(self.variables, "skel_path"))
        self.var_path = Path(get_var(self.variables, "var_path"))
        self.cleanscript_path = Path(get_var(self.variables, "cleanscript"))
        self.archives = ArchiveBookkeeping(self.mirror_path)

        # Shared per-run state.  skipclean starts with the config skip-clean
        # entries and gains every queued file (canonical **and** by-hash tree
        # spelling) plus every archive record's tree path.
        self.skipclean: set[str] = set(config.skipclean)
        self.hashsum_to_canonical: dict[str, list[str]] = {}
        self.canonical_to_hashsums: dict[str, list[str]] = {}
        self.release_urls: list[str] = []
        self.index_urls: list[str] = []
        self.index_urls_seen: set[str] = set()
        self.fetcher: Fetcher | None = None

    def run(self) -> int:
        """The full stage pipeline (spec §5): returns 0 on success.

        Frozen construction order (pinned by tests/test_run.py): create
        the three directories -> build the Fetcher -> take the lock ->
        run the stages in the original's order (release -> index ->
        archive -> skel-to-mirror copy -> clean -> postmirror).
        AptMirrorError propagates to the caller's handler; per-URL
        failures stay within their stage.
        """
        # The three needed directories (spec §5.2), created before anything
        # else is touched.
        for directory in (self.mirror_path, self.skel_path, self.var_path):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise AptMirrorError(
                    f"apt-mirror: can't create {directory} directory"
                ) from error

        self.fetcher = Fetcher(self.variables)
        specs = self.config.binaries + self.config.sources

        with locked(self.var_path):
            self._stage_release(specs)
            self._stage_index(specs)
            self._stage_archive()
            self._stage_mirror_copy()
            self._stage_clean()
            self._run_postmirror()
        return 0

    def _queue_release_urls(self, specs: list[Repo]) -> None:
        """Queue InRelease/Release/Release.gpg for every spec,
        registering each in skipclean (original add_url_to_download)."""
        seen: set[str] = set()
        for spec in specs:
            for name in _RELEASE_NAMES:
                url = remove_double_slashes(dist_base(spec) + name)
                if url in seen:
                    continue
                seen.add(url)
                self.release_urls.append(url)
                self.skipclean.add(sanitise_uri(url, self.tilde, self.plus))

    def _stage_release(self, specs: list[Repo]) -> None:
        """-------------------- Stage 1: release (spec §5.5) -----------"""
        self._queue_release_urls(specs)
        assert self.fetcher is not None
        download_stage(
            "release",
            sorted(self.release_urls),
            self.skel_path,
            self.fetcher,
            self.variables,
            self.var_path,
        )

    def _valid_release_info(self, path: Path) -> ReleaseInfo | None:
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

    def _spec_release_info(self, spec: Repo) -> ReleaseInfo | None:
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
            path = self.skel_path / sanitise_uri(url, self.tilde, self.plus)
            info = self._valid_release_info(path)
            if info is None and path.exists():
                path.unlink(missing_ok=True)
                assert self.fetcher is not None
                with contextlib.suppress(FetchError):
                    self.fetcher.fetch(url, path)
                info = self._valid_release_info(path)
            if info is not None:
                for warning in info.warnings:
                    print(warning, file=sys.stderr)
                return info
        print(
            "Failed to find InRelease or Release in "
            f"{self.skel_path}/"
            f"{sanitise_uri(dist_base(spec), self.tilde, self.plus)}",
            file=sys.stderr,
        )
        return None

    def _queue_index_urls(self, spec: Repo) -> None:
        """Queue ``spec``'s index files for the skel stage, registering
        every queued file (canonical and by-hash tree paths) in
        skipclean and populating the two by-hash maps (spec §5.6.4)."""
        info = self._spec_release_info(spec)
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
                self.tilde,
                self.plus,
            )
            self.skipclean.add(canonical_tree)
            if idx.by_hash is not None and idx.by_hashsum is not None:
                hash_tree = sanitise_uri(url, self.tilde, self.plus)
                self.skipclean.add(hash_tree)
                self.hashsum_to_canonical.setdefault(hash_tree, []).append(
                    canonical_tree
                )
                canon_hashes = self.canonical_to_hashsums.setdefault(canonical_tree, [])
                canon_hashes.append(hash_tree)
                # The original's file_to_hashsums maps a file to the
                # by-hash path of EVERY supported section it advertises,
                # so the skel->mirror stage hardlinks the one download
                # to all of them, not just the strongest fetched.
                if info is not None:
                    for entry in info.entries.get(idx.filename, []):
                        if entry.hashname not in HASH_STRENGTH:
                            continue
                        name = entry.hashname
                        if name == idx.by_hash:
                            continue
                        other_path = (
                            f"{posixpath.dirname(idx.filename)}/by-hash/"
                            f"{name}/{entry.hashsum}"
                        )
                        other_tree = sanitise_uri(
                            remove_double_slashes(base_url + "/" + other_path),
                            self.tilde,
                            self.plus,
                        )
                        if other_tree in canon_hashes:
                            continue
                        self.skipclean.add(other_tree)
                        canon_hashes.append(other_tree)
            if url not in self.index_urls_seen:
                self.index_urls_seen.add(url)
                self.index_urls.append(url)

    def _stage_index(self, specs: list[Repo]) -> None:
        """-------------------- Stage 2: index (spec §5.6) -------------"""
        marks: list[str] = []
        for spec in specs:
            # One M per config entry, in the original's output order
            # (binaries first, then sources).
            marks.append("M")
            self._queue_index_urls(spec)
        print(f"Processing metadata files from releases [{''.join(marks)}]")
        print()
        assert self.fetcher is not None
        download_stage(
            "index",
            sorted(self.index_urls),
            self.skel_path,
            self.fetcher,
            self.variables,
            self.var_path,
        )

        # By-hash index files were stored under their by-hash path; the
        # original fans them out to their canonical skel paths right after
        # the stage (download_urls, original 339-348 — the cwd is the skel
        # there), so the archive stage can open the canonical index files.
        for hash_tree, canonicals in self.hashsum_to_canonical.items():
            for canonical in canonicals:
                copy_file(
                    self.skel_path / hash_tree,
                    self.skel_path / canonical,
                    self.unlink_repl,
                )

    def _read_index(
        self, spec: Repo, path: Path, url: str, tree_prefix: str
    ) -> list[PackageRec]:
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
                assert self.fetcher is not None
                self.fetcher.fetch(url, path)
            except FetchError:
                logger.warning("apt-mirror: can't open index %s in process_index", path)
                return []
            try:
                return parse_index(path, tree_prefix)
            except AptMirrorError:
                raise
            except _INDEX_PARSE_ERRORS:
                logger.warning("apt-mirror: can't open index %s in process_index", path)
                return []

    def _process_index_spec(self, spec: Repo, index_rel: str, optional: bool) -> None:
        """The original's process_index: open ``spec``'s skel index at
        ``index_rel`` (trying each compressed extension, then the
        uncompressed file) and do the ALL/NEW/MD5/SHA1/SHA256
        bookkeeping plus the archive URL queue for every record."""
        tree_prefix = sanitise_uri(spec.uri, self.tilde, self.plus)
        skel_base = self.skel_path / tree_prefix
        for extension in _INDEX_EXTENSIONS:
            rel = f"{index_rel}{extension}"
            path = skel_base / rel
            if not path.is_file():
                continue
            for rec in self._read_index(spec, path, f"{spec.uri}/{rel}", tree_prefix):
                # One source of truth per record: every bookkeeping
                # list and the skipclean set see the very same
                # PackageRec.
                self.skipclean.add(rec.mirror_rel)
                self.archives.add(spec, rec)
            return
        if not optional:
            logger.warning(
                "apt-mirror: can't open index %s/%s in process_index",
                tree_prefix,
                index_rel,
            )

    def _stage_archive(self) -> None:
        """-------------------- Stage 3: archive (spec §5.7) -----------"""
        marks = []
        for spec in self.config.sources:
            # One S per source spec.
            marks.append("S")
            if spec.components is not None:
                for comp in spec.components:
                    self._process_index_spec(
                        spec,
                        f"dists/{spec.distribution}/{comp}/source/Sources",
                        False,
                    )
            else:
                self._process_index_spec(spec, f"{spec.distribution}/Sources", False)
        for spec in self.config.binaries:
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
                        self._process_index_spec(
                            spec,
                            f"dists/{d}/{comp}/binary-{arch}/Packages",
                            False,
                        )
                    self._process_index_spec(
                        spec, f"dists/{d}/{comp}/binary-all/Packages", True
                    )
                    for arch in archs:
                        self._process_index_spec(
                            spec,
                            f"dists/{d}/{comp}/debian-installer/binary-{arch}/Packages",
                            True,
                        )
                    self._process_index_spec(
                        spec,
                        f"dists/{d}/{comp}/debian-installer/binary-all/Packages",
                        True,
                    )
            else:
                self._process_index_spec(spec, f"{spec.distribution}/Packages", False)
        print(f"Processing indexes: [{''.join(marks)}]")
        print()

        # The five intermediate files, truncated fresh at the start of the
        # stage (the original opens them before any record is read); an
        # empty stage still leaves them in place.
        self.archives.write_var_files(self.var_path)

        print(
            f"{format_bytes(self.archives.need_bytes)} will be downloaded into archive."
        )
        assert self.fetcher is not None
        download_stage(
            "archive",
            sorted(self.archives.urls),
            self.mirror_path,
            self.fetcher,
            self.variables,
            self.var_path,
        )

    def _stage_mirror_copy(self) -> None:
        """--------------------- Skel -> mirror copy (spec §5.8) -------"""
        for url in self.release_urls + self.index_urls:
            sanitized = sanitise_uri(url, self.tilde, self.plus)
            copy_file(
                self.skel_path / sanitized,
                self.mirror_path / sanitized,
                self.unlink_repl,
            )
            # A by-hash download fans out to its canonical path, and (as
            # the original does) to every other by-hash path mapped to
            # that canonical file.
            for canonical in self.hashsum_to_canonical.get(sanitized, []):
                copy_file(
                    self.mirror_path / sanitized,
                    self.mirror_path / canonical,
                    self.unlink_repl,
                )
                for other_hash in self.canonical_to_hashsums.get(canonical, []):
                    copy_file(
                        self.mirror_path / sanitized,
                        self.mirror_path / other_hash,
                        self.unlink_repl,
                    )

    def _stage_clean(self) -> None:
        """--------------------------- Cleaning (spec §5.9) ------------"""
        unneeded = collect_unneeded(
            self.mirror_path,
            sorted(self.config.clean),
            self.skipclean,
            self.tilde,
            self.plus,
        )
        # The original opens (truncates) the cleanscript regardless of
        # mode; in _autoclean mode it stays empty.  Like the original's
        # chmod +x (both modes), the script is executable either way — in
        # _autoclean mode it is simply left empty (it is regenerated and
        # used on the next non-autoclean run).
        try:
            self.cleanscript_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cleanscript_path.open("w", encoding="utf-8"):
                pass
            self.cleanscript_path.chmod(self.cleanscript_path.stat().st_mode | 0o111)
        except OSError:
            pass
        if _config_on(get_var(self.variables, "_autoclean")):
            print(
                f"{format_bytes(unneeded.nbytes)} in "
                f"{len(unneeded.files)} files and {len(unneeded.dirs)} "
                f"directories will be freed...",
                end="",
            )
            for name in unneeded.files:
                (self.mirror_path / name).unlink(missing_ok=True)
            for name in unneeded.dirs:
                with contextlib.suppress(OSError):
                    (self.mirror_path / name).rmdir()
        else:
            print(
                f"{format_bytes(unneeded.nbytes)} in "
                f"{len(unneeded.files)} files and {len(unneeded.dirs)} "
                f"directories can be freed.\n"
            )
            print(f"Run {self.cleanscript_path} for this purpose.\n")
            write_cleanscript(
                self.cleanscript_path,
                str(self.mirror_path),
                unneeded.files,
                unneeded.dirs,
            )

    def _run_postmirror(self) -> None:
        """------------------------- Postmirror (spec §5.10) -----------"""
        if _config_on(get_var(self.variables, "run_postmirror")):
            script_path = Path(get_var(self.variables, "postmirror_script"))
            if script_path.is_file():
                run_postmirror(self.variables)


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
