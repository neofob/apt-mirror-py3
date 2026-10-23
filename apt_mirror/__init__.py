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
from apt_mirror.uri import remove_double_slashes, sanitise_uri
from apt_mirror.util import copy_file, format_bytes, locked, round_number

__all__ = [
    "DEFAULT_CONFIG",
    "MAX_EXPANSION_PASSES",
    "AptMirrorError",
    "FetchError",
    "FetchNotFound",
    "Fetcher",
    "ParsedConfig",
    "RateLimiter",
    "Repo",
    "copy_file",
    "detect_defaultarch",
    "format_bytes",
    "get_var",
    "locked",
    "parse_config",
    "parse_config_line",
    "parse_rate",
    "remove_double_slashes",
    "round_number",
    "sanitise_uri",
]
