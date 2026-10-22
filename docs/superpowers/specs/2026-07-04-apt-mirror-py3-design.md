# Design: apt-mirror for Python 3

**Date:** 2026-07-04
**Status:** Draft (pending owner approval)

## 1. Intent and why

Reimplement the Perl-based `apt-mirror` tool (this repo's `apt-mirror`, 1,169 lines)
as a clean **Python 3.12+** program, saved as `apt-mirror.py`. The motivation is to
retire the Perl/wget dependency stack: the tool should run on a minimal Debian box
with only `python3` preinstalled, do its fetching and decompressing in-process, and
keep credentials out of the process list. Behavior and the on-disk layout stay
compatible with the original so existing `mirror.list` configs and cron lines
keep working. Several known rough edges in the Perl version are fixed along the way.

## 2. Decisions the owner made

- **Clean reimplementation**, not a literal line-by-line port: keep the core
  features, fix issues where the original is awkward.
- **Python 3.12 or later**, **PEP 8** style.
- **Pure-Pythonic**: no reliance on external command-line tools for fetching
  or decompression (`wget`, `gzip`, `bzip2`, `xz` are no longer required).
- **pip packages are allowed** — but the owner delegated the runtime dependency
  choice to the builder's best judgment, and the builder's stated recommendation
  is **stdlib-only at runtime** (it costs zero and keeps the tool a single
  self-contained file). pip packages are used only for the *test* rig (e.g.
  `pytest`).
- **Include a test rig** in the deliverable (local fixture repo + automated
  end-to-end and unit checks).
- **Scope for bugs found in the original during review**: bugs like the
  ones in §7.1 — wrong URLs, silently skipped files, inconsistent
  config-error handling, corrupt-file survival — are **fixed, and each fix
  is documented** in §7.1 (the owner's explicit decision). The design's
  "port verbatim" language applies to documented behavior, not to bugs:
  where the two conflict, the fixed behavior wins and the test rig pins it.

## 3. Goals / non-goals / anti-goals

**Goals**

- Full functional parity with the current Perl script: config parsing, the
  three download stages (release / index / archive), Acquire-By-Hash support,
  size-based skip and corrupt-file retry, skel→mirror hardlinked copy,
  clean-script generation or `_autoclean`, `postmirror_script`, `flock`
  locking, stdout log output shape.
- Existing `mirror.list` files (including this repo's `mirror.list`) run
  unmodified.
- Every config variable the Perl version accepts is accepted with the same
  meaning (see §5.1).
- End-to-end verification against a local HTTP fixture repo, plus unit tests
  for the pure logic.
- Fix the issue list in §7.

**Non-goals**

- Third-party *runtime* dependencies (stdlib only — builder's call per §2).
- GPG signature verification of Release files (the original never verified
  signatures; apt itself is responsible for that).
- ftp:// mirroring. If a `deb` line uses a non-http(s) scheme, the tool
  fails fast with a clear error.
- Packaging work: no deb/sdist build, no systemd unit, no cron file
  generation, no CI, no manpage. (A short usage note may live in the module
  docstring.)
- Support for Python < 3.12.

**Anti-goals**

- It must not reimplement apt, validate signatures, or generate
  `sources.list` entries — that is the job of the user's postmirror script.
- It must not break the on-disk layout: downstream tooling and humans browse
  `base_path/mirror`, `base_path/skel`, `base_path/var`.

## 4. Deliverables

| Path | What it is |
|------|------------|
| `apt-mirror.py` | The tool. Single file, executable, `#!/usr/bin/env python3`. |
| `AGENTS.md` | Updated: Python floor corrected from 3.9+ to 3.12+ (the current text contradicts the owner's stated requirement); external-tool dependency wording updated (wget no longer required). |
| `tests/` | Test rig: `conftest.py` (fixture repo builder + local HTTP server), end-to-end tests, unit tests. |
| `pyproject.toml` | Minimal project metadata; test-only deps (`pytest`, `trustme`); tool config (`ruff`, `pytest`). Not a runtime requirement. |

The Perl `apt-mirror` file and `mirror.list` stay in the repo as reference.

## 5. Behavior specification

### 5.1 CLI and configuration

- Usage: `apt-mirror.py [configfile]` — default config `/etc/apt/mirror.list`;
  a non-existent explicit config is a hard error (message style:
  `apt-mirror: invalid config file specified`). A `--version` flag is a
  builder's courtesy; not a compatibility requirement.
- Config file lines: `#` comments, blank lines, then one of:
  - `set <key> [<value>]` — value may be bare, single- or double-quoted.
  - `deb` / `deb-src` / `deb-<arch>` lines: optional `[options]` (only
    `arch=a,b` is honoured; other keys accepted and ignored), URI,
    distribution, components. An `arch=` option overrides a `deb-<arch>`
    suffix; a line with **no components** is a *flat repo* (URL base is
    `<uri>/<distribution>/` instead of `<uri>/dists/<distribution>/`).
  - `clean <uri>` / `skip-clean <uri>` — URIs are sanitised into mirror-tree
    directory keys (see §5.4).
- Unknown line → hard error citing the 1-based line number and content
  (same as Perl).
- Variable expansion: `$name` inside values is expanded recursively from the
  config variables table; a substitution loop (> 16 passes) is a hard error.
- **Config validation is symmetric** (fixes an original inconsistency, see
  §7.1): `set <key>` with no value, and a `$name` reference to a key that
  was never set, are both hard errors naming the line number / key — the
  original died on the first with a generic "invalid line" message and
  silently expanded the second to an empty string.

**Config variables** (defaults in parentheses; all of these must be accepted):

| Key | Default | Meaning |
|-----|---------|---------|
| `defaultarch` | `dpkg --print-architecture`, else `platform.machine()` mapping (`x86_64`→`amd64`, `i686`→`i386`, `aarch64`/`arm64`→`arm64`, `armv7l`→`armhf`, `ppc64le`→`powerpc64el`, `s390x`→`s390x`), else `amd64` | Architecture for bare `deb` lines |
| `nthreads` | `20` | Concurrent fetch threads per stage |
| `base_path` | `/var/spool/apt-mirror` | Root; `mirror_path`, `skel_path`, `var_path` default under it |
| `mirror_path` | `$base_path/mirror` | Where the published mirror tree lives |
| `skel_path` | `$base_path/skel` | Staging area for release/index files |
| `var_path` | `$base_path/var` | Logs, URL lists, lock, generated files |
| `cleanscript` | `$var_path/clean.sh` | Output of the generated cleanup script |
| `postmirror_script` | `$var_path/postmirror.sh` | Script run after mirroring |
| `run_postmirror` | `1` | Whether to run it |
| `limit_rate` | `100m` | Max bytes/sec per transfer; suffix `k`=1e3, `m`/`M`=1e6, `g`/`G`=1e9 (wget semantics; `100m` ≈ unlimited in practice) |
| `use_proxy` | `off` | `on`/`yes` enables the proxies below |
| `http_proxy`, `https_proxy` | empty | Proxy URLs (credentials may be embedded) |
| `proxy_user`, `proxy_password` | empty | Separate proxy credentials (applied when `use_proxy` is on) |
| `certificate`, `private_key`, `ca_certificate` | empty | TLS client cert + key; custom CA bundle |
| `no_check_certificate` | `0` | Disable TLS verification (insecure; must not be required for normal operation) |
| `auth_no_challenge` | `0` | Send origin Basic auth for every request instead of only in response to 401 |
| `unlink` | `0` | When copying skel→mirror, replace a differing destination in place (delete then write) instead of link-or-copy-if-unchanged |
| `_autoclean` | `0` | Remove unneeded files immediately instead of generating `clean.sh` |
| `_tilde`, `_plus` | `0` | URL sanitisation: encode `~` as `%7E`, `+` as `%2B` |
| `_contents` | `1` | **Legacy no-op** — accepted for compatibility, never consulted (identical to the original) |

If `defaultarch` resolves empty → hard error:
`Please explicitly specify 'defaultarch' in mirror.list`.

### 5.2 Directories and locking

- All three paths are created (with parents) before any download.
- Before the first stage, take an exclusive non-blocking `flock` on
  `<var_path>/apt-mirror.lock`; if it is already held, exit with
  `apt-mirror is already running, exiting`. The lock file is removed on exit
  (success or failure). A lock file is *not* a skip-clean entry and is
  harmless if left behind by a crash.

### 5.3 Fetch layer (replaces wget)

A small in-process HTTP(S) client built on stdlib (`urllib.request` /
`http.client` / `ssl`) is the core of the new design:

- **Only** `http` and `https` schemes; anything else is a hard error naming
  the URL and scheme.
- **TLS**: default context (system CAs). `ca_certificate` → custom CA bundle.
  `certificate`+`private_key` → client cert (`load_cert_chain`).
  `no_check_certificate` → unverified context.
- **Origin credentials** `http://user:pass@host/...`: stripped from the URL
  and sent as a `Authorization: Basic` header — always when
  `auth_no_challenge` is on, otherwise only in response to a 401 challenge
  (which means one extra round-trip: request → 401 → request with header).
  Credentials must never appear in logs, exceptions, or any file written.
- **Proxy**: when `use_proxy` is on and a matching-scheme proxy URL is set,
  route through it (`ProxyHandler`); embedded proxy credentials or
  `proxy_user`/`proxy_password` (when on) produce `Proxy-Authorization`.
- **Retry**: up to 5 attempts on connection errors, timeouts, HTTP 429 and
  5xx, with exponential backoff (e.g. 1s, 2s, 4s, 8s — exact numbers are a
  builder's call). Socket timeout around 30s. After 5 failed attempts the URL
  is marked failed, a warning is logged, and the remaining work continues
  (one 404 — e.g. a missing `Release.gpg` on an unsigned repo — must not kill
  a mirror run).
- **Change detection** (replaces `wget -N`): when a local copy exists, send
  `If-Modified-Since` (its mtime) plus `ETag` if previously stored by us;
  HTTP 304 → file kept, nothing transferred. (Storing ETags is optional — a
  builder's call; `If-Modified-Since` alone is acceptable.)
- **Resume** (replaces the original's resume-less behavior): for a partial
  local file, send `Range: bytes=N-`; on 206 append, on 200 overwrite.
- **Rate limiting**: read the response in ~256 KiB chunks and pace the loop
  so each transfer averages ≤ `limit_rate` bytes/sec (per transfer, matching
  per-wget-process semantics). A rate this high that `limit_rate` implies
  should mean effectively zero overhead.
- **Downloads land** at `<stage_dir>/<sanitised relative URL path>` where
  `stage_dir` is `skel_path` for the release and index stages and
  `mirror_path` for the archive stage — byte-identical tree layout to the
  original. Parent directories are created as needed.
- **Atomic replacement**: every download writes to a temporary file in the
  destination directory (e.g. `<dest>.part-<pid>`) and is renamed into
  place only after a complete, clean download. A crashed or partially
  written file can therefore never clobber a previously good copy, so
  crashed runs are safe to repeat (the original wrote/`-N`-resumed in place
  — see §7.1).
- **Corrupt release/index recovery**: if a Release/InRelease or index file
  present in `skel` is empty or fails to parse, it is deleted and that URL
  is re-queued (a corrupt copy must not make every later run silently
  no-op; the original had no such check — see §7.1).
- **Concurrency**: per stage, a `ThreadPoolExecutor` with
  `max_workers = min(nthreads, #urls)`; workers pull URLs from a shared queue
  (work-stealing). A stage with zero URLs is skipped entirely.

### 5.4 URL sanitisation and path normalisation

Two functions, ported with identical observable behaviour:

- `sanitise_uri(url)`: strip `scheme://`; if the host segment contains `@`,
  strip the userinfo prefix (`user[:pass]@` or a bare `@`-containing host);
  if `_tilde`, replace `~` → `%7E`; if `_plus`, replace `+` → `%2B`; strip
  trailing `/`.
- `remove_double_slashes(path)`: repeatedly collapse `/.` → `/`, `//` → `/`
  (not across `://`), and `a/../` → ``; apply the same `_tilde`/`_plus`
  encodings. Used for every path *inside* the mirror tree.

These are exercised by unit tests against the original's outputs.

### 5.5 Stage 1 — release (skel)

For every `deb-src` and `deb` entry, download `<base>/InRelease`,
`<base>/Release`, `<base>/Release.gpg` (missing ones may 404 harmlessly)
into `skel_path`. 404s for these are warnings, never fatal.

### 5.6 Stage 2 — index (skel)

For each configured entry:

1. Open `<skel>/<sanitised base>/InRelease` (fall back to `Release`). If
   neither exists: warn
   `Failed to find InRelease or Release in <skel>/<base>` and fall back to
   the **well-known path loop**: for extensions `.gz .xz .bz2 .lzma ""`
   (binary: per component — `binary-<arch>/Packages`, `binary-all/Packages`,
   `Contents-<arch>`, `Contents-all`, plus `Contents-*` at the distribution
   root; source: `source/Sources`, `Contents-source`; flat repos: the flat
   variants) — exactly the file set the Perl version uses.
2. Parse the Release file's checksum sections. Recognise group headers
   `SHA512:`, `SHA256:`, `SHA1:`, `MD5Sum:` followed by continuation lines
   `  <hashsum> <size> <filename>` (3 whitespace-separated fields). Malformed
   continuation lines → warning naming the file. `Acquire-By-Hash: yes` is
   honoured. Note the Release lists files **relative to the repo root**;
   the matching filename patterns are ported verbatim from the Perl
   (Components, Contents, `binary-<arch|all>/Packages`, per-component
   `Release`, cnf, dep11, i18n for binary; Sources/`source/Release`/
   Contents-source for source; flat-repo variants).
3. **Acquire-By-Hash**: prefer the strongest available hash in the order
   `SHA512 > SHA256 > SHA1 > MD5Sum`; if none is supported, warn and fall
   back to canonical downloads. The file is downloaded **once** from
   `<base>/<name>/by-hash/<hash-lowercase>/<digest>`; the digest→canonical
   mapping (and the secondary canonical→other-digest mapping the Perl keeps
   for hash-variant dedupe) is preserved so every expected path in the
   mirror tree ends up with a hardlinked copy. If no hash is present for a
   filename, download from the canonical location.
   **Per-file hash selection** (fixes an original bug, see §7.1): each
   file is downloaded using the strongest hash *itself* advertises — not
   only the repo-wide strongest. In the original, a file listed only under
   a weaker section was recorded but **never downloaded**, so it was
   silently missing from the mirror.
4. Every file queued for a stage is registered in the **skip-clean** map
   (both its canonical and its by-hash spellings) so cleanup never removes
   live mirror content.

### 5.7 Stage 3 — archive (mirror)

- Open the downloaded index files from `skel_path`, decompressing in
  process (`gzip`/`bz2`/`lzma` modules; an uncompressed file is used as-is)
  — **no shell-out to external decompressors**. Decompressed contents are
  written to a temp file (or held in memory for small indexes) — the
  original's habit of leaving in-place uncompressed copies in the skel tree
  is not required to be preserved; only the set of files *copied into the
  mirror tree* must match the original.
- **Packages indexes**: entries with a `Filename:` field. For each: record
  the mirror-relative path in `var/ALL`; record `MD5sum:`/`SHA1:`/`SHA256:`
  fields in `var/MD5`/`var/SHA1`/`var/SHA256` (md5sum-style lines:
  `<hash>  <path>`). If the local file is missing, or its size differs from
  the index `Size:` field (corrupt → delete it), queue
  `<base>/<Filename>` for download and record it in `var/NEW`. File
  comparisons use a stat cache as in the original.
- **Sources indexes**: entries with a `Files:` field (possibly multi-line).
  Each `File:` value is `<hash> <size> <filename>`; the path is
  `<Directory>:/<filename>`. Same ALL/MD5/NEW bookkeeping; a malformed
  `File:` line (not 3 fields) is a hard error
  (`apt-mirror: invalid Sources format`).
- Which indexes are processed is driven by the same per-entry loops as the
  Perl (binary: per-component `binary-<arch>` + optional
  `binary-all`/`debian-installer/*` ones; source: per-component
  `source/Sources`; flat: single). A missing optional index is fine.
- Print `<size> will be downloaded into archive.` (human-readable size —
  see output below), then run the download stage.

### 5.8 Skel → mirror copy

For every release- and index-stage URL: copy
`skel/<sanitised url>` → `mirror/<sanitised url>`, creating directories,
preserving mtime/atime, preferring hardlink and falling back to copy
(cross-device). If the destination exists and its stat already matches, skip.
The `unlink` config option reproduces the original's in-place-replace
semantic when set. By-hash files additionally get hardlinked to every
canonical path in the mapping (both directions the Perl performs).

### 5.9 Cleaning

- Walk each `clean` directory (mirror-relative) recursively, bottom-up:
  files not in the skip-clean map (after `_tilde`/`_plus` normalisation)
  are listed for removal; a directory is removed only if it ends up empty
  of needed content; **symlinks are always considered live** (a directory
  containing only symlinks is kept). Sum removed-file sizes in
  512-byte block units for the reported byte count.
- `_autoclean` on: delete immediately, print
  `<size> in <N> files and <D> directories will be freed...`.
- Otherwise: generate `clean.sh` (POSIX sh, `set -e`, `cd '<mirror_path>'`,
  `rm -f '<file>'` lines with the original's `[NN%]`/`.` progress cadence,
  then `rm -r` for directories guarded by `test -d`), `chmod +x` it, and
  print `<size> in <N> files and <D> directories can be freed.` plus
  `Run <path> for this purpose.`
- With zero removable files the progress math must not divide by zero
  (the original can die here; guard it).

### 5.10 Post-mirror

If `run_postmirror` is truthy and the script exists: print
`Running the Post Mirror script ...` and run it — directly if executable,
else via `/bin/sh` — **with the working directory set to `mirror_path`**
(the original's `chdir` is still in force, and user scripts may rely on
it), then print the "Post Mirror script has completed" notice. A failing postmirror script does not change the process exit code
(documented; matches original). Finally release the lock.

### 5.11 Output and exit codes

- Human-readable, line-oriented stdout, close to the original's shape:
  `Downloading <N> files using <M> threads...`, stage progress
  (`Processing metadata files from releases [M...M]`,
  `Processing indexes: [SPS...SPS]`,
  `<size> will be downloaded into archive.`), timestamps in a fixed format
  (e.g. `Wed Jul  1 12:00:00 UTC 2026` — **not** `localtime()` raw), and a
  per-stage summary counting ok/failed URLs, listing failures with reasons.
- Sizes in `KiB`/`MiB`/`GiB`/`bytes` using the original's rounding helper
  (`round_number` ported: 1-decimal rounding, trailing-zero trimming).
- Errors and config failures: message to stderr prefixed `apt-mirror:`,
  non-zero exit. Config/lock failures are fatal; per-URL fetch failures,
  missing optional files, and a failing postmirror script are non-fatal.
- `var/<stage>-urls.txt` (the URL list) and `var/<stage>-log.txt`
  (per-URL result lines) are written, replacing the original's per-worker
  `stage-urls.$i`/`stage-log.$i` split.

## 6. Test rig (deliverable)

- **Fixture repo builder** (`tests/conftest.py`): generates a synthetic
  Debian-style layout under `tmp_path` — a `dists` repo with two components
  and one architecture, `Release` listing all four hash groups,
  `Acquire-By-Hash: yes`, a signed-looking but **unverified** `InRelease`,
  `Packages`/`Sources` (gzipped, plus at least one uncompressed index),
  `Contents-<arch>`, one `Translation-<lang>` file, and a handful of small
  dummy `.deb`/`.dsc`/`.diff.gz` files with correct hash and size entries;
  one repo with only `Release` (no `InRelease`) and one repo that has no
  Release at all (to exercise the well-known fallback) — flat or normal is
  the builder's choice. A `ThreadingHTTPServer` on `127.0.0.1:<free port>`
  serves it. A fixture mirrors `.list` config with
  `base_path → tmp_path`, low `nthreads`, and (where relevant)
  `_autoclean` / `use_proxy` off.
- **End-to-end tests** (run `apt-mirror.py` as a subprocess):
  1. First run: expected tree appears under `mirror/` (release files,
     indexes, by-hash *and* canonical paths hardlinked, all `.deb`s);
     `ALL`/`NEW`/`MD5`/`SHA1`/`SHA256` files populated correctly.
  2. Idempotent second run: no archive bytes re-downloaded; tree unchanged.
  3. Corrupt-file recovery: rewrite a `.deb` in the mirror with wrong size
     → re-downloaded and content restored on the next run.
  4. Change detection: bump mtime of an unchanged file on the server is
     irrelevant (304 or size-equal skip); a changed file *is* refreshed.
  5. Cleanup: delete a spurious file + directory in the mirror tree;
     run with `_autoclean` and with clean-script generation, and verify
     exactly the right set is removed and live content (incl. symlinks and
     by-hash paths) survives.
  6. Tolerance: 404s (drop `Release.gpg`, delete an index) warn, do not
     fail; a non-existent config file exits non-zero with the right message.
  7. Locking: hold the lock file open in the test process; the run exits
     non-zero with "already running".
  8. HTTPS: self-signed server (certs via pip `trustme`) with
     `no_check_certificate 1` works; without it the run reports a TLS
     failure for that repo but survives.
  9. Crash recovery: kill a first run mid-archive-download (SIGKILL the
     subprocess); no `.part` temp file survives and a second run completes
     with the full, correct tree. Also: corrupt a skel `Release` after a
     run (truncate it) → the next run detects the parse failure, re-fetches
     it, and mirrors normally.
  10. Regression tests for the §7.1 bug fixes: an unsigned `deb-src` repo
     (no Release) → `var/index-urls.txt` contains the `/dists/`-prefixed
     source paths; a by-hash repo where a file is listed only under a
     weaker (`SHA1`) section → the file still appears in the mirror tree.
  11. Config validation: `set key` with no value, and a `$name` reference
     to an unset key, each exit non-zero with a message naming the line /
     key.
  12. Post-mirror cwd: the fixture postmirror script records its `pwd`; the
     test asserts it equals `mirror_path`.
- **Unit tests** (no network): config line parsing (incl. `arch=`, quoted
  values, options, flat repos), `$var` expansion + loop detection, the
  symmetric config-validation errors, `sanitise_uri` / `remove_double_slashes`
  against the original's behavior, `format_bytes` / `round_number`,
  Release-file parsing (incl. per-file by-hash hash selection, per-file
  fallback, and malformed-line warnings), index-block URL generation for the
  unsigned `deb-src` fallback, index block parsing (multifield continuation,
  `Filename:` vs `Files:`), rate-limiter pacing, and the clean-script writer
  (incl. the zero-files divide-by-zero guard).

## 7. Issues fixed versus the original

1. No hard dependency on `gzip`/`bzip2`/`xz` binaries (original hard-fails
   at startup if any is missing, even for repos that don't use that format);
   in-process decompression instead of `system("gzip -d …")`.
2. Credentials (proxy or origin) never appear on any command line, log line,
   error message, or written file.
3. Work-stealing thread pool replaces round-robin forked wget lanes; an idle
   URL no longer stalls a lane.
4. Partial downloads resume via `Range` (the original's wget invocation had
   no resume at all).
5. Stage failures are summarised (failure counts and reasons) instead of
   buried in per-worker logs; timestamps are formatted, not `localtime()`
   raw.
6. Clean-script progress no longer divides by zero when there are no
   removable files.
7. `defaultarch` detection gains a `platform.machine()` fallback so the tool
   works on machines lacking `dpkg`.
8. Single `stage-urls.txt` / `stage-log.txt` per stage instead of
   `$i`-suffixed files, and fetch logs are per-URL result lines.
9. Config errors cite line numbers and the offending content.

### 7.1 Original bugs found in review — fixed, with owner approval

Per the owner's decision ("fix them"), the following defects of the original
Perl tool are **fixed** in the reimplementation, each pinned by a test from
§6. The "port verbatim" language elsewhere in this document applies to
documented behavior, never to these:

1. **Unsigned `deb-src` repos mirror to the wrong URLs.** The no-Release
   well-known path loop builds `<uri>/<distribution>/source/Sources`
   (missing the `/dists/` segment) for source lines, unlike the binary loop
   directly above it — so a normal (non-flat) unsigned source repo was never
   actually mirrored. Fixed: source fallback paths use the same
   `dists/<distribution>/…` construction as the binary fallback.
2. **By-hash repos silently skip files.** If `Acquire-By-Hash: yes` is set
   and a filename is listed only under a hash section weaker than the
   repo-wide strongest, the original records the path but never downloads
   the file. Fixed: each file is downloaded via the strongest hash *it
   itself* advertises (§5.6.3).
3. **Inconsistent config-error handling.** `set key` with no value dies with
   a generic "invalid line" message while a reference to an unset `$key`
   silently expands to the empty string. Fixed: both are hard errors naming
   the line / key (§5.1).
4. **Crash corruption sticks.** The original writes downloads in place (and
   its `wget -N` change check trusts local mtime), so a truncated Release or
   index left by a crashed run survives every later run — newer local mtime
   wins, the file parses to nothing, and the mirror silently stops updating.
   Fixed: atomic temp-file + rename downloads, and release/index files that
   are empty or unparseable are deleted and re-fetched (§5.3).

Explicitly **not** changed: `mirror.list` format, config key names and
semantics (including legacy ones), on-disk layout, lock name, postmirror
contract, stdout shape (modulo the listed improvements).

## 8. Left to the builder

- Internal file structure of `apt-mirror.py` (module layout, dataclasses,
  names) — PEP 8, clean separation of config / fetch / stages / cleaning /
  main.
- Exact backoff schedule, chunk size, ETag-storing yes/no (If-Modified-Since
  suffices), memory-vs-tempfile choice for index decompression.
- Fine wording of messages (must keep the `apt-mirror:` prefixes and overall
  shape).
- Exact contents of the synthetic fixture packages (dummy bytes with real
  hashes are fine).
- Whether to add a `--version` string and module docstring with usage.
- `pyproject.toml` content (metadata, `pytest` + `trustme` test extras,
  `ruff` line length 88, `pytest` options).

## 9. Open questions

None that the owner must answer; remaining questions are expected to be
resolved by the builder check or by the builder's judgment.
