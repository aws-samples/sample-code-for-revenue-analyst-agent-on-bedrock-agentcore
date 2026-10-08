# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import pytest

import handler
from lib.reporting_client import ACCESS_DENIED, ReportingError

from conftest import make_token

MANAGER = make_token(groups=["RevenueManager"])


class FakeClientContext:
    def __init__(self, tool_name):
        self.custom = {"bedrockAgentCoreToolName": tool_name}


class FakeContext:
    def __init__(self, tool_name):
        self.client_context = FakeClientContext(tool_name)


def test_resolve_tool_name_strips_target_prefix():
    ctx = FakeContext("myTarget___get_occupancy")
    assert handler._resolve_tool_name(ctx) == "get_occupancy"


def test_resolve_tool_name_passes_through_without_prefix():
    ctx = FakeContext("get_occupancy")
    assert handler._resolve_tool_name(ctx) == "get_occupancy"


def test_unknown_tool_returns_error(monkeypatch):
    ctx = FakeContext("target___not_a_real_tool")
    result = handler.lambda_handler({}, ctx)
    assert "error" in result
    assert "unknown_tool" in result["error"]


def test_get_occupancy_route_returns_regions_and_summary():
    ctx = FakeContext("target___get_occupancy")
    event = {"startDate": "2026-08-01", "endDate": "2026-08-31", "_callerToken": MANAGER}
    result = handler.lambda_handler(event, ctx)

    assert "error" not in result
    assert len(result["properties"]) == 50
    assert all(p["region"] for p in result["properties"])
    assert result["summary"]["totalProperties"] == 50


def test_get_occupancy_route_filters_by_region_and_recomputes_summary():
    ctx = FakeContext("target___get_occupancy")
    event = {
        "startDate": "2026-08-01",
        "endDate": "2026-08-31",
        "region": "West",
        "_callerToken": MANAGER,
    }
    result = handler.lambda_handler(event, ctx)

    assert result["properties"]
    assert all(p["region"] == "West" for p in result["properties"])
    assert result["summary"]["totalProperties"] == len(result["properties"])
    assert result["summary"]["totalRooms"] == sum(p["totalRooms"] for p in result["properties"])


def _occupancy_event(region, token):
    return {"startDate": "2026-08-01", "endDate": "2026-08-31", "region": region, "_callerToken": token}


def test_get_occupancy_regional_manager_denied_for_other_real_region():
    west = make_token(groups=["RegionalManager"], region="West")
    result = handler.lambda_handler(_occupancy_event("Northeast", west), FakeContext("target___get_occupancy"))
    assert result == {"error": "reporting_access_denied"}


def test_get_occupancy_regional_manager_own_region_allowed():
    west = make_token(groups=["RegionalManager"], region="West")
    result = handler.lambda_handler(_occupancy_event("West", west), FakeContext("target___get_occupancy"))
    assert result["properties"]
    assert all(p["region"] == "West" for p in result["properties"])


def test_get_occupancy_regional_manager_unknown_region_still_empty():
    west = make_token(groups=["RegionalManager"], region="West")
    result = handler.lambda_handler(_occupancy_event("Atlantis", west), FakeContext("target___get_occupancy"))
    assert result["properties"] == []


def test_get_occupancy_route_unknown_region_returns_empty_not_error():
    ctx = FakeContext("target___get_occupancy")
    event = {
        "startDate": "2026-08-01",
        "endDate": "2026-08-31",
        "region": "Atlantis",
        "_callerToken": MANAGER,
    }
    result = handler.lambda_handler(event, ctx)

    assert result["properties"] == []
    assert result["summary"]["totalProperties"] == 0
    assert result["summary"]["overallOccupancy"] == 0.0


def test_get_range_metrics_route():
    ctx = FakeContext("target___get_range_metrics")
    event = {
        "propertyId": "_all",
        "startDate": "2026-08-01",
        "endDate": "2026-08-07",
        "_callerToken": MANAGER,
    }
    result = handler.lambda_handler(event, ctx)

    assert "error" not in result
    assert len(result["dailyBreakdown"]) == 7
    assert result["totals"]["propertyCount"] == 50


def test_list_properties_route():
    ctx = FakeContext("target___list_properties")
    result = handler.lambda_handler({"_callerToken": MANAGER}, ctx)

    assert len(result["properties"]) == 50
    assert all(p["region"] for p in result["properties"])


def test_reporting_route_without_token_is_denied():
    ctx = FakeContext("target___list_properties")
    result = handler.lambda_handler({}, ctx)
    assert result == {"error": ACCESS_DENIED}


def test_missing_required_argument_returns_error():
    ctx = FakeContext("target___get_range_metrics")
    result = handler.lambda_handler({"propertyId": "_all"}, ctx)  # missing dates
    assert "error" in result
    assert "missing_argument" in result["error"]


def test_reporting_error_is_surfaced_safely(monkeypatch):
    def fake_get_occupancy(start_date, end_date, caller_token=None):
        raise ReportingError("invalid_date_range: endDate is before startDate")

    monkeypatch.setattr("lib.reporting_client.get_occupancy", fake_get_occupancy)

    ctx = FakeContext("target___get_occupancy")
    event = {"startDate": "2026-08-27", "endDate": "2026-08-01"}
    result = handler.lambda_handler(event, ctx)

    assert result == {"error": "invalid_date_range: endDate is before startDate"}


# ---------------------------------------------------------------------------
# query_analytics route
# ---------------------------------------------------------------------------

def test_query_analytics_route_passes_through_to_analytics_client(monkeypatch):
    captured = {}

    def fake_run_query(template, args):
        captured["template"] = template
        captured["args"] = args
        return {"template": template, "rowCount": 0, "rows": []}

    monkeypatch.setattr("lib.analytics_client.run_query", fake_run_query)

    ctx = FakeContext("target___query_analytics")
    event = {
        "template": "event_counts_by_type",
        "year": 2026,
        "month": 9,
        "day": 1,
        "source_partition": "pms",
        "_callerToken": MANAGER,
    }
    result = handler.lambda_handler(event, ctx)

    assert "error" not in result
    assert captured["template"] == "event_counts_by_type"
    # Neither `template` nor the caller token is forwarded as a query arg
    assert "template" not in captured["args"]
    assert "_callerToken" not in captured["args"]
    assert captured["args"] == {"year": 2026, "month": 9, "day": 1, "source_partition": "pms"}


def test_query_analytics_route_surfaces_analytics_query_error(monkeypatch):
    from lib.analytics_client import AnalyticsQueryError

    def fake_run_query(template, args):
        raise AnalyticsQueryError("invalid_source_partition: 'nope' not allowed")

    monkeypatch.setattr("lib.analytics_client.run_query", fake_run_query)

    ctx = FakeContext("target___query_analytics")
    event = {"template": "event_counts_by_type", "year": 2026, "month": 9, "day": 1, "source_partition": "nope", "_callerToken": MANAGER}
    result = handler.lambda_handler(event, ctx)

    assert "error" in result
    assert "invalid_source_partition" in result["error"]


def test_query_analytics_route_missing_template_arg_returns_error():
    ctx = FakeContext("target___query_analytics")
    result = handler.lambda_handler({"year": 2026, "month": 9}, ctx)  # missing `template`
    assert "error" in result
    assert "missing_argument" in result["error"]


# ---------------------------------------------------------------------------
# S3 ObjectCreated event dispatch -- must NOT be routed as
# an AgentCore Gateway tool call
# ---------------------------------------------------------------------------

def test_lambda_handler_dispatches_s3_events_to_report_delivery(monkeypatch):
    captured = {}

    def fake_handle_report_created_event(event):
        captured["event"] = event
        return {"processed": 1}

    monkeypatch.setattr("handler.handle_report_created_event", fake_handle_report_created_event)

    s3_event = {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": "reports/x.pdf"}}}]}
    # context is irrelevant for the S3 path -- pass None to confirm it's
    # never touched before the S3-shape check short-circuits
    result = handler.lambda_handler(s3_event, None)

    assert result == {"processed": 1}
    assert captured["event"] == s3_event


def test_is_s3_event_detects_records_key():
    assert handler._is_s3_event({"Records": [{}]}) is True


def test_is_s3_event_false_for_normal_tool_call_event():
    assert handler._is_s3_event({"startDate": "2026-08-27", "endDate": "2026-09-03"}) is False


# ---------------------------------------------------------------------------
# propose_recommendation / record_recommendation routes 
# ---------------------------------------------------------------------------

def test_propose_recommendation_route_passes_through(monkeypatch):
    captured = {}

    def fake_propose(summary, reasoning, supporting_data, proposer_sub):
        captured.update(summary=summary, reasoning=reasoning, supporting_data=supporting_data, proposer_sub=proposer_sub)
        return {"recommendationId": "abc123", "status": "PENDING"}

    monkeypatch.setattr("lib.recommendation_client.propose_recommendation", fake_propose)

    ctx = FakeContext("target___propose_recommendation")
    event = {"summary": "Raise rate", "reasoning": "Demand up", "supportingData": {"delta": 5}, "_callerToken": MANAGER}
    result = handler.lambda_handler(event, ctx)

    assert "error" not in result
    assert result["status"] == "PENDING"
    assert captured == {
        "summary": "Raise rate",
        "reasoning": "Demand up",
        "supporting_data": {"delta": 5},
        "proposer_sub": "test-sub",
    }


def test_record_recommendation_route_passes_through(monkeypatch):
    captured = {}

    def fake_record(recommendation_id, confirmed, caller_sub):
        captured.update(recommendation_id=recommendation_id, confirmed=confirmed, caller_sub=caller_sub)
        return {"recommendationId": recommendation_id, "status": "CONFIRMED", "alreadyConfirmed": False}

    monkeypatch.setattr("lib.recommendation_client.record_recommendation", fake_record)

    ctx = FakeContext("target___record_recommendation")
    event = {"recommendationId": "abc123", "confirmed": True, "_callerToken": MANAGER}
    result = handler.lambda_handler(event, ctx)

    assert "error" not in result
    assert result["status"] == "CONFIRMED"
    assert captured == {"recommendation_id": "abc123", "confirmed": True, "caller_sub": "test-sub"}


def test_record_recommendation_route_surfaces_recommendation_error(monkeypatch):
    from lib.recommendation_client import RecommendationError

    def fake_record(recommendation_id, confirmed, caller_sub):
        raise RecommendationError(f"recommendation_not_found: {recommendation_id}")

    monkeypatch.setattr("lib.recommendation_client.record_recommendation", fake_record)

    ctx = FakeContext("target___record_recommendation")
    event = {"recommendationId": "does-not-exist", "confirmed": True}
    result = handler.lambda_handler(event, ctx)

    assert "error" in result
    assert "recommendation_not_found" in result["error"]


def test_record_recommendation_route_missing_recommendation_id_returns_error():
    ctx = FakeContext("target___record_recommendation")
    result = handler.lambda_handler({"confirmed": True}, ctx)  # missing recommendationId
    assert "error" in result
    assert "missing_argument" in result["error"]


# ---------------------------------------------------------------------------
# Caller token passthrough at the handler layer
# ---------------------------------------------------------------------------

def test_caller_token_extracted_from_event():
    assert handler._caller_token({"_callerToken": "abc123", "startDate": "x"}) == "abc123"


def test_caller_token_none_when_absent():
    assert handler._caller_token({"startDate": "x"}) is None


@pytest.mark.parametrize(
    "tool_name,event,fn_name",
    [
        ("get_occupancy", {"startDate": "2026-08-01", "endDate": "2026-08-02"}, "get_occupancy"),
        ("list_properties", {}, "list_properties"),
        (
            "get_range_metrics",
            {"propertyId": "_all", "startDate": "2026-08-01", "endDate": "2026-08-02"},
            "get_range_metrics",
        ),
    ],
)
def test_reporting_routes_forward_caller_token(monkeypatch, tool_name, event, fn_name):
    captured = {}

    def fake(*args, caller_token=None, **kwargs):
        captured["caller_token"] = caller_token
        return {"properties": []}

    monkeypatch.setattr(f"lib.reporting_client.{fn_name}", fake)

    ctx = FakeContext(f"target___{tool_name}")
    handler.lambda_handler(dict(event, _callerToken="real-analyst-token"), ctx)

    assert captured["caller_token"] == "real-analyst-token"


# ---------------------------------------------------------------------------
# query_analytics role/region scoping (threat model F1)
# ---------------------------------------------------------------------------

def _regional_token():
    from lib import sample_data
    region = sample_data.PROPERTIES[0]["region"]
    return region, make_token(groups=["RegionalManager"], region=region)


def test_query_analytics_denied_without_token(monkeypatch):
    monkeypatch.setattr("lib.analytics_client.run_query", lambda t, a: pytest.fail("must not query"))
    ctx = FakeContext("target___query_analytics")
    event = {"template": "event_counts_by_type", "year": 2026, "month": 9, "day": 1, "source_partition": "pms"}
    assert handler.lambda_handler(event, ctx) == {"error": ACCESS_DENIED}


@pytest.mark.parametrize("template", ["event_counts_by_type", "daily_event_trend"])
def test_query_analytics_chain_wide_templates_denied_for_regional_manager(monkeypatch, template):
    monkeypatch.setattr("lib.analytics_client.run_query", lambda t, a: pytest.fail("must not query"))
    _, token = _regional_token()
    ctx = FakeContext("target___query_analytics")
    event = {"template": template, "year": 2026, "month": 9, "day": 1, "source_partition": "pms",
             "detail_type": "checkinout.checked_in", "_callerToken": token}
    assert handler.lambda_handler(event, ctx) == {"error": ACCESS_DENIED}


def test_query_analytics_top_properties_filtered_to_callers_region(monkeypatch):
    from lib import sample_data
    region, token = _regional_token()
    own = [p["propertyId"] for p in sample_data.PROPERTIES if p["region"] == region]
    other = [p["propertyId"] for p in sample_data.PROPERTIES if p["region"] != region]
    captured = {}

    def fake_run_query(template, args):
        captured["args"] = args
        rows = [{"property_id": pid, "event_count": "9"} for pid in other[:3] + own[:2]]
        return {"template": template, "rowCount": len(rows), "rows": rows}

    monkeypatch.setattr("lib.analytics_client.run_query", fake_run_query)
    ctx = FakeContext("target___query_analytics")
    event = {"template": "top_properties_by_event_type", "year": 2026, "month": 9, "day": 1,
             "source_partition": "pms", "detail_type": "checkinout.checked_in", "limit": 1,
             "_callerToken": token}
    result = handler.lambda_handler(event, ctx)

    assert "error" not in result
    # Queried at the max limit so filtering can't hide allowed rows...
    assert captured["args"]["limit"] == 50
    # ...then only the caller's own properties, truncated to the requested limit.
    assert [r["property_id"] for r in result["rows"]] == own[:1]
    assert result["rowCount"] == 1
