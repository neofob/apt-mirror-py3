"""API-surface and CLI characterization tests for the apt_mirror package."""

import subprocess
import sys

from .conftest import REPO_ROOT


def test_public_api_surface(am) -> None:
    """The package re-exports the full public surface the suite uses."""
    names = [
        "AptMirror",
        "ArchiveBookkeeping",
        "AptMirrorError",
        "DEFAULT_CONFIG",
        "FetchError",
        "FetchNotFound",
        "Fetcher",
        "HashEntry",
        "IndexUrl",
        "MAX_EXPANSION_PASSES",
        "PackageRec",
        "ParsedConfig",
        "RateLimiter",
        "Repo",
        "ReleaseInfo",
        "StageResult",
        "Unneeded",
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
        "main",
        "need_update",
        "parse_config",
        "parse_config_line",
        "parse_rate",
        "parse_release",
        "round_number",
        "sanitise_uri",
        "remove_double_slashes",
        "write_cleanscript",
        "_known_base_paths",
    ]
    for name in names:
        assert hasattr(am, name), f"apt_mirror is missing {name}"


def test_version_output(am, capsys) -> None:
    """--version prints exactly the pinned string and returns 0."""
    assert am.main(["--version"]) == 0
    assert capsys.readouterr().out == "apt-mirror (python3) 1.0.0\n"


def test_missing_config_exits_1(tmp_path, am, capsys) -> None:
    """A missing config file is a clean AptMirrorError with exit 1."""
    missing = tmp_path / "nope.list"
    assert am.main([str(missing)]) == 1
    assert "apt-mirror: invalid config file specified" in capsys.readouterr().err


def test_shim_runs_from_other_cwd(tmp_path) -> None:
    """The root shim must work from any cwd (script dir on sys.path).
    --version needs no network or config."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "apt-mirror.py"), "--version"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "apt-mirror (python3) 1.0.0" in result.stdout
