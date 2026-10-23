"""Mirror-tree cleanup collection and clean script generation (spec §5.9)."""

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from apt_mirror.errors import AptMirrorError
from apt_mirror.uri import sanitise_uri
from apt_mirror.util import _quoted_path, format_bytes


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
