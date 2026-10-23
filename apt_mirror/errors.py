"""Fatal error type for apt-mirror."""


class AptMirrorError(Exception):
    """Fatal apt-mirror error; ``str(e)`` is the full user-facing message."""
