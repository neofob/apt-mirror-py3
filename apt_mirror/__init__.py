"""The apt-mirror package; the public API is re-exported from its submodules."""

from apt_mirror.clean import Unneeded, collect_unneeded, write_cleanscript
from apt_mirror.config import (
    DEFAULT_CONFIG,
    MAX_EXPANSION_PASSES,
    ParsedConfig,
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
    "ParsedConfig",
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
