"""Characterization tests for the AptMirror orchestrator."""


def test_directory_error_precedes_fetcher_error(tmp_path, am, capsys) -> None:
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
