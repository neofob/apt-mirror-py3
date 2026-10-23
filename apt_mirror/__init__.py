"""The apt-mirror package; the public API is re-exported from its submodules."""

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
from apt_mirror.errors import AptMirrorError
from apt_mirror.fetch import Fetcher, FetchError, FetchNotFound, RateLimiter
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
from apt_mirror.uri import remove_double_slashes, sanitise_uri
from apt_mirror.util import copy_file, format_bytes, locked, round_number

__all__ = [
    "DEFAULT_CONFIG",
    "MAX_EXPANSION_PASSES",
    "AptMirrorError",
    "FetchError",
    "FetchNotFound",
    "Fetcher",
    "HashEntry",
    "IndexUrl",
    "ParsedConfig",
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
