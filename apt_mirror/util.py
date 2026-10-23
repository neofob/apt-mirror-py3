"""Shared helpers: size formatting, credential scrubbing, copying, locking."""

import fcntl
import filecmp
import logging
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from apt_mirror.errors import AptMirrorError

logger = logging.getLogger("apt-mirror")


def round_number(x: float) -> str:
    """Round ``x`` to one decimal (ties round up) and format as a string.

    Mirrors the original Perl ``round_number``: the magnitude is rounded to
    the nearest tenth and the result is always rendered with at least one
    decimal digit (whole numbers gain ``.0``).  The sign is preserved.
    """
    minus = "-" if x < 0 else ""
    tenths = int((abs(x) + 0.05) * 10)
    whole, frac = divmod(tenths, 10)
    if frac:
        return f"{minus}{whole}.{frac}"
    return f"{minus}{whole}.0"


def format_bytes(n: int) -> str:
    """Format a byte count as a human-readable size string.

    Below 1024 the value renders as ``<n> bytes``; otherwise the largest
    binary unit (KiB / MiB / GiB, all 1024-based) divides it and the quotient
    is rendered with ``round_number``.  Ports the original Perl
    ``format_bytes``.
    """
    if n < 1024:
        return f"{n} bytes"
    if n >= 1024 * 1024 * 1024:
        value, unit = n / (1024 * 1024 * 1024), "GiB"
    elif n >= 1024 * 1024:
        value, unit = n / (1024 * 1024), "MiB"
    else:
        value, unit = n / 1024, "KiB"
    return f"{round_number(value)} {unit}"


_USERINFO_RE = re.compile(r"(?<=://)[^@/]*@")


def _strip_userinfo(url: str) -> str:
    """Remove a ``user[:password]@`` prefix from a URL, if present."""
    return _USERINFO_RE.sub("", url, count=1)


def _quoted_path(path: str) -> str:
    """A path single-quoted for POSIX sh, with ``'`` escaped as ``'\\''``.

    Ports the original's ``quoted_path`` so the generated cleanup script
    survives any character in the mirror paths.
    """
    return "'" + path.replace("'", "'\\''") + "'"


def copy_file(from_p: Path, to_p: Path, unlink_repl: bool) -> None:
    """Copy (preferring a hardlink) ``from_p`` to ``to_p`` (spec §5.8).

    Skips entirely when ``from_p`` is missing (parents are not created);
    creates the destination's parent directories; when the destination
    already matches the source's ``(st_size, st_mtime_ns)``, skips (the
    common unchanged case); when ``unlink_repl`` (the ``unlink`` config
    option) is set and the destination's content differs, deletes it
    first; hardlinks when possible and falls back to a copy for the
    cross-device case; and finally restores the source's atime/mtime onto
    the destination.  A failed copy is a warning, not a fatal error (like
    the original ``copy_file``).
    """
    if not from_p.is_file():
        return
    to_p.parent.mkdir(parents=True, exist_ok=True)
    from_stat = from_p.stat()
    if to_p.is_file():
        to_stat = to_p.stat()
        if (from_stat.st_size, from_stat.st_mtime_ns) == (
            to_stat.st_size,
            to_stat.st_mtime_ns,
        ):
            return
        if unlink_repl and not filecmp.cmp(str(from_p), str(to_p), shallow=False):
            to_p.unlink(missing_ok=True)
    try:
        try:
            os.link(from_p, to_p)
        except OSError:
            shutil.copy2(from_p, to_p)
    except OSError as error:
        logger.warning("apt-mirror: can't copy %s to %s: %s", from_p, to_p, error)
        return
    os.utime(to_p, ns=(from_stat.st_atime_ns, from_stat.st_mtime_ns))


@contextmanager
def locked(var_path: Path) -> Iterator[None]:
    """Hold an exclusive non-blocking ``flock`` on
    ``<var_path>/apt-mirror.lock`` (spec §5.2).

    On acquisition failure raises AptMirrorError with the original's
    exact message (the lock file is left in place); on success the lock
    is released and the lock file removed on exit — so a stale
    (holderless) lock file left by a crash is simply re-acquired and does
    not survive the next clean exit.
    """
    lock_file = var_path / "apt-mirror.lock"
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        raise AptMirrorError("apt-mirror is already running, exiting") from error
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        lock_file.unlink(missing_ok=True)
