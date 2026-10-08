# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Athena client for the sample event lake (Glue table `events`, created by
terraform/modules/analytics and filled by scripts/generate_sample_events.py).

The model never writes SQL. It picks one of a few fixed templates and passes
scalar arguments, and this module enforces the rest:

  - Every template's WHERE clause requires year and month (and day where it
    applies), so Athena always prunes partitions instead of scanning the
    whole table.
  - Values are bound through Athena ExecutionParameters (`?` placeholders),
    never string-built into the query text.
  - `limit` is clamped to MAX_LIMIT whatever the model asks for.
  - The Athena workgroup caps bytes scanned per query as an independent
    backstop (terraform/modules/foundation/athena.tf).
"""
import json
import logging
import os
import time
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

MAX_LIMIT = 50
MIN_LIMIT = 1
POLL_INTERVAL_SECONDS = 1
MAX_POLL_ATTEMPTS = 30  # ~30s ceiling; the workgroup scan cap bounds cost, this bounds Lambda runtime

VALID_SOURCE_PARTITIONS = frozenset(
    ["crs", "pms", "billing", "loyalty", "housekeeping", "audit", "payment"]
)

_athena_client = None


class AnalyticsQueryError(Exception):
    """Raised for validation failures (bad template/args) and Athena
    execution failures. Message is safe to surface to the model -- never
    includes raw Athena stack traces or account/ARN details.
    """


def _get_athena_client():
    global _athena_client
    if _athena_client is None:
        _athena_client = boto3.client("athena")
    return _athena_client


def _validate_partition_args(year: int, month: int, day: Optional[int], source_partition: str) -> None:
    if source_partition not in VALID_SOURCE_PARTITIONS:
        raise AnalyticsQueryError(
            f"invalid_source_partition: {source_partition!r} not in {sorted(VALID_SOURCE_PARTITIONS)}"
        )
    if not (2024 <= year <= 2030):
        raise AnalyticsQueryError(f"invalid_year: {year} outside supported range 2024-2030")
    if not (1 <= month <= 12):
        raise AnalyticsQueryError(f"invalid_month: {month} must be 1-12")
    if day is not None and not (1 <= day <= 31):
        raise AnalyticsQueryError(f"invalid_day: {day} must be 1-31")


def _clamp_limit(limit: Optional[int]) -> int:
    if limit is None:
        return MAX_LIMIT
    return max(MIN_LIMIT, min(int(limit), MAX_LIMIT))


# ---------------------------------------------------------------------------
# Fixed template set. Every template's SQL is a constant string (no runtime
# concatenation of caller-supplied text anywhere) with `?` placeholders --
# the ONLY thing that varies per call is which validated scalar values get
# bound to those placeholders via Athena's ExecutionParameters.
# ---------------------------------------------------------------------------

_TEMPLATE_EVENT_COUNTS_BY_TYPE = """
SELECT detail_type, count(*) as event_count
FROM events
WHERE year = ? AND month = ? AND day = ? AND source_partition = ?
GROUP BY detail_type
ORDER BY event_count DESC
LIMIT ?
""".strip()

_TEMPLATE_TOP_PROPERTIES_BY_EVENT_TYPE = """
SELECT json_extract_scalar(detail, '$.propertyid') as property_id, count(*) as event_count
FROM events
WHERE year = ? AND month = ? AND day = ? AND source_partition = ? AND detail_type = ?
GROUP BY json_extract_scalar(detail, '$.propertyid')
ORDER BY event_count DESC
LIMIT ?
""".strip()

_TEMPLATE_DAILY_EVENT_TREND = """
SELECT day, count(*) as event_count
FROM events
WHERE year = ? AND month = ? AND source_partition = ? AND detail_type = ?
GROUP BY day
ORDER BY day
LIMIT ?
""".strip()

# Each template declares its own (sql, arg_names, arg_builder) so routing
# stays a lookup, not a branch tree that could grow inconsistent guards.
_TEMPLATES = {
    "event_counts_by_type": {
        "sql": _TEMPLATE_EVENT_COUNTS_BY_TYPE,
        "description": (
            "Counts of each detail_type within one partition-day, e.g. "
            "'what kinds of events happened in pms on 2026-09-01'."
        ),
        "required": ["year", "month", "day", "source_partition"],
    },
    "top_properties_by_event_type": {
        "sql": _TEMPLATE_TOP_PROPERTIES_BY_EVENT_TYPE,
        "description": (
            "Which properties generated the most of a specific event type on "
            "one partition-day, e.g. 'which properties had the most "
            "checkinout.checked_out events on 2026-09-01'."
        ),
        "required": ["year", "month", "day", "source_partition", "detail_type"],
    },
    "daily_event_trend": {
        "sql": _TEMPLATE_DAILY_EVENT_TREND,
        "description": (
            "Day-by-day count of one detail_type across a whole month, e.g. "
            "'trend of housekeeping.room_ready events in September 2026'."
        ),
        "required": ["year", "month", "source_partition", "detail_type"],
    },
}


def list_templates() -> dict:
    """Returns the fixed template catalog -- what the model is told exists,
    nothing more. There is no template that accepts arbitrary SQL text.
    """
    return {
        name: {"description": t["description"], "required_args": t["required"]}
        for name, t in _TEMPLATES.items()
    }


def _build_execution_params(template_name: str, args: dict) -> list:
    """Builds the ExecutionParameters list, in the exact order the
    template's `?` placeholders expect. String values are single-quoted
    per Athena's parameterized-query contract (see Athena docs: "For SQL
    execution parameters to be treated as strings, they must be enclosed
    in single quotes").
    """
    limit = _clamp_limit(args.get("limit"))

    if template_name == "event_counts_by_type":
        return [
            str(args["year"]), str(args["month"]), str(args["day"]),
            f"'{args['source_partition']}'", str(limit),
        ]
    if template_name == "top_properties_by_event_type":
        return [
            str(args["year"]), str(args["month"]), str(args["day"]),
            f"'{args['source_partition']}'", f"'{args['detail_type']}'", str(limit),
        ]
    if template_name == "daily_event_trend":
        return [
            str(args["year"]), str(args["month"]),
            f"'{args['source_partition']}'", f"'{args['detail_type']}'", str(limit),
        ]
    raise AnalyticsQueryError(f"unknown_template: {template_name}")


def _parse_results(rows: list) -> list:
    """Athena's GetQueryResults returns the header row as row 0. Convert to
    a list of dicts keyed by column name, dropping the header.
    """
    if not rows:
        return []
    header = [c.get("VarCharValue", "") for c in rows[0]["Data"]]
    parsed = []
    for row in rows[1:]:
        values = [c.get("VarCharValue") for c in row["Data"]]
        parsed.append(dict(zip(header, values)))
    return parsed


def run_query(template_name: str, args: dict) -> dict:
    """Validates the template name + args, runs the query via Athena's
    ExecutionParameters (never string-built SQL), polls to completion, and
    returns parsed rows. Raises AnalyticsQueryError for any validation
    failure -- callers never reach Athena with an invalid/unbounded query.
    """
    template = _TEMPLATES.get(template_name)
    if template is None:
        raise AnalyticsQueryError(
            f"unknown_template: {template_name!r} not in {sorted(_TEMPLATES)}"
        )

    missing = [a for a in template["required"] if a not in args or args[a] is None]
    if missing:
        raise AnalyticsQueryError(f"missing_required_args: {missing}")

    day = args.get("day")
    _validate_partition_args(
        year=int(args["year"]),
        month=int(args["month"]),
        day=int(day) if day is not None else None,
        source_partition=args["source_partition"],
    )

    execution_params = _build_execution_params(template_name, args)

    database = os.environ["ANALYTICS_GLUE_DATABASE"]
    workgroup = os.environ["ANALYTICS_WORKGROUP_NAME"]

    client = _get_athena_client()
    try:
        start_resp = client.start_query_execution(
            QueryString=template["sql"],
            QueryExecutionContext={"Database": database},
            WorkGroup=workgroup,
            ExecutionParameters=execution_params,
        )
    except Exception as exc:
        # Log the exception message as well as its type. Athena/Glue/IAM
        # error strings are permission or query-plan diagnostics, not
        # secrets, and the type alone is not enough to debug a deployment.
        logger.warning("Athena start_query_execution failed: %s: %s", type(exc).__name__, str(exc))
        raise AnalyticsQueryError("athena_query_failed_to_start") from exc

    query_execution_id = start_resp["QueryExecutionId"]

    for _ in range(MAX_POLL_ATTEMPTS):
        status_resp = client.get_query_execution(QueryExecutionId=query_execution_id)
        state = status_resp["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status_resp["QueryExecution"]["Status"].get(
                "StateChangeReason", "no reason given"
            )
            logger.warning("Athena query %s ended in %s: %s", query_execution_id, state, reason)
            # Surface the reason -- it's typically a scan-cap or SQL-shape
            # error, useful for the model to relay, and never contains
            # secrets (Athena error reasons are query-plan diagnostics).
            raise AnalyticsQueryError(f"athena_query_{state.lower()}: {reason}")
        time.sleep(POLL_INTERVAL_SECONDS)  # nosemgrep: arbitrary-sleep -- intentional fixed poll interval between Athena GetQueryExecution status checks
    else:
        raise AnalyticsQueryError("athena_query_timed_out")

    results_resp = client.get_query_results(QueryExecutionId=query_execution_id)
    rows = _parse_results(results_resp["ResultSet"]["Rows"])

    return {
        "template": template_name,
        "rowCount": len(rows),
        "rows": rows,
    }
