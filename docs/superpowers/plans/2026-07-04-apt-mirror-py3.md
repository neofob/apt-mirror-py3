# apt-mirror (Python 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reimplement the Perl `apt-mirror` (this repo's `apt-mirror`) as a single-file, stdlib-only Python 3.12 tool (`apt-mirror.py`) with a local-HTTP fixture-based test rig, fixing the reviewed bugs.

**Architecture:** One self-contained file organized as: config parsing → path/byte helpers → HTTP fetch layer (`http.client`/`urllib`/`ssl`) with rate limiting, atomic downloads and work-stealing threads → three mirror stages (release/index/archive) driven by Release-file parsing → skel→mirror hardlinked copy → cleanup (walk + `clean.sh`/autoclean) → postmirror + flock. All tests run against a synthetic Debian repo served by a local `ThreadingHTTPServer`; the tool is exercised end-to-end as a subprocess.

**Tech Stack:** Python 3.12+ stdlib only at runtime (`http.client`, `urllib.request`, `ssl`, `concurrent.futures`, `queue`, `gzip`, `bz2`, `lzma`, `fcntl`, `hashlib`, `argparse`, `dataclasses`). Test-only pip: `pytest`, `trustme`. Lint via `ruff`.

**Spec:** `docs/superpowers/specs/2026-07-04-apt-mirror-py3-design.md` (all `§` references below are to it)

## Global Constraints

- Python 3.12 floor; PEP 8; ruff line length **88**; the tool is **one file**, `apt-mirror.py`, executable with `#!/usr/bin/env python3`.
- **Stdlib-only runtime.** pip packages only under `tests` / dev (`pytest`, `trustme`).
- Schemes: **http and https only**; any other scheme in a `deb` line → fatal error naming the URL and scheme (§5.3).
- Error messages go to **stderr** prefixed `apt-mirror:`, except the lock message which is exactly `apt-mirror is already running, exiting` (§5.2, §5.11).
- Exit codes: fatal config error / missing explicit config / lock held → **non-zero**; per-URL fetch failures, missing optional files, failing postmirror script → **warning, exit 0** (§5.11, §5.10).
- On-disk layout (spec §5.3/§5.7): release+index files at `<skel_path>/<sanitised url path>`, archives at `<mirror_path>/<sanitised path>`, lock at `<var_path>/apt-mirror.lock`, per-stage `var/{stage}-urls.txt` + `var/{stage}-log.txt`, plus `var/ALL`, `var/NEW`, `var/MD5`, `var/SHA1`, `var/SHA256`, cleanscript at `$cleanscript`.
- **Credentials (origin or proxy) must never appear** in log lines, exception text, or any written file (§5.3).
- Hash strength order: `SHA512 > SHA256 > SHA1 > MD5Sum` (§5.6.3).
- Retries: 5 attempts on connection errors, timeouts, HTTP 429/5xx with exponential backoff; ~30s socket timeout. One failed URL never aborts a stage (§5.3).
- Existing `mirror.list` format compatibility is a hard requirement — the repo's own `mirror.list` must parse (spec §3).
- `defaultarch` auto-detection order: `dpkg --print-architecture` → `platform.machine()` mapping (`x86_64`→`amd64`, `i686`→`i386`, `aarch64`/`arm64`→`arm64`, `armv7l`→`armhf`, `ppc64le`→`powerpc64el`, `s390x`→`s390x`) → `amd64` (§5.1).

## Review Focus

Uncovered-by-default failure modes that would bite a real user; each is pinned by a test added to the owning task:

1. **Credentials in a repo URI** (e.g. `http://user:s3cr3t@host/...`) — the run must succeed, and `s3cr3t` must not appear in stdout, stderr, or any file under `base_path`. → pinned in Task 9 (e2e).
2. **Stale lock file from a crashed run** (lock file exists, no process holds it) — a new run must acquire the lock and proceed normally. → pinned in Task 9 (e2e).
3. **CRLF line endings in `mirror.list`** (Windows-edited config) — parses identically to LF, no stray `\r` leaking into values or paths. → pinned in Task 1 (unit).
4. **Flat repos** (`deb <uri> <dist>` with no components) — files must land under `<mirror>/<host>/<dist>/...` (no `dists/` segment), and the unsigned fallback must try `<uri>/<dist>/Packages*`. → pinned in Task 5 (unit URLs) + Task 9 (e2e).
5. **Nonsense `limit_rate`** (`fast`, empty, negative, `10x`) — clean `apt-mirror:` error naming the key, no traceback. → pinned in Task 2 (unit).

---

### Task 1: Project skeleton, config parsing and validation

**Files:**
- Create: `pyproject.toml`
- Create: `apt-mirror.py` (module skeleton)
- Create: `tests/__init__.py` (empty), `tests/conftest.py` (loader + shared fixtures)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces (exact names, used by later tasks):
  - `class AptMirrorError(Exception)` — fatal; `str(e)` is the full user-facing message (already including the `apt-mirror:` prefix where one is specified).
  - `DEFAULT_CONFIG: dict[str, str]` — every key from spec §5.1 table (except `defaultarch`, which is resolved separately).
  - `detect_defaultarch() -> str` — the detection chain from Global Constraints.
  - `@dataclass(frozen=True) Repo: type: str; arch: str; uri: str; distribution: str; components: list[str] | None` — `type` is `"deb"` or `"deb-src"`; `components=None` means a flat repo.
  - `@dataclass ParsedConfig: variables: dict[str, str]; binaries: list[Repo]; sources: list[Repo]; clean: set[str]; skipclean: set[str]`
  - `parse_config_line(line: str) -> dict | None` — `None` for blank/`#` comment lines; a `dict` with keys `type` (+ `key`/`value` for `set`, `arch`/`uri`/`distribution`/`components` for deb lines, `uri` for `clean`/`skip-clean`); raises `ValueError` for a non-blank line that matches nothing.
  - `parse_config(path: str | Path) -> ParsedConfig` — reads the file (strips `\r`), seeds `variables` with `DEFAULT_CONFIG` + `defaultarch` (from `detect_defaultarch()`), applies `set` lines in file order, sanitises `clean`/`skip-clean` URIs with the *currently* known `_tilde`/`_plus`. Raises `AptMirrorError` naming the 1-based line number for: an unparseable line (`apt-mirror: invalid line in config file (<n>: <line> ...)`), a `set` with no value, and any other config validation failure.
  - `get_var(variables: dict[str, str], key: str) -> str` — returns `variables[key]` with `$name` references expanded recursively; raises `AptMirrorError` if `key` is not in `variables` (message names the key) or if expansion does not converge (> 16 passes).

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`. Cover at least:
  - `parse_config_line`: plain `deb`, `deb-src`, `deb-powerpc64`, `deb [arch=amd64,i386]`, `[arch=...]` overriding the suffix, a flat line (no components → `components=None`), a quoted `set` value (`set x 'a b'` → value `a b`), `clean` / `skip-clean` lines, comment and blank lines → `None`.
  - CRLF: every one of the above with `\r\n` stripped by `parse_config` — assert `variables` values and URIs contain no `\r`.
  - `parse_config` errors (write temp config files): unknown line → message contains `5:` line number and the line text; `set limit_rate` (no value) → `AptMirrorError` containing the line number; `$undefined` in a `set` value → `AptMirrorError` naming `undefined`; a valid value referencing a key set *earlier* in the file expands; a `$a`→`$a` self-loop → `AptMirrorError`.
  - `get_var`: direct value; nested `$base_path/mirror`; two levels deep; convergence error.
  - `detect_defaultarch` returns a non-empty string on the test host.
  - The repo's own `mirror.list`: `parse_config("mirror.list")` succeeds; assert 6 `binary` repos, 0 `sources`, `clean` set has 3 entries, `variables["nthreads"] == "4"`, `_tilde == "0"`, `unlink == "1"`.

- [ ] **Step 2: Run tests; verify they fail** (module missing / functions undefined).

Run: `python3 -m pytest tests/test_config.py -v`
Expected: collection error or all tests FAIL.

- [ ] **Step 3: Create `pyproject.toml`** with: `[project]` name `apt-mirror-py3`, version `1.0.0`, `requires-python = ">=3.12"`; `[project.optional-dependencies] test = ["pytest", "trustme"]`; `[tool.pytest.ini_options] testpaths = ["tests"]`; `[tool.ruff] line-length = 88`.
Then create the `tests/conftest.py` helper every test file needs:

```python
import importlib.util
from pathlib import Path

def load_aptmirror() -> ModuleType:  # name it so tests do "am = load_aptmirror()"
    spec = importlib.util.spec_from_file_location(
        "aptmirror", Path(__file__).parent.parent / "apt-mirror.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

(plus `import pytest` and an `am = load_aptmirror()` *fixture* used by all unit tests — define it once here as a session-scoped fixture named `am` returning the module).

Then create `apt-mirror.py`: shebang, module docstring with a short usage note, imports, and the interfaces above. Parsing approach: one `re` per recognized line shape (mirroring the original's semantics: `deb[-arch]`, optional `[options]` where only `arch=a,b` is honoured and other option keys are ignored, URI, distribution, components). `clean`/`skip-clean` URIs are sanitised at parse time with an internal `_sanitise_uri_now(raw, variables)` that strips `scheme://`, userinfo, and trailing `/` (tilde/plus encodings default off unless already set); Task 2 replaces this internal call with the real `sanitise_uri` — until then keep the function name stable.

- [ ] **Step 4: Run tests; verify they pass.**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add pyproject.toml apt-mirror.py tests/
git commit -m "Add project skeleton and config parsing with validation"
```

---

### Task 2: Path normalisation and byte/size helpers

**Files:**
- Modify: `apt-mirror.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: Task 1 errors (`AptMirrorError`).
- Produces:
  - `sanitise_uri(url: str, tilde: bool = False, plus: bool = False) -> str` — spec §5.4.
  - `remove_double_slashes(path: str, tilde: bool = False, plus: bool = False) -> str` — spec §5.4.
  - `round_number(x: float) -> str` — 1-decimal rounding, `.0` appended, trailing-zero trim (original semantics).
  - `format_bytes(n: int) -> str` — `KiB`/`MiB`/`GiB`/`bytes` (original thresholds at 1024).
  - `parse_rate(value: str) -> float` — bytes per second; suffix `k`/`K`=1e3, `m`/`M`=1e6, `g`/`G`=1e9; bare number OK; raises `AptMirrorError(f"apt-mirror: invalid limit_rate '{value}'")` for empty, negative, or non-numeric input (Review Focus 5).

- [ ] **Step 1: Write the failing tests** in `tests/test_paths.py`. Pin at least:
  - `sanitise_uri("http://h/debian/", ...) == "h/debian"`; `sanitise_uri("https://u:p@example.com:8443/p/") == "example.com/p"` (userinfo stripped — assert no `@` or `:` remains in host segment); tilde: `sanitise_uri("http://h/a~b", tilde=True) == "h/a%7Eb"`; plus analogously `%2B`.
  - `remove_double_slashes("h/a//b") == "h/a/b"`; `"h/a/../b"` → `"h/b"`; `"h/a/./b"` → `"h/a/b"`; `"a/../../b"` → `"b"` (no leading `/`); `//host/path` leading collapse safe; `"http://h"` unchanged (do not eat `://`).
  - `round_number`: `round_number(1.0) == "1.0"`, `round_number(1.96) == "2.0"`, `round_number(1.25) == "1.3"`, `round_number(0.05) == "0.1"`, negatives keep `-`.
  - `format_bytes`: `format_bytes(0) == "0 bytes"`, `format_bytes(1023) == "1023 bytes"`, `format_bytes(1024) == "1.0 KiB"`, `format_bytes(1536) == "1.5 KiB"`, `format_bytes(1024*1024) == "1.0 MiB"`, `format_bytes(5*1024**3) == "5.0 GiB"`.
  - `parse_rate`: `"100m" → 100_000_000.0`, `"1k" → 1000.0`, `"1048576" → 1048576.0`, `"1G" → 1e9`; each of `""`, `"fast"`, `"-5"`, `"10x"` raises `AptMirrorError` whose message contains the bad literal.

- [ ] **Step 2: Run; verify FAIL.**

Run: `python3 -m pytest tests/test_paths.py -v`
Expected: FAIL (functions missing).

- [ ] **Step 3: Implement the four/five functions** in `apt-mirror.py`. `remove_double_slashes` = loop of three `re.sub`s until stable (no `://` may collapse); `format_bytes`/`round_number` port the original's numeric behavior exactly (round half-up at 1 decimal: `int(n + 0.05) / 10`). Also: once `sanitise_uri` exists, **replace** the Task 1 `_sanitise_uri_now` placeholder with real `sanitise_uri(url, tilde, plus)` calls (passing the current `variables` so `_tilde`/`_plus` are honoured), and delete the placeholder.

- [ ] **Step 4: Run; verify PASS.**

Run: `python3 -m pytest tests/test_paths.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add apt-mirror.py tests/test_paths.py
git commit -m "Add URI sanitisation, path normalisation and size formatting"
```

---

### Task 3: Fixture HTTP server + fetch layer

**Files:**
- Modify: `tests/conftest.py` (add `FixtureServer`)
- Modify: `apt-mirror.py`
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: Task 1 `get_var`/`AptMirrorError`, Task 2 `parse_rate`/`sanitise_uri`.
- Produces:
  - `class RateLimiter: def __init__(self, rate_bps: float)` ; `def pace(self, n_bytes: int) -> None` — sleeps so a steady stream of `pace(chunk)` calls averages no faster than `rate_bps`.
  - `class FetchError(Exception)` — carries `url: str` and `reason: str`; `class FetchNotFound(FetchError)`.
  - `class Fetcher:`
    - `def __init__(self, variables: dict[str, str])` — builds TLS context, proxy handler and rate limiter from the config keys in spec §5.1/§5.3; invalid `limit_rate` raises `AptMirrorError` (via `parse_rate`).
    - `def fetch(self, url: str, dest: Path) -> int` — full semantics of spec §5.3: scheme check; origin credentials → `Authorization: Basic` header (always when `auth_no_challenge`, else after a 401); change detection via `If-Modified-Since` (local mtime) → returns `0` on 304 without touching the file (**no ETag persisting** — spec §8 accepts If-Modified-Since alone); resume: existing `<dest>.part` of size N → `Range: bytes=N-`, 206 appends, 200 restarts; **atomic**: data always written to `<dest>.part` then `os.replace`d onto `dest` when complete; 5 retries + exponential backoff (1/2/4/8s, scaled by test-only `backoff_base`) on connection errors/timeouts/429/5xx; `404` → `FetchNotFound`; other 4xx → `FetchError`; returns bytes written.
  - Test fixture (in `conftest.py`):
    - `class FixtureServer` (context manager) — `ThreadingHTTPServer` on `127.0.0.1:0`; `add(path, content: bytes, last_modified: float | None = None)`; `url(path) -> str`; `require_auth(username, password)` (rejects any request whose `Authorization: Basic` header does not match, with **401**); `flaky(path, fail_times, code=500)` (first N GETs to `path` return `code`, subsequent succeed); `drop_after(path, n_bytes)` (send `n_bytes` then close the connection); `request_log: list[dict]` — one entry per request: `method`, `path`, `headers` (dict, lower-cased keys); `count(prefix) -> int` (request count for paths starting with `prefix`).
    - A session-scoped pytest fixture `server` is **not** wanted (per-test isolation): expose `FixtureServer` directly and have tests use `with FixtureServer() as srv:`.

- [ ] **Step 1: Write the failing tests** in `tests/test_fetch.py`:
  - *plain download*: server file `b"hello world"`; `Fetcher({"limit_rate": "100m"}).fetch(srv.url("/a.txt"), tmp_path/"a.txt") == 11`; file content matches; no `a.txt.part` remains.
  - *atomicity + resume*: `drop_after("/big.bin", 50)` on a 200-byte file → `fetch` raises `FetchError` (after retries — add a test-only kwarg `backoff_base: float = 1.0` to `Fetcher.__init__` and pass `0.01` so tests stay fast); assert `big.bin` does **not** exist, `big.bin.part` exists with 50 bytes; re-add the full file (reset the drop behaviour) → `fetch` resumes from the `.part` via `Range` (206 logged), succeeds, final file is the full 200 bytes and no `.part` remains.
  - *304*: file with `last_modified = 1000.0`; fetch once; fetch again → returns `0`, `request_log` shows an `If-Modified-Since` header on the 2nd request, file mtime unchanged.
  - *retries*: `flaky("/x", 2, 500)` → fetch succeeds (3 requests logged).
  - *404*: fetch of missing path raises `FetchNotFound`; `.url` attribute on the exception contains the path.
  - *scheme guard*: `fetch("ftp://example.com/x", ...)` raises `AptMirrorError` whose message contains `ftp` and `example.com` (Global Constraints: http/https only).
  - *credentials*: `require_auth("u", "s3cr3t")`; fetch `srv.url("/sec.txt").replace("://", "://u:s3cr3t@")` with `auth_no_challenge=1` → succeeds; `request_log` shows a `Authorization` header whose value is the base64 of `u:s3cr3t`; also assert `"s3cr3t"` is **not** in `str(Any exception or log produced by fetch)`'s public strings (fetcher must not raise with credentials embedded — build the exception text from the *sanitised* URL).
  - *rate pacing*: `RateLimiter(100.0)`; `pace(100*3)` three times → elapsed `>= 2.5s` (loose bound); `RateLimiter(1e9).pace(10**6)` → elapsed `< 0.2s`.
  - *bad rate*: `Fetcher({"limit_rate": "fast"})` raises `AptMirrorError` (message contains `fast`).

- [ ] **Step 2: Run; verify FAIL** (FixtureServer/Fetcher missing).

Run: `python3 -m pytest tests/test_fetch.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement.** `FixtureServer`: subclass `BaseHTTPRequestHandler`; serve from an in-memory dict; honour `If-Modified-Since` (send 304 when `last_modified` is set and not newer than the header value) and `Range` (206 + `Content-Range`); log every request into `self.server.request_log`. `Fetcher`: `http.client`-based or `urllib.request`-based (builder's call); strip credentials from the URL **before** any object that could appear in an error is built, and build `FetchError` text from the credential-free URL (skel/mirror trees must contain only mirrored bytes — no sidecar/etag files).

- [ ] **Step 4: Run; verify PASS** (the pacing test may need a tolerant bound on loaded CI — keep the assertions as written and make the rate small enough that variance is impossible).

Run: `python3 -m pytest tests/test_fetch.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add tests/conftest.py apt-mirror.py tests/test_fetch.py
git commit -m "Add fetch layer with atomic downloads, resume, retries and fixture HTTP server"
```

---

### Task 4: Stage download orchestration

**Files:**
- Modify: `apt-mirror.py`
- Test: `tests/test_stage.py`

**Interfaces:**
- Consumes: Task 1 `get_var`, Task 2 `sanitise_uri`, Task 3 `Fetcher`/`StageResult`-adjacent errors.
- Produces:
  - `@dataclass StageResult: ok: int; failed: list[tuple[str, str]]; bytes: int` — `failed` items are `(url, reason)` with **sanitised** URLs/reasons (no credentials).
  - `download_stage(stage: str, urls: list[str], dest_root: Path, fetcher: Fetcher, variables: dict[str, str], var_path: Path) -> StageResult`
    - no `urls` → returns empty `StageResult`, prints nothing.
    - prints `Downloading <N> files using <M> threads...` with `M = min(get_var(variables, "nthreads"), N)`, then `Begin time: <ts>` and `End time: <ts>` (format `%a %b %e %H:%M:%S %Y %Z`), a `[done/total]` progress tick per completion (best-effort, single line), and a summary: `<M> ok, <K> failed, <bytes> bytes` with one `  FAIL <url> (<reason>)` line per failure.
    - writes `<var_path>/<stage>-urls.txt` (all URLs, one per line, sorted) and `<var_path>/<stage>-log.txt` (`OK <url> <bytes>` / `FAIL <url> <reason>` per URL) — **every URL displayed or written anywhere in this function (urls file, log file, progress ticks, summary `FAIL` lines) uses the credential-stripped form** (drop `userinfo@`); the `Fetcher` itself still receives the full URL internally.
    - concurrency: `ThreadPoolExecutor(max_workers=M)` over a shared `queue.Queue` (work-stealing); `FetchNotFound`/`FetchError` → failed entry, worker continues; fatal `AptMirrorError` (e.g. bad scheme) propagates.
    - destination of `url` is `dest_root / sanitise_uri(url, tilde, plus)` with `_tilde`/`_plus` from `variables` via `get_var`.

- [ ] **Step 1: Write the failing tests** in `tests/test_stage.py`:
  - *all succeed*: 10 files on `FixtureServer`; `download_stage("unit", urls, tmp/"root", fetcher, vars, tmp/"var")` → `ok == 10`, `failed == []`, all files under `tmp/"root"/"127.0.0.1:<port>/..."` with correct content; `var/unit-urls.txt` has 10 lines; `var/unit-log.txt` 10 `OK` lines; `result.bytes` equals total size.
  - *one 404*: delete one file from the server → `ok == 9`, `len(failed) == 1`, failed URL is the missing one, reason mentions `404`; stage did not raise.
  - *nthreads cap*: 1 URL with `nthreads=20` → stdout contains `using 1 threads` (capture with `capsys`); 12 URLs with `nthreads=3` → `using 3 threads`; all still complete.
  - *empty*: `download_stage("empty", [], ...)` → zero files printed (`capsys` readout has no `Downloading`).
  - *credentials never leak*: server `require_auth` + URL with `u:s3cr3t@`; run a stage where one URL is bad (404 after auth) — assert `s3cr3t` is absent from captured stdout/stderr, `stage-log.txt` contents, and `stage-urls.txt` contents.

- [ ] **Step 2: Run; verify FAIL.**

Run: `python3 -m pytest tests/test_stage.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `download_stage`** in `apt-mirror.py` per the Interfaces block.

- [ ] **Step 4: Run; verify PASS.**

Run: `python3 -m pytest tests/test_stage.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add apt-mirror.py tests/test_stage.py
git commit -m "Add threaded stage download orchestration with per-stage logs"
```

---

### Task 5: Release parsing and index-URL generation

**Files:**
- Modify: `apt-mirror.py`
- Test: `tests/test_release.py`

**Interfaces:**
- Consumes: Task 1 `Repo`, Task 2 `remove_double_slashes`.
- Produces:
  - `HASH_STRENGTH: tuple[str, ...] == ("SHA512", "SHA256", "SHA1", "MD5Sum")`
  - `@dataclass HashEntry: hashname: str; hashsum: str; size: int; filename: str`
  - `@dataclass ReleaseInfo: by_hash: bool; entries: dict[str, list[HashEntry]]; warnings: list[str]` — one entry-list **per filename**, aggregating every hash section it appears in.
  - `parse_release(path: Path) -> ReleaseInfo` — group headers `SHA512:/SHA256:/SHA1:/MD5Sum:` followed by 3-field continuation lines per spec §5.6.2; a continuation line with ≠ 3 fields → append to `warnings` (`Malformed checksum line "..." in <path>`) and continue; `Acquire-By-Hash: yes` exact-value → `by_hash=True`, **any other value** (e.g. `no`, `YES`) → `False` (pins original semantics; covered by test).
  - `@dataclass IndexUrl: filename: str; size: int; by_hash: str | None; by_hashsum: str | None` — `filename` is exactly the Release-file entry (e.g. `main/binary-amd64/Packages.gz`); `by_hash` is the **lower-cased** hash name of the file's strongest advertised hash, or `None` for a canonical download; `by_hash_path` property → `f"{posixpath.dirname(filename)}/by-hash/{by_hash}/{by_hashsum}"` (e.g. `main/by-hash/sha256/<digest>` — the same `dirname(url) + "/by-hash/" + hash + "/" + digest` construction the original used), and the absolute URL = `dist_base + by_hash_path` (canonical URL = `dist_base + filename`). The fixture must place by-hash files at exactly those paths.
  - `dist_base(spec: Repo) -> str` — absolute base URL of the dist: `<uri>/dists/<dist>/` or flat `<uri>/<dist>/` (trailing slash; use `remove_double_slashes` on the final tree-relative form later, not on the URL).
  - `index_urls_for(spec: Repo, info: ReleaseInfo) -> list[IndexUrl]` — filters `info.entries` filenames with the spec §5.6.2 pattern sets (binary: Contents, per-component Contents, `binary-<arch|all>/Packages`, per-component `Release`, cnf, dep11, i18n; source: Sources/`source/Release`/Contents-source; flat variants), picks each file's **strongest hash it itself advertises** from `info.entries[filename]` (Global Constraints order; `by_hash` only when `info.by_hash` is true — otherwise `by_hash=None` canonical), deduped by (filename, by_hash).
  - `known_index_urls(spec: Repo) -> list[IndexUrl]` — the unsigned-repo well-known set (all `IndexUrl(..., by_hash=None)`), extensions in order `.gz .xz .bz2 .lzma ""`; source lines include the `/dists/` segment (spec §7.1 fix #1); flat source → `<uri>/<dist>/Sources<ext>` (pins Review Focus 4).

- [ ] **Step 1: Write the failing tests** in `tests/test_release.py`. Hand-write a realistic multi-section Release text into a temp file and assert:
  - all four sections parsed; a filename listed in both SHA256 and SHA512 → `entries` has 2 `HashEntry` for it; `index_urls_for` picks the `sha512` `IndexUrl`.
  - *per-file strongest (spec §7.1 fix #2)*: with `by_hash=True` and a file listed **only under SHA1** → `index_urls_for` yields `by_hash == "sha1"` for it (the original silently dropped these files).
  - `Acquire-By-Hash: no` → `by_hash False` → all `IndexUrl.by_hash is None` (canonical).
  - `Acquire-By-Hash: yes` but a filename with no hash entries → canonical `IndexUrl`.
  - malformed continuation (`  deadbeef 123 name extra`) → one warning, no exception, other entries fine.
  - pattern coverage: for a binary spec `(arch=amd64, uri, stable, [main, contrib])` the generated set contains `main/Contents-amd64.gz`, `Contents-amd64.gz` (dist root), `main/Contents-all.gz`, `main/binary-amd64/Packages.gz`, `main/binary-all/Packages.gz`, `main/binary-amd64/Release` (no ext), `main/cnf/Commands-amd64.gz`, `main/dep11/Components-amd64.yaml.xz`, `main/dep11/icons-48x48.tar.xz`, `main/i18n/Translation-en.gz`, and `main/Contents-powerpc.gz` is **excluded**, `main/binary-i386/Packages.gz` **excluded**, `main/binary-amd64/Contents-foo` excluded.
  - source spec: `main/source/Sources.gz`, `main/source/Release`, `main/Contents-source.gz` included, `main/binary-amd64/Packages.gz` excluded.
  - `known_index_urls`: binary spec → contains `dists/stable/main/binary-amd64/Packages.gz` (all 5 extensions); **source** spec → `dists/stable/main/source/Sources.gz` (the fix — assert the `dists/` segment); flat source spec (`components=None`) → `Sources<ext>` without `dists/` **and** without `source/`.

- [ ] **Step 2: Run; verify FAIL.**

Run: `python3 -m pytest tests/test_release.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement** `parse_release`, `index_urls_for`, `known_index_urls`, `dist_base` in `apt-mirror.py`. Patterns: build `re` from the spec's lists verbatim (anchored, `arch` alternation `(?:<arch>|all)`).

- [ ] **Step 4: Run; verify PASS.**

Run: `python3 -m pytest tests/test_release.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add apt-mirror.py tests/test_release.py
git commit -m "Add Release parsing and index URL generation with per-file by-hash selection"
```

---

### Task 6: Index (Packages/Sources) processing

**Files:**
- Modify: `apt-mirror.py`
- Test: `tests/test_index.py`

**Interfaces:**
- Consumes: Tasks 1–2 (types, `AptMirrorError`).
- Produces:
  - `@dataclass PackageRec: mirror_rel: str; size: int; md5: str | None; sha1: str | None; sha256: str | None; url_rel: str` — `mirror_rel` = path relative to the mirror tree root (no leading slash), already `remove_double_slashes`-ed; `url_rel` = path relative to the **repo root** (download URL = `<uri>/` + `url_rel`): the `Filename:` value for Packages, `f"{directory}/{filename}"` for Sources.
  - `iter_index_blocks(path: Path) -> Iterator[dict[str, str]]` — decompresses by suffix (`.gz`/`.bz2`/`.lzma`; plain otherwise) using `gzip`/`bz2`/`lzma`; blocks separated by blank lines; each block → ordered `dict` of `Field:` values, continuation lines (leading space) appended to the previous field joined with `\n`, all values stripped of leading whitespace and trailing newlines (net effect of the original's `split(/^([\w\-]+:)/m)` + `remove_spaces` + `chomp`).
  - `need_update(mirror_file: Path, size: int) -> bool` — `True` if the file is missing; `False` if present with equal size; `True` **and** deletes the file if present with *different* size (spec §5.7).
  - `parse_index(path: Path, tree_prefix: str) -> list[PackageRec]` — a block with `Filename:` → Packages record (`md5/sha1/sha256` from `MD5sum:`/`SHA1:`/`SHA256:` when present, `size` from `Size:`, `url_rel = Filename:`, `mirror_rel = remove_double_slashes(tree_prefix + "/" + Filename)`); a block with `Files:` (Sources) → one record per `File:` line, each exactly 3 fields `<hash> <size> <filename>` else `AptMirrorError("apt-mirror: invalid Sources format")`, `md5 = hash`, `size` from the `File:` line, `url_rel`/`mirror_rel` from the `Directory:` value and filename (a `Directory:` with a leading slash is normalised away); blocks with neither field are ignored. `tree_prefix` = `sanitise_uri(repo uri)` — the repo's path in the mirror tree (e.g. `127.0.0.1:54321/repo`).

- [ ] **Step 1: Write the failing tests** in `tests/test_index.py` (all against temp files, no network):
  - hand-write a Packages text (2 entries: one amd64 with all three hashes + `Size: 47`, one `all` without `SHA256:`); gzip it; `parse_index` → 2 records with correct fields; `.gz` decompression actually happened (file on disk is compressed, fields parsed).
  - a bz2 and an lzma variant of the same content parse identically.
  - Sources text: 2 packages, one with a **multi-line** `File:` block (2 `File:` lines); `Directory: /pool/main/x/foo` → `mirror_rel == "pool/main/x/foo/foo_1.0-1.dsc"`; the `File:` hash lands in `md5`.
  - malformed `File:` line (`abc 123`) → `AptMirrorError` with the message text above.
  - `need_update`: absent file → `True`; present, same size → `False`; present, wrong size → `True` **and** the file is gone afterwards.
  - blocks separated by a single blank line; a field value containing a colon (`Homepage: http://x/`) does not split.

- [ ] **Step 2: Run; verify FAIL.**

Run: `python3 -m pytest tests/test_index.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement** `iter_index_blocks`, `need_update`, `parse_index` in `apt-mirror.py`.

- [ ] **Step 4: Run; verify PASS.**

Run: `python3 -m pytest tests/test_index.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add apt-mirror.py tests/test_index.py
git commit -m "Add Packages/Sources index parsing with corrupt-file detection"
```

---

### Task 7: Cleanup, skel→mirror copy, locking, and `main()`

**Files:**
- Modify: `apt-mirror.py`
- Test: `tests/test_clean.py`

**Interfaces:**
- Consumes: all of Tasks 1–6.
- Produces:
  - `copy_file(from_p: Path, to_p: Path, unlink_repl: bool) -> None` — skip when `from_p` missing; make parents; if `to_p` exists and `(st_size, st_mtime_ns)` match `from_p` → skip; `unlink_repl` (the config `unlink` option) and contents differ → delete `to_p` first; `os.link` with fallback `shutil.copy2` (cross-device); restore `from_p`'s atime/mtime onto `to_p`.
  - `@dataclass Unneeded: files: list[str]; dirs: list[str]; nbytes: int` — tree-relative POSIX paths.
  - `collect_unneeded(mirror_root: Path, clean_dirs: list[str], skipclean: set[str], tilde: bool, plus: bool) -> Unneeded` — spec §5.9 (recursive walk, `_tilde`/`_plus` normalisation before the skip-clean lookup, symlinks always needed, empty-dir-only removal, 512-byte block accounting, bottom-up so parents are removable once children are).
  - `write_cleanscript(path: Path, mirror_path: str, files: list[str], dirs: list[str]) -> None` — POSIX sh per spec §5.9 (shebang, `set -e`, `cd '<quoted mirror path>'`, `rm -f '...'` per file with the original `[NN%]`/`.` echo cadence — guard `total == 0` (Review: the original's divide-by-zero), then `test -d ... && rm -r` per dir), then `chmod +x`. Quoting: single-quote with `'` → `'\''` escaping (original `quoted_path`).
  - `@contextmanager def locked(var_path: Path)` — `flock(LOCK_EX | LOCK_NB)` on `<var_path>/apt-mirror.lock`; on failure raise `AptMirrorError("apt-mirror is already running, exiting")`; removes the lock file on exit; a pre-existing **stale** lock file (no live holder) must be acquirable.
  - `run_postmirror(variables: dict[str, str]) -> None` — spec §5.10/§5.10-cwd: cwd = `mirror_path` (expanded); executable → `subprocess.run([script])`, else `[ "/bin/sh", script ]`; prints the fixed messages; non-zero child exit → warning only.
  - `main(argv: list[str] | None = None) -> int` — wires everything per spec §5: argparse (`configfile` positional, default `/etc/apt/mirror.list`; missing explicit file → `AptMirrorError("apt-mirror: invalid config file specified")`; `--version` → print `apt-mirror (python3) 1.0.0` and return 0); create the three dirs; `locked(var_path)`; **release stage** (per spec §5.5, dest `skel_path`); corrupt/skel re-fetch (a Release/InRelease that exists but is empty or fails `parse_release` → delete it, re-`fetch()` it once, re-parse; still failing → warn **exactly** `Failed to find InRelease or Release in <skel_path>/<sanitised base>` + `known_index_urls` fallback — same treatment for index files that fail `iter_index_blocks`, deleting and re-fetching them first); **index stage** (per spec §5.6, printing `Processing metadata files from releases [` + one `M` per entry + `]`, deduping by URL across entries, registering every queued file — canonical **and** by-hash spellings — in `skipclean`, keeping the two by-hash maps `hashsum→[canonical]` and `canonical→[hashsum]`); **archive stage** (per spec §5.7: printing `Processing indexes: [` + `S`/`P` per spec entry + `]`; for each spec, the same per-entry index loops as the original — binary: `binary-<arch>` + optional `binary-all`/`debian-installer/binary-{arch,all}`; source: `source/Sources`; flat: single; missing/unparseable optional index → skip; build `PackageRec`s via `parse_index(path, tree_prefix=sanitise_uri(spec.uri, ...))`, write `var/ALL|NEW|MD5|SHA1|SHA256`, print `<size> will be downloaded into archive.`, then `download_stage("archive", ...)`); **skel→mirror copy** (spec §5.8 incl. by-hash hardlink fan-out); **cleanup** (spec §5.9, `_autoclean` branch deletes, else `write_cleanscript`); **postmirror** (`run_postmirror` when `run_postmirror` truthy); returns 0; any `AptMirrorError` → print `str(e)` to stderr, return 1.
  - Module tail: `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 1: Write the failing tests** in `tests/test_clean.py` (filesystem-only, no network):
  - *collect_unneeded*: build a mirror tree `a/b/file1` (needed via skipclean), `a/c/junk` (not), `a/d/` (only junk), a symlink `a/link -> file1` (dir `a/e` holds only the symlink → kept), 2048-byte junk → `nbytes == 4096` (block accounting); result: `files == ["a/c/junk"]`, `dirs == ["a/c", "a/d"]` (order: files as walked, dirs bottom-up), symlink dir untouched.
  - *tilde/plus lookup*: skipclean entry built with `_tilde` encoding matches a disk path containing the literal `%7E` form exactly as the original does (encode a `~` name in the tree and in skipclean identically).
  - *write_cleanscript*: empty lists → script still valid (no divide-by-zero; run `sh -n` on it and assert it exits 0 and contains `Removing 0 unnecessary files`); 3 files + 2 dirs → `rm -f` lines in order, a `[100%]` echo present, `rm -r` guarded, `cd` single-quoted; round-trip a path containing a single quote → renders `'\''`.
  - *copy_file*: fresh copy preserves mtime; equal-file second call no-ops (assert no change via `st_ino` stable under hardlink); `unlink_repl=True` with differing content → dest replaced (new ino), `unlink_repl=False` → original's in-place behaviour (link attempt); cross-device is out of scope for unit tests (covered by e2e on the real fs).
  - *locked*: open + flock the lock file in the test (using `fcntl`), then `locked(var)` raises `AptMirrorError` with the exact message; release + keep the stale file on disk → next `locked()` succeeds and **removes the file** on exit.

- [ ] **Step 2: Run; verify FAIL.**

Run: `python3 -m pytest tests/test_clean.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement** the functions and `main()` end-to-end per the Interfaces block. This is the integration step: run the real repo config through it once by hand against the local fixture server from Task 3 (manual spike, keep or throw) to shake out ordering bugs before the e2e rig exists.

- [ ] **Step 4: Run full unit suite; verify PASS.**

Run: `python3 -m pytest -q`
Expected: all PASS (config, paths, fetch, stage, release, index, clean).

- [ ] **Step 5: Commit.**

```bash
git add apt-mirror.py tests/test_clean.py
git commit -m "Add cleanup, skel-to-mirror copy, locking and main orchestration"
```

---

### Task 8: E2E rig — fixture repo, first run, idempotency, recovery

**Files:**
- Modify: `tests/conftest.py` (add `build_fixture_repo` + `write_config`)
- Test: `tests/test_e2e_basic.py`

**Interfaces:**
- Consumes: the completed tool (`python3 apt-mirror.py <config>` as a subprocess).
- Produces (test infrastructure used by Task 9):
  - `build_fixture_repo(root: Path, *, by_hash: bool = True, unsigned_src_repo: bool = True, insecure_repo: bool = True) -> dict[str, str]` — under `root`: one main repo `<root>/repo/dist` (dists layout, components `main`+`contrib`, arch `amd64`; `Release` with **all four** hash sections and `Acquire-By-Hash: yes` when `by_hash`; `InRelease` = Release + a fake `Signature:` block; **no** `Release.gpg` (404 tolerance); indexes: `main/binary-amd64/Packages.gz` (2 `.deb`s in `main/pool/...`), `main/binary-all/Packages` (uncompressed, 1 all-arch `.deb`), `main/Contents-amd64.gz`, `main/i18n/Translation-en.gz`, `main/source/Sources.gz` (1 source with 2 `File:` entries → `.dsc` + `.diff.gz` in `pool/`), and — **only when `by_hash`** — the by-hash copies at exactly the paths the Task 5 `IndexUrl.by_hash_path` URLs require; when `insecure_repo`, a second repo `<root>/insecure` with only `Release` (no InRelease) containing one tiny Packages; when `unsigned_src_repo`, a third repo `<root>/nosrc` carrying **only** raw `dists/nosrc/main/source/Sources*` files and nothing else (exercises the fixed unsigned-source fallback)). Returns a mapping of *role → http base URL*, e.g. `{"main": "http://127.0.0.1:P/repo", ...}`. Every generated index/Release must list real sizes and real digests of the generated files (hash every file with `hashlib`).
  - `write_config(base: Path, urls: dict[str, str], *, autoclean: str = "0", extra: list[str] | None = None) -> Path` — generates a `mirror.list` with `base_path → base`, low `nthreads` (2), `limit_rate 100m`, `run_postmirror` off, one `deb`/`deb-src` line per role, `clean` lines for each base URL, and an **executable** `postmirror.sh` that writes `$(pwd)` into `base/var/pwd.txt` (used by Task 9; harmless when off).
  - Helper `run_mirror(config: Path) -> subprocess.CompletedProcess` — `subprocess.run([sys.executable, "apt-mirror.py", str(config)], capture_output=True, text=True, timeout=120, cwd=<repo root>)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_e2e_basic.py`:
  1. *first run*: build fixture (all defaults), config with `main` + `nosrc` roles and a `deb-src` line for `main`; run → exit 0; assert under `base/mirror/`: Release+InRelease present (no Release.gpg), `main/pool/.../*.deb` (3 files incl. all-arch), `source` pool files (`*.dsc`, `*.diff.gz`), the by-hash **and** canonical index paths hardlinked (same `st_ino` for two), `Translation-en.gz` present; `var/ALL` contains the 3 pool paths; `var/NEW` non-empty on first run; `var/MD5` lines match `hashlib.md5` of the `.deb`s; the `nosrc` repo's `Sources` file appears under `mirror/<nosrc host>/dists/nosrc/main/source/`.
  2. *idempotency*: after the first run, note `srv.count("/pool/")`; run again → exit 0, `var/NEW` is empty, no `.deb` under `base/mirror` changed (content + mtime), and `srv.count("/pool/")` did **not** increase (on the second run every archive file passes `need_update`, so the archive stage is invoked with an empty URL list and transfers nothing).
  3. *corrupt `.deb`*: truncate one pool `.deb` to a wrong size → run → file restored byte-identical to the generated original (and listed in `var/NEW`).
  4. *changed file*: server-side, rewrite a `.deb` with new content + new size (rebuild its Release/index entries via `build_fixture_repo`'s internals or regenerate the whole repo under the same URL) → run → mirror copy updated to the new bytes.
  5. *crash recovery*: start the run, once `base/mirror/.../pool` exists `proc.kill()` (SIGKILL); assert exit non-zero; any `*.part` files are **orphaned** — then run again normally → exit 0, full tree correct, and zero `*.part` files anywhere under `base`. Additionally: truncate `base/skel/.../Release` to 10 bytes, run → the tool re-fetches it (parse recovery; assert `Release` content is complete again and indexes for that repo exist).
  6. *config parse guard*: `subprocess` with a non-existent config path → exit non-zero, stderr contains `apt-mirror: invalid config file specified`.

- [ ] **Step 2: Run; verify FAIL** (e2e rig missing).

Run: `python3 -m pytest tests/test_e2e_basic.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `build_fixture_repo`/`write_config`/`run_mirror`** in `conftest.py` and fix whatever tool behavior the tests expose (this is the payback for the whole plan — treat failing e2e as a tool bug unless the fixture is provably wrong; the spec fixes the expected file set).

- [ ] **Step 4: Run; verify PASS (and re-run once to confirm no flakiness).**

Run: `python3 -m pytest tests/test_e2e_basic.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add tests/conftest.py tests/test_e2e_basic.py apt-mirror.py
git commit -m "Add end-to-end rig: first run, idempotency, recovery scenarios"
```

---

### Task 9: E2E rig — cleanup, resilience, security, regression pins

**Files:**
- Test: `tests/test_e2e_advanced.py` (+ small `conftest.py` additions: a trustme-backed HTTPS `FixtureServer`, a flat-repo variant of `build_fixture_repo`, and a `require_auth`-based credential-leak fixture — the latter reuses Task 3's `require_auth`)

**Interfaces:**
- Consumes: Task 8 fixtures/helpers (`build_fixture_repo`, `write_config`, `run_mirror`).
- Produces: nothing (test-only task).

- [ ] **Step 1: Write the failing tests** in `tests/test_e2e_advanced.py`:
  1. *cleanup/autoclean*: after a first run, plant `mirror/<host>/junk.txt` + `mirror/<host>/emptydir/leaf.txt`; run with `_autoclean 1` → both gone, live content (incl. by-hash paths) intact, stdout contains `will be freed`. Plant again and run with `_autoclean 0` → `var/clean.sh` exists, is executable, its `rm -f` lines cover exactly the two planted paths (no live paths), then execute it with `sh` and assert the same deletion result and `done.` output.
  2. *404 tolerance*: server-side remove `InRelease` and one `Translation` index → run → exit 0, warnings on stderr, everything else complete.
  3. *locking*: hold `base/var/apt-mirror.lock` open+flocked in the test process → run → non-zero exit, stderr contains the exact `apt-mirror is already running, exiting`. Release the lock but **keep the stale file** → next run succeeds (Review Focus 2).
  4. *HTTPS*: serve a fixture repo over TLS with `trustme` certs; config `no_check_certificate 0` → run exits 0 overall but the HTTPS repo's files are **absent** with a TLS failure warning (survives); `no_check_certificate 1` → files present.
  5. *by-hash weak-hash regression (spec §7.1 #2)*: fixture variant where one index (e.g. `Contents-amd64.gz`) is listed in `Release` **only** under its `SHA1:` section (omit it from the others) → run → the file is present in the mirror (the original silently skipped it).
  6. *unsigned `deb-src` fallback regression (spec §7.1 #1)*: the `nosrc` role from Task 8 already exercises it; assert explicitly on `var/index-urls.txt` that the queued URLs contain the `dists/nosrc/main/source/` prefix.
  7. *config validation*: config with `set limit_rate` (bare) → non-zero, line number in stderr; config with `set mirror_path $nope` → non-zero, message names `nope` (spec §7.1 #3).
  8. *postmirror cwd*: config `run_postmirror 1` → `base/var/pwd.txt` content == `base/mirror` (spec §5.10).
  9. *credentials never leak (Review Focus 1)*: `require_auth("u", "s3cr3t")` server; `deb` line with `http://u:s3cr3t@127.0.0.1:P/...` and `auth_no_challenge 1` → run exit 0, files present, and `s3cr3t` absent from stdout+stderr and from every file under `base` (walk and scan).
  10. *flat repo (Review Focus 4)*: role `<root>/flat` with `flat/<dist>/Packages` (+ `.gz`) and pool files at repo root; `deb` line with no components → files land under `mirror/<host>/<dist>/...` (assert **no** `dists/` segment in the tree).
  11. *nthreads > urls*: config `nthreads 64` on a 3-URL stage → run exit 0, stage log shows `using 3 threads` (or fewer; assert no crash and all files present).
  12. *invalid limit_rate (Review Focus 5)*: config `limit_rate bananas` → non-zero exit, stderr contains `limit_rate` and no `Traceback`.

- [ ] **Step 2: Run; verify FAIL / gaps.**

Run: `python3 -m pytest tests/test_e2e_advanced.py -v`
Expected: FAILs exposing any missing behavior.

- [ ] **Step 3: Fix the tool** for each gap (spec §5/§7.1 are the arbiter; do **not** weaken a test to pass — fix the fixture only when it contradicts the spec).

- [ ] **Step 4: Run full suite twice; verify PASS.**

Run: `python3 -m pytest -q && python3 -m pytest -q`
Expected: all PASS both runs.

- [ ] **Step 5: Commit.**

```bash
git add tests/test_e2e_advanced.py tests/conftest.py apt-mirror.py
git commit -m "Add e2e coverage for cleanup, resilience, TLS, security and bug-fix regressions"
```

---

### Task 10: Docs, lint, final verification

**Files:**
- Modify: `AGENTS.md`
- May modify: `apt-mirror.py` (lint only)

- [ ] **Step 1: Update `AGENTS.md`**: the Python line becomes "3.12+ (stdlib only — no third-party runtime dependencies)"; replace the `wget` requirement bullet with "pure stdlib; no external commands required (dpkg is optional, for `defaultarch` auto-detection)"; note `pytest` + `trustme` as test-only dependencies and that tests must not touch the network beyond `127.0.0.1`.
- [ ] **Step 2: Lint clean.**

Run: `python3 -m ruff format --check apt-mirror.py tests/ && python3 -m ruff check apt-mirror.py tests/`
Fix whatever it flags (no semantic changes).

- [ ] **Step 3: Full verification.**

Run: `python3 -m pytest -q`
Expected: entire suite green.
Then: `cd <anywhere> && python3 /home/tuan/src/apt-mirror-py3/apt-mirror.py --version` → `apt-mirror (python3) 1.0.0`.

- [ ] **Step 4: Commit.**

```bash
git add AGENTS.md apt-mirror.py
git commit -m "Update AGENTS.md for the Python 3 implementation and pass lint"
```
