# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Security control: unparameterized or unbounded queries must be impossible
to build, not just discouraged by convention, so this is covered by tests.
"""
import pytest

from lib import analytics_client
from lib.analytics_client import (
    AnalyticsQueryError,
    MAX_LIMIT,
    _build_execution_params,
    _clamp_limit,
    _validate_partition_args,
    run_query,
)


# ---------------------------------------------------------------------------
# Template catalog is fixed -- no way to supply raw SQL
# ---------------------------------------------------------------------------

def test_list_templates_returns_only_the_fixed_set():
    templates = analytics_client.list_templates()
    assert set(templates) == {
        "event_counts_by_type",
        "top_properties_by_event_type",
        "daily_event_trend",
    }


def test_run_query_rejects_unknown_template_name(monkeypatch):
    with pytest.raises(AnalyticsQueryError, match="unknown_template"):
        run_query("DROP TABLE events; --", {"year": 2026, "month": 9, "day": 1, "source_partition": "pms"})


def test_run_query_rejects_template_name_containing_sql(monkeypatch):
    """A model or prompt-injected input might try to pass a SQL string as
    the 'template' argument itself. Must be rejected the same as any other
    unknown template name -- there is no code path that treats `template`
    as anything but a dict lookup key.
    """
    with pytest.raises(AnalyticsQueryError, match="unknown_template"):
        run_query(
            "event_counts_by_type; SELECT * FROM events",
            {"year": 2026, "month": 9, "day": 1, "source_partition": "pms"},
        )


# ---------------------------------------------------------------------------
# Every template requires year/month/day (or year/month) partition
# predicates -- no template can scan unpartitioned
# ---------------------------------------------------------------------------

def test_run_query_rejects_missing_required_args():
    with pytest.raises(AnalyticsQueryError, match="missing_required_args"):
        run_query("event_counts_by_type", {"year": 2026, "month": 9})  # missing day, source_partition


def test_daily_event_trend_does_not_require_day_but_requires_month_and_year():
    with pytest.raises(AnalyticsQueryError, match="missing_required_args"):
        run_query("daily_event_trend", {"year": 2026, "source_partition": "pms", "detail_type": "x"})


# ---------------------------------------------------------------------------
# Partition value validation (bounds checking, enum checking)
# ---------------------------------------------------------------------------

def test_validate_partition_args_accepts_valid_values():
    _validate_partition_args(year=2026, month=9, day=1, source_partition="pms")  # no raise


def test_validate_partition_args_rejects_invalid_source_partition():
    with pytest.raises(AnalyticsQueryError, match="invalid_source_partition"):
        _validate_partition_args(year=2026, month=9, day=1, source_partition="'; DROP TABLE events; --")


def test_validate_partition_args_rejects_out_of_range_year():
    with pytest.raises(AnalyticsQueryError, match="invalid_year"):
        _validate_partition_args(year=1999, month=9, day=1, source_partition="pms")


def test_validate_partition_args_rejects_out_of_range_month():
    with pytest.raises(AnalyticsQueryError, match="invalid_month"):
        _validate_partition_args(year=2026, month=13, day=1, source_partition="pms")


def test_validate_partition_args_rejects_out_of_range_day():
    with pytest.raises(AnalyticsQueryError, match="invalid_day"):
        _validate_partition_args(year=2026, month=9, day=32, source_partition="pms")


def test_validate_partition_args_allows_missing_day_for_month_level_templates():
    _validate_partition_args(year=2026, month=9, day=None, source_partition="pms")  # no raise


# ---------------------------------------------------------------------------
# Limit clamping -- the model can never request more than MAX_LIMIT rows,
# regardless of what it asks for
# ---------------------------------------------------------------------------

def test_clamp_limit_defaults_to_max_when_not_specified():
    assert _clamp_limit(None) == MAX_LIMIT


def test_clamp_limit_caps_an_oversized_request():
    assert _clamp_limit(999999) == MAX_LIMIT


def test_clamp_limit_floors_a_zero_or_negative_request():
    assert _clamp_limit(0) >= 1
    assert _clamp_limit(-50) >= 1


def test_clamp_limit_passes_through_a_reasonable_value():
    assert _clamp_limit(10) == 10


# ---------------------------------------------------------------------------
# ExecutionParameters binding -- values are bound positionally via Athena's
# native parameterization, never string-concatenated into SQL text
# ---------------------------------------------------------------------------

def test_build_execution_params_binds_values_not_sql_text():
    params = _build_execution_params(
        "event_counts_by_type",
        {"year": 2026, "month": 9, "day": 1, "source_partition": "pms", "limit": 5},
    )
    assert params == ["2026", "9", "1", "'pms'", "5"]


def test_build_execution_params_quotes_string_values_for_athena():
    """Athena's parameterized-query contract requires string execution
    parameters to be single-quoted (see Athena docs). A source_partition
    value containing a single quote must still be treated as an opaque
    string value passed to Athena's own binding -- it is never spliced
    into the SQL text, so it cannot break out of the parameter position.
    """
    params = _build_execution_params(
        "event_counts_by_type",
        {"year": 2026, "month": 9, "day": 1, "source_partition": "pms' OR '1'='1", "limit": 5},
    )
    # The malicious-looking string is still just ONE bound parameter value,
    # not concatenated SQL -- Athena will treat it as a literal string to
    # compare source_partition against, which the enum check upstream
    # would have already rejected in run_query() before reaching here.
    assert params[3] == "'pms' OR '1'='1'"
    assert len(params) == 5  # still exactly 5 positional params, no injected extra clause


def test_run_query_rejects_injection_attempt_via_source_partition_before_athena_call(monkeypatch):
    """End-to-end: even if _build_execution_params would happily quote a
    malicious string, run_query's enum check on source_partition rejects
    it before any Athena call is made.
    """

    def fail_if_called(*args, **kwargs):
        raise AssertionError("Athena should never be called for an invalid source_partition")

    monkeypatch.setattr(analytics_client, "_get_athena_client", fail_if_called)

    with pytest.raises(AnalyticsQueryError, match="invalid_source_partition"):
        run_query(
            "event_counts_by_type",
            {"year": 2026, "month": 9, "day": 1, "source_partition": "pms'; DROP TABLE events; --"},
        )


# ---------------------------------------------------------------------------
# Result parsing (header row handling)
# ---------------------------------------------------------------------------

def test_parse_results_drops_header_row_and_maps_columns():
    rows = [
        {"Data": [{"VarCharValue": "detail_type"}, {"VarCharValue": "event_count"}]},
        {"Data": [{"VarCharValue": "checkinout.checked_out"}, {"VarCharValue": "351"}]},
    ]
    parsed = analytics_client._parse_results(rows)
    assert parsed == [{"detail_type": "checkinout.checked_out", "event_count": "351"}]


def test_parse_results_handles_empty_rows():
    assert analytics_client._parse_results([]) == []


# ---------------------------------------------------------------------------
# Full run_query happy path, Athena client mocked
# ---------------------------------------------------------------------------

def test_run_query_happy_path_polls_and_returns_parsed_rows(monkeypatch):
    class FakeAthenaClient:
        def __init__(self):
            self.start_call = None

        def start_query_execution(self, **kwargs):
            self.start_call = kwargs
            return {"QueryExecutionId": "fake-qid"}

        def get_query_execution(self, QueryExecutionId):
            assert QueryExecutionId == "fake-qid"
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        def get_query_results(self, QueryExecutionId):
            return {
                "ResultSet": {
                    "Rows": [
                        {"Data": [{"VarCharValue": "detail_type"}, {"VarCharValue": "event_count"}]},
                        {"Data": [{"VarCharValue": "checkinout.checked_out"}, {"VarCharValue": "42"}]},
                    ]
                }
            }

    fake_client = FakeAthenaClient()
    monkeypatch.setattr(analytics_client, "_get_athena_client", lambda: fake_client)
    monkeypatch.setenv("ANALYTICS_GLUE_DATABASE", "revagent_dev_analytics")
    monkeypatch.setenv("ANALYTICS_WORKGROUP_NAME", "revagent-dev-workgroup")

    result = run_query(
        "event_counts_by_type",
        {"year": 2026, "month": 9, "day": 1, "source_partition": "pms"},
    )

    assert result["template"] == "event_counts_by_type"
    assert result["rowCount"] == 1
    assert result["rows"] == [{"detail_type": "checkinout.checked_out", "event_count": "42"}]
    # Confirm no free-form SQL text was ever built with concatenated values
    assert "?" in fake_client.start_call["QueryString"]
    assert fake_client.start_call["WorkGroup"] == "revagent-dev-workgroup"
    assert fake_client.start_call["ExecutionParameters"] == ["2026", "9", "1", "'pms'", "50"]


def test_run_query_raises_on_athena_query_failure(monkeypatch):
    class FakeAthenaClient:
        def start_query_execution(self, **kwargs):
            return {"QueryExecutionId": "fake-qid"}

        def get_query_execution(self, QueryExecutionId):
            return {
                "QueryExecution": {
                    "Status": {"State": "FAILED", "StateChangeReason": "SYNTAX_ERROR: bad query"}
                }
            }

    monkeypatch.setattr(analytics_client, "_get_athena_client", lambda: FakeAthenaClient())
    monkeypatch.setenv("ANALYTICS_GLUE_DATABASE", "revagent_dev_analytics")
    monkeypatch.setenv("ANALYTICS_WORKGROUP_NAME", "revagent-dev-workgroup")

    with pytest.raises(AnalyticsQueryError, match="athena_query_failed"):
        run_query(
            "event_counts_by_type",
            {"year": 2026, "month": 9, "day": 1, "source_partition": "pms"},
        )
