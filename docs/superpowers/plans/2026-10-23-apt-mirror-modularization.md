# apt-mirror Modular Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the 2,496-line single file `apt-mirror.py` into the `apt_mirror/` package with a class-based architecture, with zero behavior change.

**Architecture:** Ten responsibility modules plus a dependency-free `errors.py` leaf; the `_mirror_run` mega-function with its nested closures becomes an `AptMirror` orchestrator class (one method per stage) plus an `ArchiveBookkeeping` class in `run.py`; the root `apt-mirror.py` becomes a 6-line entry shim so `python3 apt-mirror.py <config>` keeps working. The existing pytest suite (in-process + 127.0.0.1 fixture-mirror e2e) is the behavior freeze and gates every commit.

**Tech Stack:** Python 3.12+, stdlib only; pytest + trustme (test-only); ruff (format + check, line-length 88).

**Spec:** `docs/superpowers/specs/2026-10-23-apt-mirror-modularization-design.md` (read it first; the "Frozen behavior" list is the acceptance contract).

## Global Constraints

- Python 3.12+ (`requires-python = ">=3.12"`); **no new third-party runtime dependencies**; test-only deps stay `pytest` and `trustme`.
- No new external-command requirements; fetching/decompression stay in-process stdlib; the `postmirror_script` subprocess stays.
- **Behavior freeze:** all pinned single-line stdout status prints (design spec §5.11, including mark sequences `Processing metadata files from releases [M...]` / `Processing indexes: [S...P...]` and cleaning summaries), stderr messages verbatim, exit codes (0 success; 1 on `AptMirrorError`), lock semantics (`fcntl.flock`, `<var_path>/apt-mirror.lock`, "apt-mirror is already running, exiting"), idempotency (If-Modified-Since/304, stat+filecmp, temp-file+rename), credential hygiene (proxy creds never in output), and the exact construction order `mkdir ×3 → Fetcher(variables) → lock` (a doubly-broken config must report the directory error first).
- **Move discipline:** code moves verbatim — signatures, names (including privates), and docstrings are preserved; only `self.` prefixes are added where a closure captured a local. Behavior changes disguised as cleanup are out of scope; if you spot a logic bug while moving, note it in your final message, do not fix it.
- Each new module declares the stdlib imports it needs; any module that references `logger` declares `logger = logging.getLogger("apt-mirror")` itself (same logger name → same instance). After each move, trim imports the root file no longer uses (ruff must stay clean).
- PEP 8 per repo convention: double quotes, type hints on all signatures, `snake_case` functions, `PascalCase` classes, `UPPER_SNAKE_CASE` module constants, one-line-summary-then-detail docstrings.
- Tests must not touch the network beyond `127.0.0.1`; e2e keeps spawning `[sys.executable, "apt-mirror.py", config]` with `cwd=repo root`.
- Every task ends with `pytest -q` fully green, `ruff format --check` + `ruff check` clean over `apt-mirror.py apt_mirror/ tests/`, and a commit.

**Note on TDD for this plan:** this is a behavior-preserving refactor, so there is no "new behavior" to drive red→green. Where a task adds a test, it is written **before** the move and must be green against the current implementation first — it pins current behavior (characterization) and must stay green after. The `--version` and API-surface tests in Task 8 are the one genuine red→green slice.

## Review Focus

Five input classes / failure modes the spec implies but the existing suite does not fully exercise; each gets a test in the task named:

1. **Doubly-broken config** (un-creatable `base_path` *and* invalid proxy): the directory error must be reported before any fetcher config error — the frozen mkdir→Fetcher order. → test in **Task 7**.
2. **Proxy URL error with embedded credentials**: `Fetcher` construction on an invalid proxy URL must name the option but never echo the `user:pass@` userinfo. → test in **Task 4**.
3. **`--version` exact output**: `main(["--version"])` prints exactly `apt-mirror (python3) 1.0.0` and returns 0. → test in **Task 8**.
4. **Shim run from a foreign cwd**: `python3 /abs/path/apt-mirror.py --version` from any cwd must work (script dir on `sys.path`). → test in **Task 8**.
5. **Missing config file**: `main([str(missing_path)])` exits 1 with `apt-mirror: invalid config file specified (<path>)` on stderr. → test in **Task 8**.

---

### Task 1: Rewrite AGENTS.md for the modular layout

Per the owner's instruction this happens **first**. The rewrite swaps the single-file mandate for the modular design so implementers and future agents work from the current source of truth.

**Files:**
- Modify: `AGENTS.md` (whole file)

**Interfaces:**
- Consumes: the approved design doc's target layout (§"Agreed target layout") and module-ownership rule.
- Produces: updated project guidance; no code interface.

- [ ] **Step 1: Rewrite the affected sections**

Sections to change (rest stays):
- **Project overview** — "single-file reimplementation" → "a modular stdlib-only Python package (`apt_mirror/`) with a thin `apt-mirror.py` entry shim"; entry point line → "the root `apt-mirror.py` shim (CLI) over the `apt_mirror/` package".
- **Repository layout** — replace the tree with the design doc's tree.
- Replace "The codebase is intentionally one file. Do not split it into modules…" with the **module-ownership rule** (verbatim intent from the design doc: new index formats → `index.py`/`release.py`; retry/timeouts/TLS → `fetch.py`; config options → `config.py`; stage-flow changes → `run.py`; new fatal/user-facing exceptions → `errors.py`; new modules only when a new responsibility appears, documented here). The stdlib-only / no-external-commands constraints carry over unchanged.
- **Code style** — add: class architecture note (`AptMirror` orchestrator with one method per stage + `ArchiveBookkeeping` in `run.py`; `Fetcher` in `fetch.py`); keep every other convention.
- **Tooling** — commands now cover the package: `ruff format apt-mirror.py apt_mirror/ tests/`, `ruff check apt-mirror.py apt_mirror/ tests/`, `mypy --python-version 3.12 --strict apt_mirror/ apt-mirror.py`.
- **Testing** — runtime import is `import apt_mirror` (the hyphenated file is loaded only by the root shim via `from apt_mirror.cli import main`); the `importlib` file-location hack for the `am` fixture is removed.
- **Definition of done** — item 2 commands updated to include `apt_mirror/`; item 3 unchanged in spirit (package is one unit, still stdlib-only, no new deps).

- [ ] **Step 2: Verify scope and baseline**

Run: `git diff --name-only` → Expected: only `AGENTS.md`.
Run: `pytest -q` → Expected: fully green (baseline before any code change; record the test count).

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "Document modular package layout in AGENTS.md"
```

---

### Task 2: Package skeleton + leaf modules (`errors.py`, `uri.py`, `util.py`)

**Files:**
- Create: `apt_mirror/__init__.py`, `apt_mirror/errors.py`, `apt_mirror/uri.py`, `apt_mirror/util.py`
- Modify: `apt-mirror.py` (strip moved code; add re-imports; trim now-unused imports)
- Test: existing suite (no new test)

**Interfaces:**
- Consumes: the root file's current definitions.
- Produces (home of each module; later tasks import from these exact names):
  - `apt_mirror/errors.py`: `AptMirrorError(Exception)` — imports **nothing** from the package.
  - `apt_mirror/uri.py`: `sanitise_uri(url: str, tilde: bool = False, plus: bool = False) -> str`, `remove_double_slashes(path: str, tilde: bool = False, plus: bool = False) -> str`.
  - `apt_mirror/util.py`: `round_number(x: float) -> str`, `format_bytes(n: int) -> str`, `_strip_userinfo(url: str) -> str`, `_quoted_path(path: str) -> str`, `copy_file(from_p: Path, to_p: Path, unlink_repl: bool) -> None` (imports `_quoted_path`), `locked(var_path: Path) -> Iterator[None]` (imports `AptMirrorError`).
  - `apt_mirror/__init__.py`: package docstring (placeholder for now; the usage text lands in Task 8) + re-exports: `AptMirrorError`, `sanitise_uri`, `remove_double_slashes`, `round_number`, `format_bytes`, `copy_file`, `locked`.

- [ ] **Step 1: Create `apt_mirror/__init__.py`**

Docstring: one line naming the package and that the public API is re-exported from the submodules. Then the six re-export imports (ruff-sorted). No `logger` here.

- [ ] **Step 2: Create `errors.py`, `uri.py`, `util.py` by moving**

Move each listed name **verbatim** (signatures, docstrings, bodies). Move the stdlib imports each body references into the new module (e.g. `re` and `posixpath` for `uri.py`; `shutil`, `filecmp`, `fcntl`, `contextlib` for `util.py`). Add `logger = logging.getLogger("apt-mirror")` to a module only if its moved code references `logger`.

- [ ] **Step 3: Rewire the root file**

Delete the moved blocks from `apt-mirror.py`. Add re-imports at the top for **only** the moved names the remaining root code still references (at minimum `AptMirrorError`, `sanitise_uri`, `remove_double_slashes`, `format_bytes`, `_strip_userinfo`, `copy_file`, `locked`: `_mirror_run` and the remaining modules' uses decide the exact set — let ruff's unused-import check confirm nothing is imported but unused).

- [ ] **Step 4: Verify**

Run: `pytest -q` → Expected: fully green, same count as the Task 1 baseline.
Run: `ruff format --check apt-mirror.py apt_mirror/ tests/ && ruff check apt-mirror.py apt_mirror/ tests/` → Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add apt-mirror.py apt_mirror/
git commit -m "Add apt_mirror package skeleton with errors, uri, and util modules"
```

---

### Task 3: Move config parsing into `apt_mirror/config.py`

**Files:**
- Create: `apt_mirror/config.py`
- Modify: `apt-mirror.py` (strip moved code; re-imports), `apt_mirror/__init__.py` (extend re-exports)
- Test: existing suite (`tests/test_config.py` is the home suite)

**Interfaces:**
- Consumes: `apt_mirror.errors.AptMirrorError` (Task 2).
- Produces (everything `config.py` owns): `MAX_EXPANSION_PASSES`, `_VAR_REF_RE`, `_MACHINE_TO_ARCH`, `DEFAULT_CONFIG`, `Repo`, `ParsedConfig`, `detect_defaultarch()`, `_expand_once(value, variables, where)`, `_expand_template(template, variables, where)`, `get_var(variables, key)`, `parse_rate(value) -> float`, `_config_on(value) -> bool`, `_arch_from_options(options) -> str | None`, `parse_config_line(line) -> dict | None`, `parse_config(path) -> ParsedConfig`.
- Produces into `__init__.py`: `Repo`, `ParsedConfig`, `parse_config`, `parse_config_line`, `get_var`, `detect_defaultarch`, `parse_rate`, `DEFAULT_CONFIG`, `MAX_EXPANSION_PASSES`.

- [ ] **Step 1: Create `apt_mirror/config.py` by moving**

Move the listed names verbatim. Intra-package import: `from apt_mirror.errors import AptMirrorError`. Move the stdlib imports the bodies need (`platform`, `re`, `logging` if referenced). `Repo` and `ParsedConfig` are dataclasses — move them together.

- [ ] **Step 2: Rewire root + `__init__`**

Root: strip the block (~lines 57–251 region except `uri`/`util` names already moved, plus `DEFAULT_CONFIG`/expand/parse bodies) and re-import exactly what the remaining root code references (`Repo`, `get_var`, `_config_on`, `detect_defaultarch`-derived bits are module-level only — ruff decides). `__init__.py`: add the nine re-exports (ruff-sorted).

- [ ] **Step 3: Verify**

Run: `pytest -q` → Expected: fully green.
Run: `ruff format --check apt-mirror.py apt_mirror/ tests/ && ruff check apt-mirror.py apt_mirror/ tests/` → Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add apt-mirror.py apt_mirror/
git commit -m "Move config parsing and variable expansion into apt_mirror.config"
```

---

### Task 4: Move the fetch layer into `apt_mirror/fetch.py`

**Files:**
- Create: `apt_mirror/fetch.py`
- Modify: `apt-mirror.py` (strip moved code; re-imports), `apt_mirror/__init__.py` (extend re-exports)
- Test: `tests/test_fetch.py` (add one characterization test)

**Interfaces:**
- Consumes: `apt_mirror.errors.AptMirrorError`; `apt_mirror.config._config_on`, `apt_mirror.config.DEFAULT_CONFIG`, `apt_mirror.config.parse_rate`.
- Produces: `FetchError(Exception)` (with `url`/`reason` attributes via `__init__(self, url: str, reason: str)`), `FetchNotFound(FetchError)`, `_TruncatedBodyError(http.client.HTTPException)`, `_basic_token(username, password) -> bytes`, `_Target` (dataclass), `_parse_target(url) -> _Target`, `RateLimiter` (`__init__(self, rate_bps: float)`, `pace(self, n_bytes) -> None`), `Fetcher` (`__init__(self, variables: dict[str, str], backoff_base: float = 1.0, max_redirects: int = 10) -> None`, `fetch(self, url: str, dest: Path) -> int`, and the static/instance helpers `_build_tls_context`, `_build_proxy_table`, `_connect`, `_range_start`, `_require_complete`, `_write_body`, `_walk_redirects`).
- Produces into `__init__.py`: `Fetcher`, `FetchError`, `FetchNotFound`, `RateLimiter`.

- [ ] **Step 1: Write the characterization test (proxy-error credential hygiene)**

Add to the existing proxy-error test class in `tests/test_fetch.py` (next to `test_out_of_range_proxy_port_is_an_apt_mirror_error`):

```python
def test_proxy_port_error_does_not_echo_credentials(self, am) -> None:
    """An invalid proxy URL with embedded credentials must name the
    option but never echo the user:pass@ userinfo (credential
    hygiene)."""
    with pytest.raises(am.AptMirrorError) as excinfo:
        am.Fetcher({"use_proxy": "on", "http_proxy": "http://secretx:secret@127.0.0.1:99999"})
    message = str(excinfo.value)
    assert "http_proxy" in message
    assert "secretx" not in message
    assert "secret@127.0.0.1" not in message
```

- [ ] **Step 2: Run it to verify it pins current behavior**

Run: `pytest tests/test_fetch.py -k "credentials" -v` → Expected: PASS (characterization of current no-echo behavior). If it FAILS, stop: the current code leaks credentials, which is a behavior change, out of scope — report it.

- [ ] **Step 3: Create `apt_mirror/fetch.py` by moving**

Move `FetchError`, `FetchNotFound`, `_TruncatedBodyError`, `_basic_token`, `_Target`, `_parse_target`, `RateLimiter`, and the entire `Fetcher` class (~lines 360–941) verbatim, including its static methods. Intra-package imports: `apt_mirror.errors`, `apt_mirror.config` (`DEFAULT_CONFIG`, `_config_on`, `parse_rate`). Standard lib: `base64`, `email.utils`, `http.client`, `posixpath`, `ssl`, `time`, `urllib.parse` (as referenced). If any moved body references `logger`, declare the module `logger`.

- [ ] **Step 4: Rewire root + `__init__`**

Root: strip the block; re-import `Fetcher`, `FetchError` (referenced by remaining root code — `_mirror_run`'s re-fetch paths and `download_stage` live in the root until Tasks 6/7). `__init__.py`: add `Fetcher`, `FetchError`, `FetchNotFound`, `RateLimiter`.

- [ ] **Step 5: Verify**

Run: `pytest -q` → Expected: fully green (the new test included).
Run: `ruff format --check apt-mirror.py apt_mirror/ tests/ && ruff check apt-mirror.py apt_mirror/ tests/` → Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apt-mirror.py apt_mirror/ tests/test_fetch.py
git commit -m "Move fetcher, rate limiter, and fetch errors into apt_mirror.fetch"
```

---

### Task 5: Move metadata parsing into `apt_mirror/release.py` and `apt_mirror/index.py`

Two independent, pure modules; two commits.

**Files:**
- Create: `apt_mirror/release.py`, `apt_mirror/index.py`
- Modify: `apt-mirror.py` (strip moved code; re-imports), `apt_mirror/__init__.py` (extend re-exports)
- Test: existing suite (`tests/test_release.py`, `tests/test_index.py`)

**Interfaces:**
- Consumes: `apt_mirror.errors.AptMirrorError`; `apt_mirror.config.Repo`; (release.py bodies may also reference `apt_mirror.uri` helpers — move whichever the bodies name).
- Produces in `release.py`: `HASH_STRENGTH`, `HashEntry`, `ReleaseInfo`, `parse_release(path) -> ReleaseInfo`, `IndexUrl` (with `by_hash_path` property), `dist_base(spec) -> str`, `_alternation(names)`, `_index_patterns(spec) -> list[re.Pattern[str]]`, `_strongest_advertised(entries) -> HashEntry | None`, `index_urls_for(spec, info) -> list[IndexUrl]`, `_known_base_paths(spec) -> list[str]`, `known_index_urls(spec) -> list[IndexUrl]`, `_RELEASE_NAMES`.
- Produces in `index.py`: `PackageRec`, `_read_index(path) -> str`, `iter_index_blocks(path) -> Iterator[dict[str, str]]`, `need_update(mirror_file: Path, size: int) -> bool`, `_index_size(raw) -> int`, `_packages_record(block, tree_prefix) -> PackageRec`, `_sources_records(block, tree_prefix) -> list[PackageRec]`, `parse_index(path, tree_prefix) -> list[PackageRec]`, `_INDEX_EXTENSIONS`, `_INDEX_PARSE_ERRORS`.
- Produces into `__init__.py`: `HashEntry`, `ReleaseInfo`, `IndexUrl`, `parse_release`, `dist_base`, `index_urls_for`, `known_index_urls`, `_known_base_paths` (re-exported under the underscore name — the tests reference it), `PackageRec`, `iter_index_blocks`, `parse_index`, `need_update`.

- [ ] **Step 1: Create `apt_mirror/release.py` by moving**

Move the listed names verbatim, including the module-level constants `_RELEASE_NAMES` (currently defined late, near `_mirror_run`) and `HASH_STRENGTH`. Intra-package import: `Repo` from `apt_mirror.config`, `AptMirrorError` if referenced.

- [ ] **Step 2: Verify release move**

Run: `pytest -q` → Expected: fully green.
Run: `ruff format --check apt-mirror.py apt_mirror/ tests/ && ruff check apt-mirror.py apt_mirror/ tests/` → Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add apt-mirror.py apt_mirror/
git commit -m "Move release metadata parsing into apt_mirror.release"
```

- [ ] **Step 4: Create `apt_mirror/index.py` by moving**

Move the listed names verbatim (the `PackageRec` dataclass, block iterator with compression handling via `gzip`/`bz2`/`lzma`, `need_update`, record builders, `parse_index`, and both private constants). Intra-package imports as referenced.

- [ ] **Step 5: Verify index move**

Run: `pytest -q` → Expected: fully green.
Run: `ruff format --check apt-mirror.py apt_mirror/ tests/ && ruff check apt-mirror.py apt_mirror/ tests/` → Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apt-mirror.py apt_mirror/
git commit -m "Move index parsing into apt_mirror.index"
```

---

### Task 6: Move `download.py` and `clean.py`

Two small, independent modules; two commits.

**Files:**
- Create: `apt_mirror/download.py`, `apt_mirror/clean.py`
- Modify: `apt-mirror.py` (strip moved code; re-imports), `apt_mirror/__init__.py` (extend re-exports)
- Test: existing suite (`tests/test_stage.py`, `tests/test_clean.py`)

**Interfaces:**
- Consumes (`download.py`): `apt_mirror.fetch.FetchError`; `apt_mirror.errors.AptMirrorError`.
- Produces in `download.py`: `StageResult` (dataclass), `download_stage(stage, urls, base_path, fetcher, variables, var_path) -> StageResult` with its nested worker exactly as today.
- Produces in `clean.py`: `Unneeded` (dataclass: `files`, `dirs`, `nbytes`), `collect_unneeded(mirror_path, clean_keys, skipclean, tilde, plus) -> Unneeded`, `write_cleanscript(cleanscript_path, mirror_path, files, dirs) -> None`.
- Produces into `__init__.py`: `download_stage`, `collect_unneeded`, `write_cleanscript`, `StageResult`, `Unneeded`.

- [ ] **Step 1: Create `apt_mirror/download.py` by moving**

Move `StageResult` + `download_stage` (with the nested `worker()`) verbatim.

- [ ] **Step 2: Verify**

Run: `pytest -q` → Expected: fully green.
Run: `ruff format --check apt-mirror.py apt_mirror/ tests/ && ruff check apt-mirror.py apt_mirror/ tests/` → Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add apt-mirror.py apt_mirror/
git commit -m "Move threaded stage downloader into apt_mirror.download"
```

- [ ] **Step 4: Create `apt_mirror/clean.py` by moving**

Move `Unneeded`, `collect_unneeded`, `write_cleanscript` verbatim (imports `AptMirrorError`, and `apt_mirror.uri` helpers if the bodies use them — they do for tree-key sanitising).

- [ ] **Step 5: Verify**

Run: `pytest -q` → Expected: fully green.
Run: `ruff format --check apt-mirror.py apt_mirror/ tests/ && ruff check apt-mirror.py apt_mirror/ tests/` → Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apt-mirror.py apt_mirror/
git commit -m "Move cleanup collection into apt_mirror.clean"
```

---

### Task 7: Extract the `AptMirror` orchestrator (`apt_mirror/run.py`)

The structural heart of the refactor: the `_mirror_run` body (currently ~415 lines with nine nested closures) becomes a class whose per-stage methods are the former closures, with captured locals promoted to instance attributes.

**Files:**
- Create: `apt_mirror/run.py`, `tests/test_run.py`
- Modify: `apt-mirror.py` (replace `_mirror_run`/`run_postmirror` with a delegate), `apt_mirror/__init__.py` (extend re-exports)
- Test: `tests/test_run.py` (new, one characterization test)

**Interfaces:**
- Consumes: `apt_mirror.config` (`ParsedConfig`, `Repo`, `_config_on`, `get_var`), `apt_mirror.uri`, `apt_mirror.util` (`copy_file`, `format_bytes`, `locked`), `apt_mirror.fetch` (`Fetcher`, `FetchError`), `apt_mirror.download.download_stage`, `apt_mirror.release` (`dist_base`, `parse_release`, `index_urls_for`, `known_index_urls`, `_RELEASE_NAMES`, `HASH_STRENGTH`, `ReleaseInfo`), `apt_mirror.index` (`PackageRec`, `parse_index`, `need_update`, `_INDEX_EXTENSIONS`, `_INDEX_PARSE_ERRORS`), `apt_mirror.clean` (`collect_unneeded`, `write_cleanscript`), `apt_mirror.errors.AptMirrorError`.
- Produces:

```python
class ArchiveBookkeeping:
    """Accumulates the var/ALL, NEW, MD5, SHA1, SHA256 line lists and the
    archive download queue while archive indexes are parsed."""

    def __init__(self, mirror_path: Path) -> None: ...
    def add(self, spec: Repo, rec: PackageRec) -> None:
        """One PackageRec added once: mirror_rel → all lines; md5/sha1/
        sha256 records; and, when need_update(mirror_path/mirror_rel,
        size) is true, the credential-stripped NEW line plus the full
        (userinfo-carrying) URL → size in urls (spec §7.1 fix #2)."""
    def write_var_files(self, var_path: Path) -> None:
        """Truncates and writes ALL/NEW/MD5/SHA1/SHA256 under var_path,
        one line-terminated entry each, in that order."""
    @property
    def urls(self) -> dict[str, int]: ...   # full URL → size
    @property
    def need_bytes(self) -> int: ...        # sum of urls.values()

class AptMirror:
    """One full mirror pass driven by a ParsedConfig."""

    def __init__(self, config: ParsedConfig) -> None:
        """Stores config/variables; resolves tilde, plus, unlink_repl
        (via _config_on/get_var); resolves the mirror/skel/var/cleanscript
        Paths; seeds skipclean from config.skipclean and the per-run
        collections (hashsum_to_canonical, canonical_to_hashsums,
        release_urls, index_urls, index_urls_seen); sets fetcher to None.
        No side effects: no mkdir, no lock, no Fetcher."""
    def run(self) -> int:
        """mkdir ×3 (same AptMirrorError message as today) →
        fetcher = Fetcher(variables) → with locked(var_path):
        _stage_release, _stage_index, _stage_archive,
        _stage_mirror_copy, _stage_clean, _run_postmirror → return 0."""
    def _queue_release_urls(self) -> None: ...
    def _stage_release(self) -> None: ...
    def _valid_release_info(self, path: Path) -> ReleaseInfo | None: ...
    def _spec_release_info(self, spec: Repo) -> ReleaseInfo | None: ...
    def _queue_index_urls(self, spec: Repo) -> None: ...
    def _stage_index(self) -> None: ...
    def _read_index(self, spec: Repo, path: Path, url: str, tree_prefix: str) -> list[PackageRec]: ...
    def _process_index_spec(self, spec: Repo, index_rel: str, optional: bool) -> None: ...
    def _stage_archive(self) -> None: ...
    def _stage_mirror_copy(self) -> None: ...
    def _stage_clean(self) -> None: ...
    def _run_postmirror(self) -> None: ...

def run_postmirror(variables: dict[str, str]) -> None: ...  # moved verbatim
```

- [ ] **Step 1: Write the characterization test (error-priority freeze)**

Create `tests/test_run.py` (no new imports needed — the test uses only fixtures):

```python
"""Characterization tests for the AptMirror orchestrator."""


def test_directory_error_precedes_fetcher_error(
    tmp_path, am, capsys
) -> None:
    """Doubly-broken config: an un-creatable base_path AND an invalid
    proxy.  The frozen construction order (mkdir ×3 -> Fetcher) means
    the directory error is reported first and the proxy fault is not."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    config = tmp_path / "mirror.list"
    config.write_text(
        "set base_path "
        + str(blocker / "sub")
        + "\nset use_proxy 1\nset http_proxy http://127.0.0.1:99999\n",
        encoding="utf-8",
    )
    assert am.main([str(config)]) == 1
    captured = capsys.readouterr()
    assert "can't create" in captured.err
    assert "99999" not in captured.err
```

- [ ] **Step 2: Run it to verify it pins current behavior**

Run: `pytest tests/test_run.py -v` → Expected: PASS against the current `_mirror_run` (characterization). If it FAILS, stop and report — the frozen order differs from the spec.

- [ ] **Step 3: Create `apt_mirror/run.py` (the class extraction)**

Move `_mirror_run`'s body into `AptMirror` following this exact mapping (keep method names exactly; the first method is `_queue_release_urls` — the placeholder above is wrong by one word):

- Captured locals → instance attributes: `variables`, `tilde`, `plus`, `mirror_path`, `skel_path`, `var_path`, `cleanscript_path`, `unlink_repl`, `skipclean`, `hashsum_to_canonical`, `canonical_to_hashsums`, `release_urls`, `index_urls`, `index_urls_seen`, `fetcher`; `specs = config.binaries + config.sources` becomes a local in `run()` passed where the closures used it, and `config` is kept for `config.clean`/`config.sources`/`config.binaries`.
- Nested closures → methods (bodies verbatim, `self.` on captured names): `queue_release_urls` → `_queue_release_urls`; `valid_release_info` → `_valid_release_info`; `spec_release_info` → `_spec_release_info`; `queue_index_urls` → `_queue_index_urls`; `read_index` → `_read_index` (keep the `spec` parameter even though the body does not use it — verbatim move); `process_index_spec` → `_process_index_spec`.
- Inline stage blocks → methods: release stage → `_stage_release`; the index-stage block (marks loop → queue → print → download → by-hash fan-out) → `_stage_index`; archive-stage bookkeeping → `ArchiveBookkeeping` (`add` is exactly the per-record block at the top of `process_index_spec`'s record loop *minus* `skipclean.add`, which stays in `_process_index_spec` as `self.skipclean.add(rec.mirror_rel)`; `write_var_files` is the five-file truncated-write block), remainder → `_stage_archive`; skel→mirror copy loop → `_stage_mirror_copy`; cleaning block → `_stage_clean`; the `run_postmirror` module function stays standalone (moved verbatim) and `_run_postmirror` calls it, preserving today's exact call site and message behavior.
- The top-of-function mkdir loop, `Fetcher(variables)` construction, and `with locked(var_path):` stay in `run()` exactly in today's order.
- `main`'s current docstring/behavior is untouched here (Task 8 owns cli).

- [ ] **Step 4: Rewire root + `__init__`**

Root `apt-mirror.py`: replace the `_mirror_run` body and delete `run_postmirror` with:

```python
def _mirror_run(configfile: str) -> int:
    """See apt_mirror.run.AptMirror — behavior frozen by the test suite."""
    return AptMirror(parse_config(configfile)).run()
```

Trim root imports down to what `main`/`_mirror_run` need. `__init__.py`: add `AptMirror`, `ArchiveBookkeeping`.

- [ ] **Step 5: Verify**

Run: `pytest -q` → Expected: fully green (new test included; e2e basic/advanced/clean/index suites are the real gate — the pinned stdout strings and mark sequences must match byte-for-byte).
Run: `ruff format --check apt-mirror.py apt_mirror/ tests/ && ruff check apt-mirror.py apt_mirror/ tests/` → Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apt-mirror.py apt_mirror/ tests/test_run.py
git commit -m "Extract stage pipeline into AptMirror orchestrator class"
```

---

### Task 8: `cli.py`, the entry shim, conftest switch, and the API surface

The cutover: `main` moves to `cli.py`, the root file becomes the 6-line shim, and the test harness drops the `importlib` hyphen hack.

**Files:**
- Create: `apt_mirror/cli.py`, `tests/test_api.py`
- Modify: `apt-mirror.py` (becomes the shim), `apt_mirror/__init__.py` (final docstring + re-exports), `tests/conftest.py` (fixture)
- Test: `tests/test_api.py` (new)

**Interfaces:**
- Consumes: `apt_mirror.config.parse_config`, `apt_mirror.run.AptMirror`, `apt_mirror.errors.AptMirrorError` (by `cli.py`).
- Produces: `apt_mirror.cli.main(argv: list[str] | None = None) -> int` — identical behavior to today's root `main` (argparse `prog="apt-mirror.py"`, `configfile` positional defaulting to `/etc/apt/mirror.list`, `--version` printing exactly `apt-mirror (python3) 1.0.0` and returning 0, `AptMirrorError` → message to stderr + 1) but calling `AptMirror(parse_config(configfile)).run()` in place of `_mirror_run`.
- Produces: final root shim (exact content):

```python
#!/usr/bin/env python3
"""apt-mirror: mirror apt repositories (implementation in apt_mirror/)."""

from apt_mirror.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- Produces: final `apt_mirror/__init__.py` — the usage docstring moved from the old shim (the config-file-syntax block, lines 2–23 of the original file) as the module docstring, plus re-exports of: `main`, `AptMirror`, `ArchiveBookkeeping`, `AptMirrorError`, `DEFAULT_CONFIG`, `MAX_EXPANSION_PASSES`, `ParsedConfig`, `Repo`, `detect_defaultarch`, `get_var`, `parse_config`, `parse_config_line`, `parse_rate`, `FetchError`, `FetchNotFound`, `Fetcher`, `RateLimiter`, `StageResult`, `download_stage`, `HashEntry`, `IndexUrl`, `ReleaseInfo`, `dist_base`, `index_urls_for`, `known_index_urls`, `_known_base_paths`, `parse_release`, `PackageRec`, `iter_index_blocks`, `need_update`, `parse_index`, `Unneeded`, `collect_unneeded`, `write_cleanscript`, `copy_file`, `format_bytes`, `locked`, `round_number`, `sanitise_uri`, `remove_double_slashes`.
- Produces: `am` fixture = the `apt_mirror` package itself.

- [ ] **Step 1: Write the new tests**

Create `tests/test_api.py` with three tests (the fourth, the missing-config test, joins it in the same file):

```python
def test_public_api_surface(am) -> None:
    """The package re-exports the full public surface the suite uses."""
    names = [
        "AptMirror", "ArchiveBookkeeping", "AptMirrorError",
        "DEFAULT_CONFIG", "FetchError", "FetchNotFound", "Fetcher",
        "HashEntry", "IndexUrl", "MAX_EXPANSION_PASSES", "PackageRec",
        "ParsedConfig", "RateLimiter", "Repo", "ReleaseInfo",
        "StageResult", "Unneeded", "collect_unneeded", "copy_file",
        "detect_defaultarch", "dist_base", "download_stage",
        "format_bytes", "get_var", "index_urls_for", "iter_index_blocks",
        "known_index_urls", "locked", "main", "need_update",
        "parse_config", "parse_config_line", "parse_rate",
        "parse_release", "round_number", "sanitise_uri",
        "remove_double_slashes", "write_cleanscript",
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
    assert (
        "apt-mirror: invalid config file specified"
        in capsys.readouterr().err
    )


def test_shim_runs_from_other_cwd(tmp_path) -> None:
    """The root shim must work from any cwd (script dir on sys.path).
    --version needs no network or config."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "apt-mirror.py"), "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "apt-mirror (python3) 1.0.0" in result.stdout
```

(Test file imports: `import subprocess`, `import sys`, and `from .conftest import REPO_ROOT` — the repo root path the existing e2e tests already import this way.)

- [ ] **Step 2: Run to verify expected red/green mix**

Run: `pytest tests/test_api.py -v` → Expected: `test_public_api_surface` **FAILS** (no `main` on the package yet); the other three PASS against the current root implementation (characterization of pinned strings).

- [ ] **Step 3: Create `apt_mirror/cli.py`**

Move root `main` verbatim into `cli.py`, keeping its docstring (adjust "``argv`` defaults to ``sys.argv[1:]``" phrasing only if it has moved — otherwise touch nothing), `prog="apt-mirror.py"`, the exact version string, and the `AptMirrorError` → stderr + 1 handler. The body calls `AptMirror(parse_config(args.configfile)).run()`.

- [ ] **Step 4: Finalize `__init__.py`**

Set the package docstring to the moved usage text (the `set <key>`, `deb`, `deb-src`, `clean`, `skip-clean` syntax block from the original top-of-file docstring). Add the re-exports listed in Interfaces (ruff-sorted).

- [ ] **Step 5: Shrink the root shim**

Replace `apt-mirror.py` entirely with the 6-line shim from Interfaces (keep the executable bit — edit in place, do not delete+create).

- [ ] **Step 6: Rewrite the conftest fixture**

In `tests/conftest.py`: delete `load_aptmirror()` and the hyphen rationale in the module docstring; the session fixture becomes:

```python
@pytest.fixture(scope="session")
def am() -> ModuleType:
    """The apt_mirror package, shared by all unit tests."""
    import apt_mirror

    return apt_mirror
```

(`ModuleType` stays as the annotation type; drop now-unused `importlib` imports.)

- [ ] **Step 7: Verify**

Run: `pytest -q` → Expected: fully green — unit tests now resolve every `am.<name>` through the package `__init__`, and the e2e harness spawns the shim unchanged.
Run: `ruff format --check apt-mirror.py apt_mirror/ tests/ && ruff check apt-mirror.py apt_mirror/ tests/` → Expected: clean.
Run: `python3 apt-mirror.py --version` from the repo root and from `/tmp` → Expected: `apt-mirror (python3) 1.0.0` both times.

- [ ] **Step 8: Commit**

```bash
git add apt-mirror.py apt_mirror/ tests/
git commit -m "Make apt-mirror.py an entry shim over apt_mirror.cli"
```

---

## Self-review notes (run by the author of this plan)

- **Spec coverage:** layout → Tasks 2–8; `AptMirror`/`ArchiveBookkeeping` → Task 7; `Fetcher` order freeze → Task 7 test; shim + `cli` → Task 8; test/conftest changes → Task 8; AGENTS.md-first → Task 1; verification gate → every task's verify step. Nothing in the spec's "Goals" is unassigned.
- **Interface consistency:** `ArchiveBookkeeping(mirror_path)` / `add(spec, rec)` / `urls` / `need_bytes` / `write_var_files(var_path)` used identically in Task 7's mapping and verification; the orchestrator's release-method is spelled `_queue_release_urls` in both the Interfaces block and the Step 3 mapping.
- **Proportion:** code moves are specified by name lists + verbatim-move discipline, not transcribed; the two new-behavior-free characterization tests are shown in full because their assertions pin frozen strings.
