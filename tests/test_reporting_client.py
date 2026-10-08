# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import datetime

import pytest

from lib import reporting_client, sample_data
from lib.region import VALID_REGIONS
from lib.reporting_client import ACCESS_DENIED, MAX_RANGE_DAYS, ReportingError

from conftest import make_token

MANAGER = make_token(groups=["RevenueManager"])
WEST = make_token(groups=["RegionalManager"], region="West")


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(reporting_client, "_today", lambda: datetime.date(2026, 9, 15))


# --- sample data ----------------------------------------------------------

def test_portfolio_has_50_properties_with_unique_ids_and_valid_regions():
    ids = [p["propertyId"] for p in sample_data.PROPERTIES]
    assert len(ids) == 50
    assert len(set(ids)) == 50
    assert all(p["region"] in VALID_REGIONS for p in sample_data.PROPERTIES)


def test_daily_stats_are_deterministic():
    prop = sample_data.PROPERTIES[0]
    day = datetime.date(2026, 8, 1)
    assert sample_data.daily_stats(prop, day) == sample_data.daily_stats(prop, day)


def test_daily_stats_are_internally_consistent():
    for prop in sample_data.PROPERTIES:
        stats = sample_data.daily_stats(prop, datetime.date(2026, 7, 4))
        assert 0 <= stats["occupiedRooms"] <= stats["totalRooms"]
        assert stats["reservationsCancelled"] <= stats["reservationsCreated"]
        assert stats["revenue"] >= 0


# --- date validation ------------------------------------------------------

def test_range_accepts_exactly_max_days():
    start = datetime.date(2026, 5, 1)
    end = start + datetime.timedelta(days=MAX_RANGE_DAYS - 1)
    reporting_client.validate_date_range(start.isoformat(), end.isoformat())


def test_range_rejects_one_day_over_max():
    start = datetime.date(2026, 5, 1)
    end = start + datetime.timedelta(days=MAX_RANGE_DAYS)
    with pytest.raises(ReportingError, match="date_range_too_long"):
        reporting_client.validate_date_range(start.isoformat(), end.isoformat())


def test_range_rejects_end_before_start():
    with pytest.raises(ReportingError, match="invalid_date_range"):
        reporting_client.validate_date_range("2026-08-10", "2026-08-01")


def test_range_rejects_bad_format():
    with pytest.raises(ReportingError, match="invalid_date_format"):
        reporting_client.validate_date_range("08/01/2026", "2026-08-10")


# --- range metrics --------------------------------------------------------

def test_range_totals_equal_sum_of_daily_breakdown():
    result = reporting_client.get_range_metrics("_all", "2026-08-01", "2026-08-31", caller_token=MANAGER)
    assert len(result["dailyBreakdown"]) == 31
    assert result["totals"]["checkIns"] == sum(d["checkIns"] for d in result["dailyBreakdown"])
    assert result["totals"]["revenue"] == pytest.approx(sum(d["revenue"] for d in result["dailyBreakdown"]))


def test_range_trims_future_dates():
    result = reporting_client.get_range_metrics("_all", "2026-09-01", "2026-09-30", caller_token=MANAGER)
    assert result["dataThrough"] == "2026-09-15"
    assert len(result["dailyBreakdown"]) == 15


def test_range_single_property():
    prop = sample_data.PROPERTIES[3]
    result = reporting_client.get_range_metrics(prop["propertyId"], "2026-08-01", "2026-08-02", caller_token=MANAGER)
    assert result["totals"]["propertyCount"] == 1
    assert result["totals"]["totalRooms"] == prop["totalRooms"]


def test_range_unknown_property():
    with pytest.raises(ReportingError, match="unknown_property"):
        reporting_client.get_range_metrics("not-a-property", "2026-08-01", "2026-08-02", caller_token=MANAGER)


# --- access scoping -------------------------------------------------------

def test_revenue_manager_sees_all_properties():
    assert len(reporting_client.list_properties(caller_token=MANAGER)["properties"]) == 50


def test_regional_manager_sees_only_their_region():
    props = reporting_client.list_properties(caller_token=WEST)["properties"]
    assert props
    assert all(p["region"] == "West" for p in props)
    occupancy = reporting_client.get_occupancy("2026-09-01", "2026-09-15", caller_token=WEST)
    assert all(p["region"] == "West" for p in occupancy["properties"])


def test_regional_manager_denied_other_region_property():
    other = next(p for p in sample_data.PROPERTIES if p["region"] != "West")
    with pytest.raises(ReportingError, match=ACCESS_DENIED):
        reporting_client.get_range_metrics(other["propertyId"], "2026-08-01", "2026-08-02", caller_token=WEST)


@pytest.mark.parametrize(
    "token",
    [
        None,
        "not-a-jwt",
        make_token(groups=[]),
        make_token(groups=["Housekeeping"]),
        make_token(groups=["RegionalManager"]),  # no region claim
    ],
)
def test_callers_without_a_reporting_role_are_denied(token):
    with pytest.raises(ReportingError, match=ACCESS_DENIED):
        reporting_client.list_properties(caller_token=token)
