"""Threaded stage download orchestration (spec §5.3 concurrency, §5.11 output)."""

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from apt_mirror.config import _config_on, get_var
from apt_mirror.errors import AptMirrorError
from apt_mirror.fetch import Fetcher, FetchError
from apt_mirror.uri import sanitise_uri
from apt_mirror.util import _strip_userinfo

#: Wall-clock format for the ``Begin time`` / ``End time`` progress lines.
_TIME_FORMAT = "%a %b %e %H:%M:%S %Y %Z"


@dataclass
class StageResult:
    """Outcome of one download stage.

    ``ok`` counts the URLs that fetched cleanly, ``failed`` holds one
    ``(url, reason)`` pair per URL that could not be fetched, and ``bytes``
    is the total number of bytes written on this call.  URLs and reasons
    are in the credential-free form, safe to display or persist.
    """

    ok: int
    failed: list[tuple[str, str]]
    bytes: int


def download_stage(
    stage: str,
    urls: list[str],
    dest_root: Path,
    fetcher: Fetcher,
    variables: dict[str, str],
    var_path: Path,
) -> StageResult:
    """Download one stage's URLs into ``dest_root`` on a worker pool.

    Prints ``Downloading <N> files using <M> threads...`` with
    ``M = min(nthreads, len(urls))``, then ``Begin time: <ts>``; one
    ``[done/total]`` line per completed URL; then ``End time: <ts>`` (both
    timestamps in the fixed ``%a %b %e %H:%M:%S %Y %Z`` format); and
    finally the summary ``<ok> ok, <failed> failed, <bytes> bytes
    transferred`` followed by one ``  FAIL <url> (<reason>)`` line per
    failed URL.  Persists ``<var_path>/<stage>-urls.txt`` (all queued URLs,
    one per line, sorted) and ``<var_path>/<stage>-log.txt`` (one
    ``OK <url> <bytes>`` or ``FAIL <url> <reason>`` line per URL).  Every
    URL displayed or written here is in the credential-stripped form
    (``userinfo@`` removed); the fetcher itself receives the full URL.
    Each URL lands at ``dest_root / sanitise_uri(url, _tilde, _plus)``,
    with ``_tilde`` / ``_plus`` read via ``get_var``.

    The workers pull URLs from a shared queue (work-stealing, spec §5.3).
    FetchError / FetchNotFound become a failed entry and the worker
    continues; a fatal AptMirrorError (e.g. a bad scheme) propagates out of
    download_stage.  A stage with no URLs returns an empty StageResult and
    prints and writes nothing.
    """
    total = len(urls)
    if total == 0:
        return StageResult(ok=0, failed=[], bytes=0)
    raw = get_var(variables, "nthreads")
    try:
        nthreads = int(raw)
    except ValueError as error:
        raise AptMirrorError(f"apt-mirror: invalid nthreads '{raw}'") from error
    if nthreads < 1:
        raise AptMirrorError(f"apt-mirror: invalid nthreads '{raw}' (must be >= 1)")
    n_workers = min(nthreads, total)
    tilde = _config_on(get_var(variables, "_tilde"))
    plus = _config_on(get_var(variables, "_plus"))
    print(f"Downloading {total} files using {n_workers} threads...")
    print(f"Begin time: {time.strftime(_TIME_FORMAT, time.localtime())}")

    url_queue: queue.Queue[str] = queue.Queue()
    for url in urls:
        url_queue.put(url)
    lock = threading.Lock()
    done = 0
    results: dict[str, tuple[str, int | str]] = {}

    def worker() -> None:
        """Fetch URLs off the shared queue until it is drained."""
        nonlocal done
        while True:
            try:
                url = url_queue.get_nowait()
            except queue.Empty:
                return
            display = _strip_userinfo(url)
            try:
                written = fetcher.fetch(url, dest_root / sanitise_uri(url, tilde, plus))
            except FetchError as error:
                entry = ("FAIL", error.reason)
            else:
                entry = ("OK", written)
            with lock:
                results[display] = entry
                done += 1
                print(f"[{done}/{total}]")

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        # Submit every worker up front: awaiting a future while the
        # generator expression is still being consumed would run the
        # workers one at a time, not concurrently (spec §5.3).
        futures = [pool.submit(worker) for _ in range(n_workers)]
        for future in futures:
            future.result()
    print(f"End time: {time.strftime(_TIME_FORMAT, time.localtime())}")

    ok = 0
    byte_count = 0
    failed: list[tuple[str, str]] = []
    for display, (status, value) in results.items():
        if status == "OK":
            ok += 1
            byte_count += value
        else:
            failed.append((display, str(value)))
    failed.sort()

    var_path.mkdir(parents=True, exist_ok=True)
    (var_path / f"{stage}-urls.txt").write_text(
        "".join(f"{display}\n" for display in sorted(results)), encoding="utf-8"
    )
    log_lines = [
        f"{status} {display} {value}"
        for display, (status, value) in sorted(results.items())
    ]
    (var_path / f"{stage}-log.txt").write_text(
        "".join(f"{line}\n" for line in log_lines), encoding="utf-8"
    )
    print(f"{ok} ok, {len(failed)} failed, {byte_count} bytes transferred")
    for display, reason in failed:
        print(f"  FAIL {display} ({reason})")
    return StageResult(ok, failed, byte_count)
