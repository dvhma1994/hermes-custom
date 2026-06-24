"""Focused regression test: _format_size rounding-rollover boundary bug.

The old implementation picks the display unit by magnitude BEFORE rounding,
so a value just below the next unit boundary (e.g. 1048575 = 1 MB - 1 byte)
renders as "1024.0 KB" instead of "1.0 MB".  These tests assert the invariant
that the formatted value must never show >= 1024 in a sub-TB unit.
"""
import pytest

from hermes_cli.backup import _format_size


class TestFormatSizeRollover:
    """Values just below a unit boundary must roll into the next unit."""

    @pytest.mark.parametrize("n,expected_unit", [
        (1023, "B"),          # 1023 B — stays bytes
        (1024, "KB"),         # exactly 1 KB
        (1048575, "MB"),      # 1 MB - 1 byte  -> must NOT be "1024.0 KB"
        (1048576, "MB"),      # exactly 1 MB
        (1073741823, "GB"),   # 1 GB - 1 byte  -> must NOT be "1024.0 MB"
        (1073741824, "GB"),   # exactly 1 GB
        (1099511627775, "TB"),  # 1 TB - 1 byte -> must NOT be "1024.0 GB"
    ])
    def test_boundary_uses_correct_unit(self, n, expected_unit):
        result = _format_size(n)
        assert expected_unit in result, (
            f"_format_size({n}) = {result!r}, expected unit {expected_unit!r}"
        )
        # The formatted numeric value must never roll over to >= 1000 in a
        # sub-TB unit (1024.0 KB is the classic symptom).
        if expected_unit != "TB":
            value_str = result.split()[0]
            assert float(value_str) < 1024.0, (
                f"_format_size({n}) = {result!r}, numeric part >= 1024 (rollover)"
            )