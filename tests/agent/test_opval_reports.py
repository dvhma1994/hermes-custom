"""Tests for agent/opval/reports.py — monthly report period boundaries."""

import calendar
import time
from datetime import datetime, timezone

import pytest

from agent.opval.reports import MonthlyReportGenerator
from agent.opval.store import OpvalStore


@pytest.fixture()
def opval_store(tmp_path):
    store = OpvalStore(tmp_path / "opval.db")
    yield store
    store.close()


def test_monthly_report_period_boundaries_are_utc(tmp_path, monkeypatch):
    """MonthlyReportGenerator must build period_start/period_end as UTC month
    boundaries. ``year``/``month`` come from ``utcfromtimestamp`` (UTC); a
    NAIVE ``datetime(...).timestamp()`` would interpret them as LOCAL time and
    shift the window by the host's UTC offset — selecting the wrong sessions."""
    store = OpvalStore(tmp_path / "opval.db")
    try:
        # Pin "now" to a known instant and derive the expected UTC month window.
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        monkeypatch.setattr(time, "time", lambda: fixed_now)

        dt_utc = datetime.utcfromtimestamp(fixed_now)
        year, month = dt_utc.year, dt_utc.month
        _, end_day = calendar.monthrange(year, month)
        expected_start = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        expected_end = datetime(
            year, month, end_day, 23, 59, 59, tzinfo=timezone.utc
        ).timestamp()

        # Simulate a non-UTC host. A naive datetime's .timestamp() routes
        # through time.mktime, so injecting an offset there shifts the OLD
        # (buggy) naive boundary but leaves an aware-UTC boundary untouched
        # (aware datetimes use (self - EPOCH).total_seconds(), not mktime).
        real_mktime = time.mktime

        def fake_mktime(tt):
            return real_mktime(tt) + 5 * 3600  # +5h local offset

        monkeypatch.setattr(time, "mktime", fake_mktime)

        report = MonthlyReportGenerator(store).generate()

        assert report["period_start"] == expected_start
        assert report["period_end"] == expected_end
    finally:
        store.close()


def test_monthly_report_period_boundaries_explicit_args_passthrough(opval_store):
    """When the caller supplies explicit period_start/period_end, the report
    echoes them verbatim (no UTC/local reinterpretation)."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    end = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()

    report = MonthlyReportGenerator(opval_store).generate(
        period_start=start, period_end=end
    )

    assert report["period_start"] == start
    assert report["period_end"] == end
