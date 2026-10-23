"""Unit tests for apt-mirror config parsing and validation (Task 1)."""

from pathlib import Path

import pytest

REPO_MIRROR_LIST = Path(__file__).parent.parent / "mirror.list"


def _crlf_content(*lines: str) -> str:
    """Join lines into a string with CRLF line endings."""
    return "\r\n".join(lines) + "\r\n"


class TestParseConfigLine:
    """Tests for parse_config_line (single lines, no file, no CRLF)."""

    def test_plain_deb(self, am) -> None:
        parsed = am.parse_config_line(
            "deb http://deb.example.com/debian stable main contrib"
        )
        assert parsed == {
            "type": "deb",
            "arch": None,
            "uri": "http://deb.example.com/debian",
            "distribution": "stable",
            "components": ["main", "contrib"],
        }

    def test_deb_src(self, am) -> None:
        parsed = am.parse_config_line(
            "deb-src http://src.example.com/debian stable main"
        )
        assert parsed == {
            "type": "deb-src",
            "arch": None,
            "uri": "http://src.example.com/debian",
            "distribution": "stable",
            "components": ["main"],
        }

    def test_deb_arch_suffix(self, am) -> None:
        parsed = am.parse_config_line(
            "deb-powerpc64 http://p64.example.com/debian stable main"
        )
        assert parsed["type"] == "deb"
        assert parsed["arch"] == "powerpc64"
        assert parsed["uri"] == "http://p64.example.com/debian"

    def test_deb_arch_option(self, am) -> None:
        parsed = am.parse_config_line(
            "deb [arch=amd64,i386] http://opt.example.com/debian stable main"
        )
        assert parsed["arch"] == "amd64,i386"

    def test_arch_option_overrides_suffix(self, am) -> None:
        parsed = am.parse_config_line(
            "deb-powerpc64 [arch=amd64] http://p64.example.com/debian stable main"
        )
        assert parsed["arch"] == "amd64"

    def test_other_options_are_ignored(self, am) -> None:
        parsed = am.parse_config_line(
            "deb [trusted=yes,arch=arm64] http://p64.example.com/debian stable main"
        )
        assert parsed["arch"] == "arm64"
        assert parsed["uri"] == "http://p64.example.com/debian"

    def test_flat_repo_has_no_components(self, am) -> None:
        parsed = am.parse_config_line("deb http://flat.example.com/repo bookworm")
        assert parsed["arch"] is None
        assert parsed["uri"] == "http://flat.example.com/repo"
        assert parsed["distribution"] == "bookworm"
        assert parsed["components"] is None

    def test_set_bare_value(self, am) -> None:
        assert am.parse_config_line("set nthreads 4") == {
            "type": "set",
            "key": "nthreads",
            "value": "4",
        }

    def test_set_quoted_values(self, am) -> None:
        assert am.parse_config_line("set x 'a b'")["value"] == "a b"
        assert am.parse_config_line('set x "a b"')["value"] == "a b"

    def test_partially_quoted_value_is_kept_verbatim(self, am) -> None:
        assert am.parse_config_line("set x 'a b' c")["value"] == "'a b' c"

    def test_set_without_value(self, am) -> None:
        assert am.parse_config_line("set limit_rate") == {
            "type": "set",
            "key": "limit_rate",
            "value": None,
        }

    def test_clean_and_skip_clean(self, am) -> None:
        assert am.parse_config_line("clean http://mirror.example.com/debian") == {
            "type": "clean",
            "uri": "http://mirror.example.com/debian",
        }
        assert am.parse_config_line("skip-clean http://mirror.example.com/dsc") == {
            "type": "skip-clean",
            "uri": "http://mirror.example.com/dsc",
        }

    def test_comment_and_blank_lines_are_none(self, am) -> None:
        assert am.parse_config_line("") is None
        assert am.parse_config_line("   \t") is None
        assert am.parse_config_line("# a comment") is None
        assert am.parse_config_line("  # indented comment") is None

    def test_unparseable_line_raises_value_error(self, am) -> None:
        with pytest.raises(ValueError):
            am.parse_config_line("this is not a valid line")


class TestParseConfig:
    """Tests for parse_config (end-to-end over a temp config file)."""

    def test_unknown_line_error_cites_line_number_and_text(self, am, tmp_path) -> None:
        config = tmp_path / "mirror.list"
        config.write_text(
            "set nthreads 4\n"
            "\n"
            "# comment\n"
            "deb http://a.example/ stable main\n"
            "this line is invalid\n",
            encoding="utf-8",
        )
        with pytest.raises(am.AptMirrorError) as excinfo:
            am.parse_config(config)
        message = str(excinfo.value)
        assert "5:" in message
        assert "this line is invalid" in message

    def test_set_without_value_error_includes_line_number(self, am, tmp_path) -> None:
        config = tmp_path / "mirror.list"
        config.write_text("set nthreads 4\nset limit_rate\n", encoding="utf-8")
        with pytest.raises(am.AptMirrorError) as excinfo:
            am.parse_config(config)
        message = str(excinfo.value)
        assert "2:" in message
        assert "limit_rate" in message

    def test_undefined_variable_reference_is_error(self, am, tmp_path) -> None:
        config = tmp_path / "mirror.list"
        config.write_text("set foo $undefined\n", encoding="utf-8")
        with pytest.raises(am.AptMirrorError) as excinfo:
            am.parse_config(config)
        assert str(excinfo.value) == (
            "apt-mirror: config file line 1: set foo $undefined: "
            "reference to undefined variable 'undefined'"
        )

    def test_reference_to_earlier_key_expands(self, am, tmp_path) -> None:
        config = tmp_path / "mirror.list"
        config.write_text(
            "set root /srv/mirror\nset mirror $root/pub\n",
            encoding="utf-8",
        )
        parsed = am.parse_config(config)
        assert parsed.variables["root"] == "/srv/mirror"
        assert parsed.variables["mirror"] == "/srv/mirror/pub"

    def test_self_referencing_set_is_error(self, am, tmp_path) -> None:
        config = tmp_path / "mirror.list"
        config.write_text("set a $a\n", encoding="utf-8")
        with pytest.raises(am.AptMirrorError) as excinfo:
            am.parse_config(config)
        # ``a`` is not in the table while its own value is still expanding,
        # so the self-reference surfaces as an undefined-variable error.
        assert str(excinfo.value) == (
            "apt-mirror: config file line 1: set a $a: "
            "reference to undefined variable 'a'"
        )

    def test_non_converging_expansion_message_is_pinned(self, am) -> None:
        variables = {"a": "$a $a"}
        with pytest.raises(am.AptMirrorError) as excinfo:
            am.get_var(variables, "a")
        assert str(excinfo.value) == (
            "apt-mirror: while expanding 'a': variable expansion does not "
            f"converge (no stable value after {am.MAX_EXPANSION_PASSES} passes)"
        )

    def test_missing_config_file_is_error(self, am, tmp_path) -> None:
        with pytest.raises(am.AptMirrorError):
            am.parse_config(tmp_path / "does-not-exist.list")

    def test_defaults_are_seeded(self, am, tmp_path) -> None:
        config = tmp_path / "mirror.list"
        config.write_text("set nthreads 4\n", encoding="utf-8")
        parsed = am.parse_config(config)
        assert parsed.variables["nthreads"] == "4"
        assert parsed.variables["base_path"] == "/var/spool/apt-mirror"
        assert parsed.variables["limit_rate"] == "100m"
        assert parsed.variables["defaultarch"] == am.detect_defaultarch()
        assert parsed.binaries == []
        assert parsed.sources == []
        assert parsed.clean == set()
        assert parsed.skipclean == set()

    def test_crlf_line_endings_are_stripped(self, am, tmp_path) -> None:
        config = tmp_path / "mirror.list"
        config.write_text(
            _crlf_content(
                "set nthreads 4",
                "set gnuhost gnu.example.org",
                "set note 'quoted value'",
                "",
                "# a comment",
                "deb http://plain.example.com/debian stable main",
                "deb-src http://src.example.com/debian stable main",
                "deb-powerpc64 http://p64.example.com/debian stable main",
                "deb [arch=amd64,i386] http://opt.example.com/debian stable main",
                "deb http://flat.example.com/repo bookworm",
                "clean http://clean.example.com/debian/",
                "skip-clean http://skip.example.com/dsc",
            ),
            encoding="utf-8",
        )
        parsed = am.parse_config(config)
        for value in parsed.variables.values():
            assert "\r" not in value
        for repo in [*parsed.binaries, *parsed.sources]:
            assert "\r" not in repo.uri
            assert "\r" not in repo.distribution
            assert "\r" not in repo.arch
            for component in repo.components or []:
                assert "\r" not in component
        for uri in parsed.clean | parsed.skipclean:
            assert "\r" not in uri
        assert parsed.variables["note"] == "quoted value"
        assert len(parsed.binaries) == 4
        assert len(parsed.sources) == 1
        assert parsed.clean == {"clean.example.com/debian"}
        assert parsed.skipclean == {"skip.example.com/dsc"}


class TestGetVar:
    """Tests for $name reference expansion via get_var."""

    def test_direct_value(self, am) -> None:
        assert am.get_var({"name": "value"}, "name") == "value"

    def test_nested_reference(self, am) -> None:
        variables = {
            "base_path": "/srv/mirror",
            "mirror_path": "$base_path/mirror",
        }
        assert am.get_var(variables, "mirror_path") == "/srv/mirror/mirror"

    def test_two_levels_deep_reference(self, am) -> None:
        variables = {"a": "$b", "b": "$c", "c": "end"}
        assert am.get_var(variables, "a") == "end"

    def test_unknown_key_names_the_key(self, am) -> None:
        with pytest.raises(am.AptMirrorError, match="nokey"):
            am.get_var({}, "nokey")

    def test_non_converging_expansion_is_error(self, am) -> None:
        variables = {"a": "$a $a"}
        with pytest.raises(am.AptMirrorError):
            am.get_var(variables, "a")


def test_detect_defaultarch_returns_nonempty_string(am) -> None:
    assert am.detect_defaultarch()


def test_repo_mirror_list_parses(am) -> None:
    parsed = am.parse_config(REPO_MIRROR_LIST)
    assert len(parsed.binaries) == 6
    assert len(parsed.sources) == 0
    assert parsed.clean == {
        "us.archive.ubuntu.com/ubuntu",
        "download.docker.com/linux/ubuntu",
        "ppa.launchpad.net/gluster/glusterfs-11/ubuntu",
    }
    assert parsed.variables["nthreads"] == "4"
    assert parsed.variables["_tilde"] == "0"
    assert parsed.variables["unlink"] == "1"
    assert parsed.variables["defaultarch"] == "amd64"
