"""The apt-mirror CLI: argument parsing and the ``main`` entry point."""

import argparse
import sys

from apt_mirror.config import parse_config
from apt_mirror.errors import AptMirrorError
from apt_mirror.run import AptMirror


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
        return AptMirror(parse_config(args.configfile)).run()
    except AptMirrorError as error:
        print(str(error), file=sys.stderr)
        return 1
