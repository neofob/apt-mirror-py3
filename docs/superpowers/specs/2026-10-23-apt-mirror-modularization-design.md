# Design: modularize apt-mirror.py into a class-based package

Date: 2026-10-23
Status: proposed (pre-approval)

## Intent and why

`apt-mirror.py` is a 2,496-line single file. The owner wants it refactored
into a modular, class-based package because the single file has become **hard
to maintain**. The concrete worst spot: `_mirror_run` (lines 2078–2492) is a
~415-line procedural function whose seven stages live as nested closures over
shared mutable state (`skipclean`, two by-hash maps, URL lists). Nothing in
there can be read, tested, or changed in isolation.

The target state (owner decision): a **modular package with a class
architecture** — split by responsibility, clear classes per concern — so that
a person who has never seen the codebase can add a feature (a new index type,
a retry policy) and have an obvious place to put it.

## Goals

1. Split the code into a package of modules, each owning one responsibility.
2. Extract the stage pipeline into an orchestrator class with one method per
   stage; the archive-stage line accumulation becomes a small bookkeeping
   class.
3. Keep `python3 apt-mirror.py <config>` working exactly as today
   (owner decision) via a thin entry shim at the repository root.
4. PEP 8 throughout (ruff format + ruff check clean, type hints, double
   quotes).
5. Rewrite `AGENTS.md` **first** to describe the new layout and module
   ownership, replacing the sections that currently mandate the single-file
   design (owner instruction).

## Non-goals

- No behavior change. The refactor is move-and-restructure only.
- No new third-party runtime dependencies; stdlib only (existing hard rule).
- No new external-command requirements (fetching/decompression stay
  in-process stdlib; the `postmirror_script` subprocess exception is kept).
- No CLI/argument changes (`configfile` positional, `--version`, default
  `/etc/apt/mirror.list`).
- No packaging/distribution work (no console script, no installable dist) —
  the repo still ships the file plus the package; packaging can come later.

## Anti-goals

- Behavior changes disguised as cleanup: output strings, exit codes, locking,
  and ordering of the stage pipeline are frozen (see "Frozen behavior").
- An "obviously better" rewrite inside any module while moving it. The move
  is mechanical; logic stays as-is. If a logic bug is noticed, note it, don't
  fix it in this work.
- Splitting modules for the sake of symmetry (e.g. one module per function).

## Constraints

- Python 3.12+, `requires-python = ">=3.12"` (unchanged).
- ruff `line-length = 88`; repo convention is double quotes, snake_case
  functions, PascalCase classes, UPPER_SNAKE_CASE module constants,
  one-line-then-detail docstrings.
- Unit tests run against `apt-mirror.py` only today, and must not do more
  than `127.0.0.1` networking (fixture mirror server). This stays.
- Test-only deps remain `pytest` and `trustme`.
- Existing unit tests reference ~35 public names through a session fixture
  `am` (module load). The existing e2e harness (`tests/conftest.py`) runs the
  tool as a subprocess: `[sys.executable, "apt-mirror.py", config]`.

## Frozen behavior (regression gate)

- All pinned single-line stdout status prints (design spec §5.11), including
  the exact `Processing metadata files from releases [MMM...]` /
  `Processing indexes: [SSSPPS...]` mark sequences and the cleaning summary
  lines; blank-line placement included.
- stderr: `AptMirrorError` messages printed verbatim by `main` + exit 1;
  Release `info.warnings` printed verbatim; "Failed to find InRelease or
  Release in <path>" and the index warnings.
- Exit codes: 0 success (all stages), 1 on `AptMirrorError` (config,
  directory creation, lock), per-URL fetch failures never fatal.
- `--version` prints exactly `apt-mirror (python3) 1.0.0`.
- File locking: `fcntl.flock` non-blocking on `<var_path>/apt-mirror.lock`.
- Idempotency and atomic-download semantics (temp-file + rename).
- Credentials never appear in any output.
- Directory creation order (`mirror_path`, `skel_path`, `var_path`) before
  locking; stage order release → index → archive → skel-to-mirror copy →
  clean → postmirror.

## Agreed target layout

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

Module-ownership rule (written into AGENTS.md): new index formats go in
`index.py`/`release.py`; retry/timeouts/TLS changes go in `fetch.py`; config
options in `config.py`; stage-flow changes in `run.py`; new fatal/user-facing
exceptions in `errors.py`; network types in `fetch.py`. New modules are added
only when a new responsibility appears, and documented in AGENTS.md.

## Class architecture (decided design)

### `AptMirror` — orchestrator (`run.py`)

Replaces `_mirror_run` and its nested closures.

Where `AptMirrorError` lives (design call, not left to the builder): a
dependency-free leaf module `apt_mirror/errors.py` holds `AptMirrorError`.
`errors.py` imports nothing from the package, so every module that raises it
(`config`, `fetch`, `download`, `clean`, `run`, `cli`) can import it without
an import cycle. `FetchError`/`FetchNotFound` stay in `fetch.py` (they are
fetch-layer types). `__init__` re-exports `AptMirrorError`.

```python
class AptMirror:
    """One full mirror pass driven by a ParsedConfig."""

    def __init__(self, config: ParsedConfig) -> None:
        # Store config, variables, tilde/plus/unlink_repl flags, resolved
        # Paths (mirror/skel/var/cleanscript).  Initialize per-run state:
        # skipclean (from config.skipclean), hashsum_to_canonical,
        # canonical_to_hashsums, release_urls, index_urls,
        # index_urls_seen.  No Fetcher here (see run()).  No side effects:
        # no mkdir, no lock.
        ...

    def run(self) -> int:
        """Create the three directories, THEN build Fetcher(variables),
        take the lock, run the seven stages in the original order, return
        0.  AptMirrorError propagates to cli.main.

        Order matters (frozen behavior): the current code does
        mkdir ×3  ->  Fetcher(variables)  ->  lock.  Fetcher.__init__
        itself raises AptMirrorError on a bad limit_rate, an unreadable
        ca_certificate/certificate/private_key, or a malformed proxy URL.
        Deferring Fetcher construction to run() after the mkdirs (rather
        than in __init__) is what preserves that exact error-priority
        order, so a doubly-broken config still reports "can't create
        <path> directory" first as it does today.  Design call."""
        ...

    # One method per stage (bodies are the current closures/inlines,
    # with `self.` where the closures captured locals):
    def _queue_release_urls(self) -> None: ...
    def _stage_release(self) -> None: ...
    def _valid_release_info(self, path: Path) -> ReleaseInfo | None: ...
    def _spec_release_info(self, spec: Repo) -> ReleaseInfo | None: ...
    def _queue_index_urls(self, spec: Repo) -> None: ...
    def _stage_index(self) -> None: ...
    def _read_index(self, spec, path, url, tree_prefix) -> list[PackageRec]: ...
    def _process_index_spec(self, spec, index_rel, optional) -> None: ...
    def _stage_archive(self) -> None: ...
    def _stage_mirror_copy(self) -> None: ...
    def _stage_clean(self) -> None: ...
    def _run_postmirror(self) -> None: ...
```

Per-run state (`skipclean`, `hashsum_to_canonical`,
`canonical_to_hashsums`, `release_urls`, `index_urls`,
`index_urls_seen`) becomes instance attributes instead of closure captures —
that is the whole point of the refactor; no other state machine.

### `ArchiveBookkeeping` (`run.py`)

Small class that replaces the archive stage's five parallel lists plus the
URL-size dict (currently closure-captured `all_lines`, `new_lines`,
`md5_lines`, `sha1_lines`, `sha256_lines`, `archive_urls`):

```python
class ArchiveBookkeeping:
    """Accumulates the var/ALL, NEW, MD5, SHA1, SHA256 line lists and the
    archive download queue while archive indexes are parsed."""
    def add(self, rec: PackageRec, spec: Repo,
            tree_prefix: str) -> None: ...   # one PackageRec added once
    def write_var_files(self, var_path: Path) -> None: ...
    @property
    def need_bytes(self) -> int: ...
    @property
    def urls(self) -> dict[str, int]: ...    # full (userinfo) URL → size
```

`add` centralizes the rule that var/NEW lines carry the credential-stripped
URL (spec §7.1 fix #2) while `urls` keys keep the full URL.

### Unchanged classes

`Fetcher` (moved as-is to `fetch.py`), all dataclasses
(`Repo`, `ParsedConfig`, `ReleaseInfo`, `HashEntry`, `IndexUrl`,
`PackageRec`, `StageResult`, `Unneeded`).

## Public API and the shim

- `apt_mirror/__init__.py` re-exports **the full public surface** currently
  reachable as `am.<name>` in the tests, plus `MAX_EXPANSION_PASSES` and
  `_known_base_paths` (both referenced by tests). This keeps the unit-test
  diff minimal: they keep using the `am` fixture.
  - The long usage docstring (config file syntax) moves from the shim to
    `apt_mirror/__init__.py`.
- Root `apt-mirror.py` shrinks to a shim; Python puts the script's directory
  on `sys.path`, so the `import apt_mirror` works regardless of cwd:

  ```python
  #!/usr/bin/env python3
  """apt-mirror: mirror apt repositories (implementation in apt_mirror/)."""

  from apt_mirror.cli import main

  if __name__ == "__main__":
      raise SystemExit(main())
  ```

- `cli.main` keeps its exact behavior (argparse setup, `--version` string,
  `AptMirrorError` → stderr + 1) and calls
  `AptMirror(parse_config(configfile)).run()` in place of `_mirror_run`.

## Test changes

- `tests/conftest.py`: delete `load_aptmirror()` (the `importlib`
  file-location hack exists only because of the hyphenated filename); the
  session fixture becomes `import apt_mirror as am`. pytest's rootdir
  insertion (test package `tests/` → repo root on `sys.path`) makes the
  import work; if the runner changes that, conftest adds an explicit path
  insert.
- Unit test files: no reorganization and no logic changes; name references
  (`am.sanitise_uri`, `am.Fetcher`, …) keep working through the `__init__`
  re-exports.
- e2e harness unchanged: still spawns `python apt-mirror.py <config>`.

## AGENTS.md updates (done first, per owner instruction)

- Repository layout → the new tree above.
- Replace "The codebase is intentionally one file. Do not split…" with the
  module-ownership rule and the "stdlib only, no external commands"
  constraints (unchanged).
- Document the class architecture (`AptMirror`, `ArchiveBookkeeping`,
  `Fetcher`) and where new features go.
- Tooling/test sections: paths updated to `apt_mirror/ tests/`; the
  `importlib` hack and the `python -c` loading note removed; note that
  runtime imports of the tool are `import apt_mirror`.
- Definition of done: commands now cover the package
  (`ruff ... apt-mirror.py apt_mirror/ tests/`), everything else as-is.

## What's left to the builder

- Exact placement of small helpers wherever either of two candidate modules
  fits (e.g. `_basic_token` in `fetch.py`; the `email.utils`/hashlib
  usage in `release.py`).
- Whether `run_postmirror` keeps its standalone-function shape in `run.py`
  or becomes a method — either is fine as long as output matches.
- Docstring wording per module (keep the one-line-then-detail convention).
- Keeping ruff clean as the code moves (import ordering per module).
- Ordering of the migration commits (the plan can decide, e.g.
  move-then-restructure vs. interleave) — behavior must be identical and
  the suite green at the end of the work, always.

## Verification

The definition of done is the existing gate: `pytest -q` fully green against
the fixture mirror (stdout strings are asserted), `ruff format --check` and
`ruff check` over shim + package + tests, stdlib-only unchanged, no new
subprocess use. `--version` and the default-config read path are covered by
the suite; a manual smoke run against `mirror.list` (read-only inspection)
is a bonus, not a gate.
