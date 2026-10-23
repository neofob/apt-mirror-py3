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
import sys

from apt_mirror.clean import Unneeded, collect_unneeded, write_cleanscript
from apt_mirror.config import (
    DEFAULT_CONFIG,
    MAX_EXPANSION_PASSES,
    Repo,
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
    PackageRec,
    iter_index_blocks,
    need_update,
    parse_index,
)
from apt_mirror.release import (
    HashEntry,
    IndexUrl,
    ReleaseInfo,
    _known_base_paths,
    dist_base,
    index_urls_for,
    known_index_urls,
    parse_release,
)
from apt_mirror.run import AptMirror, ArchiveBookkeeping
from apt_mirror.uri import remove_double_slashes, sanitise_uri
from apt_mirror.util import copy_file, format_bytes, locked, round_number

#: Re-exports from ``apt_mirror`` that keep this module's test interface
#: (tests load it as the ``aptmirror`` module) stable; mirror
#: ``apt_mirror/__init__``.
__all__ = [
    "DEFAULT_CONFIG",
    "MAX_EXPANSION_PASSES",
    "AptMirror",
    "AptMirrorError",
    "ArchiveBookkeeping",
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
    "Unneeded",
    "_known_base_paths",
    "collect_unneeded",
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
    "write_cleanscript",
]


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
    """See apt_mirror.run.AptMirror — behavior frozen by the test suite."""
    return AptMirror(parse_config(configfile)).run()


if __name__ == "__main__":
    raise SystemExit(main())
