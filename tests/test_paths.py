"""Unit tests for path/URI normalisation and byte/size formatting (Task 2).

The expected values below are pinned to the *original Perl* behaviour, which
the design (spec §5.4) mandates as "identical observable behaviour".  Two of
the task brief's literal pins turn out to disagree with the original; the
discrepancies are called out inline and in the task-2 report:

- ``sanitise_uri`` keeps the host ``:port`` (original strips only the
  ``userinfo@``); the brief sketched ``example.com/p`` where the original
  yields ``example.com:8443/p``.
- ``remove_double_slashes("a/../../b")`` yields ``a/b`` in the original (it
  collapses a ``/seg/../`` pair in a single pass); the brief sketched ``b``.
"""

import pytest


class TestSanitiseUri:
    """Tests for sanitise_uri (URL -> mirror-tree directory key)."""

    def test_scheme_stripped(self, am) -> None:
        assert am.sanitise_uri("http://h/debian/") == "h/debian"

    def test_userinfo_stripped_port_preserved(self, am) -> None:
        # Original strips only the ``u:p@`` userinfo prefix; the host:port
        # survives.  (Brief sketched "example.com/p"; see report.)
        result = am.sanitise_uri("https://u:p@example.com:8443/p/")
        assert result == "example.com:8443/p"
        assert "@" not in result
        host = result.split("/")[0]
        assert "@" not in host

    def test_trailing_slash_stripped(self, am) -> None:
        assert am.sanitise_uri("https://skip.example.com/dsc/") == (
            "skip.example.com/dsc"
        )

    def test_no_scheme_unchanged(self, am) -> None:
        assert am.sanitise_uri("mirror.example.com/debian") == (
            "mirror.example.com/debian"
        )

    def test_tilde_encoding(self, am) -> None:
        assert am.sanitise_uri("http://h/a~b", tilde=True) == "h/a%7Eb"

    def test_plus_encoding(self, am) -> None:
        assert am.sanitise_uri("http://h/a+b", plus=True) == "h/a%2Bb"

    def test_tilde_plus_together(self, am) -> None:
        assert am.sanitise_uri("http://h/a~b+c", tilde=True, plus=True) == (
            "h/a%7Eb%2Bc"
        )

    def test_userinfo_with_trailing_path_and_port(self, am) -> None:
        assert am.sanitise_uri("http://user@host:1234/x/y/") == "host:1234/x/y"


class TestRemoveDoubleSlashes:
    """Tests for remove_double_slashes (mirror-tree path normalisation)."""

    def test_collapse_double_slash(self, am) -> None:
        assert am.remove_double_slashes("h/a//b") == "h/a/b"

    def test_collapse_dotdot(self, am) -> None:
        assert am.remove_double_slashes("h/a/../b") == "h/b"

    def test_collapse_dot(self, am) -> None:
        assert am.remove_double_slashes("h/a/./b") == "h/a/b"

    def test_repeated_dotdot_preserves_first_segment(self, am) -> None:
        # Original collapses the leading ``/../..`` pair in one pass, leaving
        # the first segment.  (Brief sketched "b"; see report.)
        assert am.remove_double_slashes("a/../../b") == "a/b"

    def test_leading_double_slash_safe(self, am) -> None:
        assert am.remove_double_slashes("//host/path") == "/host/path"

    def test_scheme_is_not_eaten(self, am) -> None:
        assert am.remove_double_slashes("http://h") == "http://h"

    def test_sequential_normalisation(self, am) -> None:
        assert am.remove_double_slashes("a/b/../../c/../d//./e") == "a/../d/e"

    def test_tilde_and_plus_encoding(self, am) -> None:
        assert am.remove_double_slashes("a//b~c+d", tilde=True, plus=True) == (
            "a/b%7Ec%2Bd"
        )


class TestRoundNumber:
    """Tests for round_number (one-decimal, half-up rounding, string out)."""

    def test_whole_number_gains_trailing_zero(self, am) -> None:
        assert am.round_number(1.0) == "1.0"

    def test_rounds_to_integer(self, am) -> None:
        assert am.round_number(1.96) == "2.0"

    def test_rounds_half_up(self, am) -> None:
        assert am.round_number(1.25) == "1.3"

    def test_rounds_small_half_up(self, am) -> None:
        assert am.round_number(0.05) == "0.1"

    def test_negative_preserves_sign(self, am) -> None:
        assert am.round_number(-1.25) == "-1.3"

    def test_sub_half_rounds_down(self, am) -> None:
        assert am.round_number(0.04) == "0.0"


class TestFormatBytes:
    """Tests for format_bytes (byte count -> human-readable size)."""

    def test_zero(self, am) -> None:
        assert am.format_bytes(0) == "0 bytes"

    def test_under_kib_is_bytes(self, am) -> None:
        assert am.format_bytes(1023) == "1023 bytes"

    def test_kib_boundary(self, am) -> None:
        assert am.format_bytes(1024) == "1.0 KiB"

    def test_kib_fraction(self, am) -> None:
        assert am.format_bytes(1536) == "1.5 KiB"

    def test_mib_boundary(self, am) -> None:
        assert am.format_bytes(1024 * 1024) == "1.0 MiB"

    def test_gib_value(self, am) -> None:
        assert am.format_bytes(5 * 1024**3) == "5.0 GiB"

    def test_just_under_gib_stays_mib(self, am) -> None:
        assert am.format_bytes(1024 * 1024 * 1024 - 1) == "1024.0 MiB"


class TestParseRate:
    """Tests for parse_rate (limit_rate -> bytes per second)."""

    def test_megabyte_suffix_lowercase(self, am) -> None:
        assert am.parse_rate("100m") == 100_000_000.0

    def test_kilobyte_suffix(self, am) -> None:
        assert am.parse_rate("1k") == 1000.0

    def test_bare_number(self, am) -> None:
        assert am.parse_rate("1048576") == 1048576.0

    def test_uppercase_gigabyte_suffix(self, am) -> None:
        assert am.parse_rate("1G") == 1e9

    def test_mixed_case_suffixes(self, am) -> None:
        assert am.parse_rate("2K") == 2000.0
        assert am.parse_rate("3M") == 3_000_000.0
        assert am.parse_rate("0.5g") == 500_000_000.0

    @pytest.mark.parametrize("value", ["", "fast", "-5", "10x"])
    def test_invalid_rate_raises_and_names_literal(self, am, value: str) -> None:
        with pytest.raises(am.AptMirrorError) as excinfo:
            am.parse_rate(value)
        message = str(excinfo.value)
        assert "invalid limit_rate" in message
        assert value in message


class TestSwapHonoursVariables:
    """The parse_config clean/skip-clean path must use the real sanitise_uri.

    These exercise the Task-2 swap: ``_tilde`` / ``_plus`` set in the config
    must drive the encoding of the sanitised clean/skip-clean keys.
    """

    def test_tilde_plus_honoured_for_clean(self, am, tmp_path) -> None:
        config = tmp_path / "mirror.list"
        config.write_text(
            "set _tilde 1\nset _plus 1\nclean http://mirror.example.com/deb~ian+\n",
            encoding="utf-8",
        )
        parsed = am.parse_config(config)
        assert parsed.clean == {"mirror.example.com/deb%7Eian%2B"}

    def test_no_encoding_when_disabled(self, am, tmp_path) -> None:
        config = tmp_path / "mirror.list"
        config.write_text(
            "clean http://mirror.example.com/deb~ian\n",
            encoding="utf-8",
        )
        parsed = am.parse_config(config)
        assert parsed.clean == {"mirror.example.com/deb~ian"}
