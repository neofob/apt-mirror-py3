# AGENTS.md

Guidance for AI coding agents (and human contributors) working in this repository.

## Project overview

`apt-mirror-py` is a Python 3, single-file reimplementation of the classic
Perl-based **apt-mirror** tool. It mirrors apt repositories (Packages,
Sources, Release, `.deb` archives), manages a local skel directory, generates
a clean script, and supports proxy, rate limiting, and
`.gz`/`.bz2`/`.xz` index handling.

- **Entry point:** `apt-mirror.py` (CLI, self-contained single file)
- **Target platform:** Debian-based Linux (pure-stdlib in-process HTTP
  fetching; `dpkg` is optional, for `defaultarch` auto-detection; `fcntl`
  file locking)
- **Python:** 3.12+ (stdlib only — no third-party runtime dependencies)

## Repository layout

```
apt-mirror-py/
├── apt-mirror.py   # Entire application: config parser, download engine, CLI
├── apt-mirror      # The original Perl tool, kept as the behavioral reference
├── mirror.list     # Example config (documented; ships with the tool)
├── pyproject.toml  # Tooling config only (ruff line length, test deps)
├── tests/          # pytest suite (127.0.0.1-only fixture mirror server)
├── docs/           # Design spec and review docs
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
- Pure stdlib; no external commands required (`dpkg` is optional, for
  `defaultarch` auto-detection — it falls back to a `platform.machine()`
  mapping, else `amd64`)
- Writes are confined to `base_path` (default `/var/spool/apt-mirror`) and the
  config-specified mirror/skel/var paths
- One exception to "no external commands": the user-supplied
  `postmirror_script` is executed via subprocess (`subprocess.run`) when
  `run_postmirror 1` — it is the operator's script, not tool behavior

## Code style — PEP 8 and project conventions

- Follow **PEP 8** (`ruff`-clean code). Target line length is 88 chars
  (configured in `pyproject.toml`).
- Type hints on function signatures (the codebase already uses them, e.g.
  `def get_var(variables: dict[str, str], key: str) -> str`). Keep new code
  consistent.
- Use **double quotes** for strings (existing convention).
- Naming: `snake_case` for functions/variables, `PascalCase` for classes,
  `UPPER_SNAKE_CASE` for module-level constants (e.g. `MAX_EXPANSION_PASSES`).
- Docstrings: one-line summary followed by a blank line and details
  (Sphinx-compatible, triple-quoted double quotes).
- Output: status lines go to **stdout** via `print()` — the fixed,
  single-line, machine-parseable strings pinned by the design spec §5.11
  (e.g. `Downloading <N> files using <M> threads...`,
  `Processing metadata files from releases [M...M]`,
  `Processing indexes: [SPS...SPS]`, the cleaning summary). Warnings and
  user-facing errors go to **stderr**: the module `logger`
  (`logging.getLogger("apt-mirror")`) for warnings, `print(...,
  file=sys.stderr)` for fatal `apt-mirror:`-prefixed errors. Do not configure
  logging (no `logging.basicConfig`); keep every message single-line.
- Error handling: raise `AptMirrorError` with a descriptive message prefixed
  `apt-mirror:` on user-facing config errors; avoid bare `except:` — catch
  the specific exception (the codebase already does).
- Filesystem access: use `pathlib.Path`; always resolve relative to the
  config, never hardcode paths.

## Tooling (recommended, keep stdlib-only for the app itself)

Dev tooling is separate from runtime dependencies:

```bash
# Formatting / lint (CI and pre-commit)
pip install ruff
ruff format apt-mirror.py tests/
ruff check apt-mirror.py tests/

# Static type checking (no runtime deps)
pip install mypy
mypy --python-version 3.12 --strict apt-mirror.py   # aim warnings-free over time

# Tests (test-only deps: pytest, trustme — see pyproject.toml)
pip install pytest trustme
pytest
```

## Testing

- Tests live in `tests/` (pytest layout: `tests/test_*.py`), do not pollute
  the single-file entry point with test code.
- Test-only dependencies are `pytest` and `trustme` (self-signed certs for
  the local HTTPS test server); they are declared in `pyproject.toml`
  (`[project.optional-dependencies].test`) and are never runtime deps —
  `apt-mirror.py` must stay a single stdlib-only file.
- Tests must **not** touch the network beyond `127.0.0.1`:
  - End-to-end tests run `apt-mirror.py` as a subprocess
    (`run_mirror` in `tests/conftest.py`) against a local `ThreadingHTTPServer`
    fixture repo under `tmp_path`, with the config `base_path` pointed there.
  - Unit tests exercise the pure logic directly: config parsing and `$var`
    expansion, `sanitise_uri`, `remove_double_slashes`, Release-file parsing,
    `format_bytes`, rate-limiter pacing, the clean-script writer.
- Every bug fix and new feature requires a regression test.
- Run the full test suite before considering any change complete:

```bash
pytest -q
```

## DevOps practices

### CI/CD

- Every push to the default branch and every PR must pass:
  1. `ruff format --check` and `ruff check` (over `apt-mirror.py` and `tests/`)
  2. `pytest`
- The workflow should run on the matrix Python `3.12` and `3.13` (latest) on
  `ubuntu-latest`.
- Releases: tag with `vX.Y.Z` semantic versioning; a release workflow publishes
  a tarball. The tool ships as a single file — the "package" is `apt-mirror.py`
  plus `mirror.list` examples; consider also packaging with
  `python3-apt-mirror` naming for Debian.

### Deployment & operations

- **Idempotency:** running the tool twice must be safe (it skips unchanged
  files via `If-Modified-Since`/304 and size checks, `stat` + `filecmp`, and
  downloads atomically via temp-file + rename). Do not add state that breaks
  this.
- **Locking:** the tool takes an exclusive non-blocking `fcntl.flock` on
  `<var_path>/apt-mirror.lock` and exits "already running" if it is held.
  Do not launch it concurrently from cron without relying on that lock.
- **Output format:** line-oriented, designed for log collectors — fixed
  single-line `print()` status lines on stdout (spec §5.11) and single-line
  warnings on stderr. Keep messages single-line; never output or log raw
  config contents that may include `proxy_user` / `proxy_password` or embedded
  origin credentials.
- **Security:**
  - Credentials in config (`proxy_user`, `proxy_password`, or embedded
    `user:pass@` URLs) must never appear in stdout, stderr, error messages, or
    exception output.
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
2. `ruff format --check apt-mirror.py tests/` and
   `ruff check apt-mirror.py tests/` pass.
3. No new third-party runtime dependencies (`apt-mirror.py` stays a single
   stdlib-only file; anything new belongs to the test rig).
4. No external commands (no `wget` or shell-outs for fetching or
   decompression); downloads and rate limiting are in-process stdlib (pacing
   via the `limit_rate` config option); status output stays the pinned
   single-line `print()` lines of spec §5.11; no hardcoded absolute paths
   outside config defaults.
5. Behavior verified against the fixture mirror config (a local `127.0.0.1`
   server) where feasible.
