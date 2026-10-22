# AGENTS.md

Guidance for AI coding agents (and human contributors) working in this repository.

## Project overview

`apt-mirror-py` is a Python 3, single-file port of the classic Perl-based
**apt-mirror** tool. It mirrors apt repositories (Packages, Sources, Release,
`.deb` archives), manages a local skel directory, generates a clean script,
and supports proxy, rate limiting, and `.gz`/`.bz2`/`.xz` index handling.

- **Entry point:** `apt-mirror.py` (CLI, stdlib-only)
- **Target platform:** Debian-based Linux (uses `dpkg`, `wget`, `fcntl` file locking)
- **Python:** 3.9+ (3.10+ recommended; stdlib only — no third-party dependencies)

## Repository layout

```
apt-mirror-py/
├── apt-mirror.py   # Entire application: config parser, download engine, CLI
└── AGENTS.md       # This file
```

The codebase is intentionally one file. Do **not** split it into modules, add
a package directory, or introduce dependencies (e.g. `requests`, `urllib3`)
without an explicit discussion — the single-file, stdlib-only design is a
contribution goal (easy deployment on minimal servers).

## Running the tool

```bash
# Run with an explicit config file
python3 apt-mirror.py /path/to/mirror.list

# Run with the default config location (read-only inspection is safe)
python3 apt-mirror.py /etc/apt/mirror.list
```

Requirements:
- `wget` on `PATH` (downloads are delegated to `wget` via `subprocess`)
- `dpkg` for architecture detection (falls back to `amd64` if absent)
- Writes are confined to `base_path` (default `/var/spool/apt-mirror`) and the
  config-specified mirror/skel/var paths

## Code style — PEP 8 and project conventions

- Follow **PEP 8** (`pep8`/`ruff` clean code). Target line length is 88 chars.
- Type hints on function signatures (the codebase already uses them, e.g.
  `def get_var(self, name: str) -> str`). Keep new code consistent.
- Use **double quotes** for strings (existing convention).
- Naming: `snake_case` for functions/variables, `PascalCase` for classes,
  `UPPER_SNAKE_CASE` for module-level constants (e.g. `COMPRESSION_MODULES`).
- Docstrings: one-line summary followed by a blank line and details
  (Sphinx-compatible, triple-quoted double quotes).
- Logging: use the module `logger` (`logging.getLogger("apt-mirror")`).
  **Never** call `print()` for status output; **never** use `logging.basicConfig`
  again (it is configured once at import time).
- Error handling: raise `ValueError`/`FileNotFoundError` with descriptive
  messages prefixed `apt-mirror:` on user-facing config errors; avoid bare
  `except:` — catch the specific exception (the codebase already does).
- Filesystem access: use `pathlib.Path`; always resolve relative to the
  config, never hardcode paths.

## Tooling (recommended, keep stdlib-only for the app itself)

Dev tooling is separate from runtime dependencies:

```bash
# Formatting / lint (CI and pre-commit)
pip install ruff
ruff format apt-mirror.py
ruff check --select E,W,F,I,UP,B,SIM apt-mirror.py

# Static type checking (no runtime deps)
pip install mypy
mypy --python-version 3.10 --strict apt-mirror.py   # aim warnings-free over time

# Tests
pip install pytest
pytest
```

## Testing

- Tests live in `tests/` (pytest layout: `tests/test_*.py`), do not pollute
  the single-file entry point with test code.
- Tests must **not** perform real network downloads:
  - Monkeypatch `AptMirror.download_archive`, or fake a mirror under
    `tmp_path` (pytest fixture) and point the config `base_path` there.
  - Test pure logic directly: `get_var`, `_remove_double_slashes`,
    `_sanitise_uri`, `format_bytes`, config parsing.
- Every bug fix and new feature requires a regression test.
- Run the full test suite before considering any change complete:

```bash
pytest -q
```

## DevOps practices

### CI/CD

- Every push to the default branch and every PR must pass:
  1. `ruff format --check` and `ruff check`
  2. `pytest`
- The workflow should run on the matrix Python `3.9`, `3.10`, `3.11`, `3.12`
  and `3.13` (latest) on `ubuntu-latest`.
- Releases: tag with `vX.Y.Z` semantic versioning; a release workflow publishes
  a tarball. The tool ships as a single file — the "package" is `apt-mirror.py`
  plus `mirror.list` examples; consider also packaging with
  `python3-apt-mirror` naming for Debian.

### Deployment & operations

- **Idempotency:** running the tool twice must be safe (it skips unchanged
  files via `stat` + `filecmp`). Do not add state that breaks this.
- **Locking:** the tool acquires an exclusive `fcntl.flock` on the spool
  directory (`lock_file`). Do not launch it concurrently from cron without
  relying on that lock.
- **Log format:** `%(message)s` line-oriented output, designed for log
  collectors. Keep single-line messages; never log raw config contents that
  may include `proxy_password` / `private_key`.
- **Security:**
  - Credentials in config (`proxy_user`, `proxy_password`) must never appear
    in logs, error messages, or exception output.
  - TLS verification: the `no_check_certificate` config option exists for
    compatibility — document it as insecure when enabling.
- **cron usage (typical):**
  ```
  0 4 * * * root /usr/bin/python3 /usr/local/bin/apt-mirror.py /etc/apt/mirror.list >> /var/log/apt-mirror.log 2>&1
  ```

### Git workflow

- One commit per logical change; commit messages in imperative mood
  (`Parse tilde option in config`, not `Parsed ...`).
- Keep the working tree in a state where `pytest` passes; do not commit files
  under `base_path`, `*.deb`, or generated clean scripts.
- No secrets or local mirror configs in the repo (`.gitignore` should cover
  `mirror.list`, spool directories, `__pycache__/`, `.venv/`).

## Definition of done (any change)

1. `pytest -q` passes locally.
2. `ruff format --check` and `ruff check` pass.
3. No new third-party runtime dependencies.
4. No `print()`, no `time.sleep` for pacing (use rate limiting via `wget`),
   no hardcoded absolute paths outside config defaults.
5. Behavior verified against a real or fixture mirror config where feasible.
