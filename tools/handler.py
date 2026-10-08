# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Single Lambda behind every AgentCore Gateway tool target.

Tools routed here (by the tool name AgentCore puts in the Lambda context):
  - get_range_metrics      -> per-period booking/revenue totals (read-only)
  - get_occupancy          -> current occupancy snapshot (read-only)
  - list_properties        -> property list with regions (read-only)
  - query_analytics        -> templated Athena queries over the event lake
  - propose_recommendation -> writes a PENDING recommendation to S3
  - record_recommendation  -> confirms an existing PENDING recommendation.
                              It cannot create a CONFIRMED record directly,
                              which makes human confirmation a server-side
                              gate (see lib/recommendation_client.py).

The three reporting tools read the synthetic portfolio in lib/sample_data.py
through lib/reporting_client.py, which scopes results by the caller's
Cognito groups and region claim.

generate_report is not implemented here. It is a local tool in
agent/agent.py that reads the PDF straight from the Code Interpreter sandbox
and writes it to S3 with the Runtime's own role, so PDF bytes never pass
through the model's context.

This function is also the target of the S3 ObjectCreated trigger on the
reports/ prefix (see terraform/modules/tools/report_delivery.tf). One Lambda
has one handler, so lambda_handler recognizes an S3 event by its shape (a
top-level "Records" key) and sends it to handle_report_created_event, which
emails the requesting analyst a pre-signed download link through SES.

AgentCore Gateway Lambda target contract: the tool name, prefixed with the
target name, arrives in context.client_context.custom
["bedrockAgentCoreToolName"], and the event is the tool's input arguments.
"""
import logging

from lib import analytics_client, recommendation_client, reporting_client
from lib.analytics_client import AnalyticsQueryError
from lib.recommendation_client import RecommendationError
from lib.report_delivery import handle_report_created_event
from lib.region import VALID_REGIONS
from lib.reporting_client import ACCESS_DENIED, ReportingError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TOOL_NAME_DELIMITER = "___"


def _resolve_tool_name(context) -> str:
    """Strip the AgentCore-added `{target_name}___` prefix off the tool
    name delivered in the Lambda context, per AWS's documented contract.
    Falls back to the raw value if the delimiter isn't present (e.g. local
    / direct-invoke testing).
    """
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    raw_name = custom.get("bedrockAgentCoreToolName", "")
    if TOOL_NAME_DELIMITER in raw_name:
        return raw_name.split(TOOL_NAME_DELIMITER, 1)[1]
    return raw_name


def _caller_token(event: dict) -> "str | None":
    """The analyst's Cognito ID token, injected by the agent's
    BeforeToolCallEvent hook (agent/agent.py, _create_caller_token_hook).
    The model does not supply it and cannot override it. It is read here and
    never echoed back in tool output or logs.
    """
    return event.get("_callerToken")


def _tool_get_range_metrics(event: dict) -> dict:
    property_id = event.get("propertyId", "_all")
    start_date = event["startDate"]
    end_date = event["endDate"]
    return reporting_client.get_range_metrics(
        property_id, start_date, end_date, caller_token=_caller_token(event)
    )


def _tool_get_occupancy(event: dict) -> dict:
    start_date = event["startDate"]
    end_date = event["endDate"]
    region = event.get("region")
    caller_token = _caller_token(event)

    result = reporting_client.get_occupancy(start_date, end_date, caller_token=caller_token)
    properties = result.get("properties")
    if region and isinstance(properties, list):
        filtered = [p for p in properties if p.get("region") == region]
        # A region-scoped analyst asking about a different, real region gets
        # an explicit access denial, not an empty list the model might read
        # as "the chain has no properties there".
        if not filtered and region in VALID_REGIONS:
            is_chain_level, _ = reporting_client.caller_scope(caller_token)
            if not is_chain_level:
                raise ReportingError(ACCESS_DENIED)
        # Recompute the summary for the filtered set, so the agent never
        # reads a chain-wide summary next to a regional property list.
        total_rooms = sum(p.get("totalRooms", 0) for p in filtered)
        total_occupied = sum(p.get("occupiedRooms", 0) for p in filtered)
        result = dict(result)
        result["properties"] = filtered
        result["summary"] = {
            "totalProperties": len(filtered),
            "totalRooms": total_rooms,
            "totalOccupied": total_occupied,
            "overallOccupancy": (
                round(total_occupied / total_rooms * 100, 1) if total_rooms else 0.0
            ),
        }
    return result


def _tool_list_properties(event: dict) -> dict:
    return reporting_client.list_properties(caller_token=_caller_token(event))


def _tool_query_analytics(event: dict) -> dict:
    """Templated, parameterized Athena queries over the sample event lake. `event` here is the tool's raw input arguments -- NOT SQL.
    See tools/lib/analytics_client.py for the fixed template set and why
    free-form SQL is structurally impossible, not just discouraged.
    """
    template = event["template"]
    # Everything except `template` and the injected caller token is passed
    # through to analytics_client, which validates every required arg and
    # rejects unknown templates/out-of-range values before any Athena call.
    args = {k: v for k, v in event.items() if k not in ("template", "_callerToken")}

    # Apply the same role/region scoping as the reporting tools. Chain-level
    # users may run every template. Region-scoped users may only run the
    # per-property template, and only see rows for their own properties;
    # the other templates aggregate across the whole chain, so they are
    # denied rather than returning chain-wide totals.
    is_chain_level, allowed_ids = reporting_client.caller_scope(_caller_token(event))
    if is_chain_level:
        return analytics_client.run_query(template, args)

    if template not in _PROPERTY_LEVEL_TEMPLATES:
        raise ReportingError(reporting_client.ACCESS_DENIED)

    # Query at the maximum limit, filter to the caller's properties, then
    # apply the requested limit, so filtering does not silently drop rows
    # the caller is allowed to see.
    requested_limit = analytics_client._clamp_limit(args.get("limit"))
    args["limit"] = analytics_client.MAX_LIMIT
    result = analytics_client.run_query(template, args)
    rows = [r for r in result.get("rows", []) if r.get("property_id") in allowed_ids]
    rows = rows[:requested_limit]
    return {**result, "rowCount": len(rows), "rows": rows}


# Templates whose rows are keyed by property_id and can be filtered to the
# caller's allowed properties.
_PROPERTY_LEVEL_TEMPLATES = frozenset({"top_properties_by_event_type"})


def _tool_propose_recommendation(event: dict) -> dict:
    return recommendation_client.propose_recommendation(
        summary=event["summary"],
        reasoning=event["reasoning"],
        supporting_data=event.get("supportingData"),
        proposer_sub=reporting_client.caller_sub(_caller_token(event)),
    )


def _tool_record_recommendation(event: dict) -> dict:
    return recommendation_client.record_recommendation(
        recommendation_id=event["recommendationId"],
        confirmed=event.get("confirmed"),
        caller_sub=reporting_client.caller_sub(_caller_token(event)),
    )


_ROUTES = {
    "get_range_metrics": _tool_get_range_metrics,
    "get_occupancy": _tool_get_occupancy,
    "list_properties": _tool_list_properties,
    "query_analytics": _tool_query_analytics,
    "propose_recommendation": _tool_propose_recommendation,
    "record_recommendation": _tool_record_recommendation,
}


def _is_s3_event(event: dict) -> bool:
    """S3 ObjectCreated notifications arrive as {"Records": [...]}, with
    no AgentCore Gateway tool-call context at all. Distinguishing on event
    SHAPE (not a Lambda trigger-type flag, since there isn't one available
    at this layer) is safe here because the two trigger sources produce
    structurally disjoint event payloads -- an AgentCore tool call is
    always the raw tool input schema (no "Records" key by contract).
    """
    return isinstance(event, dict) and "Records" in event


def lambda_handler(event: dict, context) -> dict:
    if _is_s3_event(event):
        return handle_report_created_event(event)

    tool_name = _resolve_tool_name(context)
    logger.info("Routing tool call: %s", tool_name)

    handler_fn = _ROUTES.get(tool_name)
    if handler_fn is None:
        logger.error("Unknown tool name requested: %s", tool_name)
        return {"error": f"unknown_tool: {tool_name}"}

    try:
        return handler_fn(event)
    except (ReportingError, AnalyticsQueryError, RecommendationError) as exc:
        logger.error("Tool %s failed: %s", tool_name, str(exc))
        return {"error": str(exc)}
    except KeyError as exc:
        logger.error("Tool %s missing required argument: %s", tool_name, str(exc))
        return {"error": f"missing_argument: {exc}"}
