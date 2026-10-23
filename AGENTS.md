# AGENTS.md

Guidance for AI coding agents (and human contributors) working in this repository.

## Project overview

`apt-mirror-py` is a Python 3 reimplementation of the classic Perl-based
**apt-mirror** tool: a modular, stdlib-only Python package (`apt_mirror/`)
with a thin `apt-mirror.py` entry shim. It mirrors apt repositories
(Packages, Sources, Release, `.deb` archives), manages a local skel
directory, generates a clean script, and supports proxy, rate limiting, and
`.gz`/`.bz2`/`.xz` index handling.

- **Entry point:** the root `apt-mirror.py` shim (CLI) over the `apt_mirror/` package
- **Target platform:** Debian-based Linux (pure-stdlib in-process HTTP
  fetching; `dpkg` is optional, for `defaultarch` auto-detection; `fcntl`
  file locking)
- **Python:** 3.12+ (stdlib only — no third-party runtime dependencies)

## Repository layout

```
apt-mirror-py/
├── apt-mirror.py          # Entry shim (~6 lines) — keeps the CLI stable
├── apt_mirror/            # The application (stdlib only)
│   ├── __init__.py        # Package docstring (the usage text) + public API
│   ├── errors.py          # AptMirrorError (dependency-free leaf module)
│   ├── cli.py             # main(), argparse, --version
│   ├── config.py          # Repo, ParsedConfig, parse_config(_line), $var
│   │                      #   expansion, get_var, detect_defaultarch,
│   │                      #   parse_rate, _config_on, DEFAULT_CONFIG,
│   │                      #   MAX_EXPANSION_PASSES, _MACHINE_TO_ARCH
│   ├── uri.py             # sanitise_uri, remove_double_slashes
│   ├── util.py            # format_bytes, round_number, _strip_userinfo,
│   │                      #   copy_file, locked (context manager)
│   ├── fetch.py           # Fetcher, FetchError, FetchNotFound,
│   │                      #   RateLimiter, _Target/_parse_target,
│   │                      #   _TruncatedBodyError, _basic_token,
│   │                      #   _build_tls_context/_build_proxy_table helpers
│   ├── download.py        # StageResult, download_stage (+ worker)
│   ├── release.py         # HashEntry, ReleaseInfo, IndexUrl,
│   │                      #   parse_release, dist_base, index_urls_for,
│   │                      #   known_index_urls, _known_base_paths,
│   │                      #   HASH_STRENGTH, _RELEASE_NAMES
│   ├── index.py           # PackageRec, iter_index_blocks, parse_index,
│   │                      #   need_update, _INDEX_EXTENSIONS,
│   │                      #   _INDEX_PARSE_ERRORS
│   ├── clean.py           # Unneeded, collect_unneeded, write_cleanscript
│   └── run.py             # AptMirror (orchestrator class),
│                          #   ArchiveBookkeeping, run_postmirror
├── tests/                 # Same files; conftest loader rewritten (below)
├── apt-mirror             # Original Perl tool (behavioral reference)
├── mirror.list, pyproject.toml, AGENTS.md, docs/
```

Module ownership: new index formats go in `index.py`/`release.py`;
retry/timeouts/TLS changes go in `fetch.py`; config options in `config.py`;
stage-flow changes in `run.py`; new fatal/user-facing exceptions in
`errors.py`; network types in `fetch.py`. New modules are added only when a
new responsibility appears, and must be documented in this file. Do **not**
introduce third-party dependencies (e.g. `requests`, `urllib3`) — the
stdlib-only design is still a contribution goal (easy deployment on minimal
servers).

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
- Class architecture: the `AptMirror` orchestrator has one method per
  stage, with `ArchiveBookkeeping` alongside in `run.py`; `Fetcher` lives
  in `fetch.py`. Place new responsibilities per the module-ownership rule
  above.
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
ruff format apt-mirror.py apt_mirror/ tests/
ruff check apt-mirror.py apt_mirror/ tests/

# Static type checking (no runtime deps)
pip install mypy
mypy --python-version 3.12 --strict apt_mirror/ apt-mirror.py   # aim warnings-free over time

# Tests (test-only deps: pytest, trustme — see pyproject.toml)
pip install pytest trustme
pytest
```

## Testing

- Tests live in `tests/` (pytest layout: `tests/test_*.py`), do not pollute
  the `apt_mirror/` package with test code.
- The runtime import of the tool is `import apt_mirror`; the hyphenated
  root `apt-mirror.py` is loaded only by the shim, via
  `from apt_mirror.cli import main`. The unit-test `am` session fixture is
  a plain `import apt_mirror` — the former `importlib` file-location hack
  is gone.
- Test-only dependencies are `pytest` and `trustme` (self-signed certs for
  the local HTTPS test server); they are declared in `pyproject.toml`
  (`[project.optional-dependencies].test`) and are never runtime deps —
  the `apt_mirror/` package must stay stdlib-only.
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
2. `ruff format --check apt-mirror.py apt_mirror/ tests/` and
   `ruff check apt-mirror.py apt_mirror/ tests/` pass.
3. No new third-party runtime dependencies (the `apt_mirror/` package is
   one unit, stdlib-only; anything new belongs to the test rig).
4. No external commands (no `wget` or shell-outs for fetching or
   decompression); downloads and rate limiting are in-process stdlib (pacing
   via the `limit_rate` config option); status output stays the pinned
   single-line `print()` lines of spec §5.11; no hardcoded absolute paths
   outside config defaults.
5. Behavior verified against the fixture mirror config (a local `127.0.0.1`
   server) where feasible.
