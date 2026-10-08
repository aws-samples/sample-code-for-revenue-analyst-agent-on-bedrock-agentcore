# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Reporting tools: range metrics, occupancy snapshot, and property list.

Figures come from the deterministic sample portfolio in sample_data.py. To
connect a real property-management or reporting system, replace the three
public functions below with calls to that system; the handler, the agent,
and the Gateway tool schemas do not need to change.

Access scoping
--------------
Every call receives `caller_token`, the analyst's Cognito ID token. The agent
injects it into the tool call in a hook the model cannot influence (see
agent/agent.py, _create_caller_token_hook). The AgentCore Runtime's JWT
authorizer validates the token before the agent runs, but this module does
not rely on that: every claim used for scoping comes from
lib/token_verifier.py, which verifies the signature against the user pool's
JWKS and checks issuer, audience, token_use, and expiry. A token that fails
any check is treated as no token.

  - RevenueManager group: chain-level, sees every property.
  - RegionalManager group: sees only properties in its `custom:region`.
  - Anyone else, or no token: access denied.

A denied call raises ReportingError(ACCESS_DENIED). The agent recognizes that
exact error and answers with a fixed message instead of letting the model
narrate it.
"""
import datetime
import logging

from . import sample_data
from .token_verifier import verified_claims

logger = logging.getLogger(__name__)

MAX_RANGE_DAYS = 92
ACCESS_DENIED = "reporting_access_denied"

CHAIN_LEVEL_GROUPS = frozenset({"RevenueManager"})
REGION_SCOPED_GROUPS = frozenset({"RegionalManager"})


class ReportingError(Exception):
    """Raised for validation failures and access denials. The message is
    safe to return to the model: it never contains tokens or stack traces.
    """


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def _decode_claims(token: "str | None") -> dict:
    """Claims from a verified token, or {} if the token is missing or fails
    verification (see token_verifier.verified_claims)."""
    return verified_claims(token)


def _allowed_properties(caller_token: "str | None") -> list:
    """Return the properties this caller may see, or raise ACCESS_DENIED."""
    claims = _decode_claims(caller_token)
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    groups = set(groups)

    if groups & CHAIN_LEVEL_GROUPS:
        return list(sample_data.PROPERTIES)

    if groups & REGION_SCOPED_GROUPS:
        region = claims.get("custom:region")
        if region:
            return [p for p in sample_data.PROPERTIES if p["region"] == region]

    raise ReportingError(ACCESS_DENIED)


def caller_scope(caller_token: "str | None") -> "tuple[bool, frozenset]":
    """Return (is_chain_level, allowed_property_ids) for the caller, or
    raise ACCESS_DENIED. Used by tools outside this module (for example
    query_analytics) that must apply the same role and region scoping as
    the reporting tools.
    """
    allowed = _allowed_properties(caller_token)
    claims = _decode_claims(caller_token)
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    is_chain_level = bool(set(groups) & CHAIN_LEVEL_GROUPS)
    return is_chain_level, frozenset(p["propertyId"] for p in allowed)


def caller_sub(caller_token: "str | None") -> "str | None":
    """The caller's `sub` claim, or None if the token is missing or
    unreadable. Used to bind recommendation records to the analyst who
    proposed them.
    """
    sub = _decode_claims(caller_token).get("sub")
    return sub if isinstance(sub, str) and sub else None


def _parse_date(value: str, field: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ReportingError(f"invalid_date_format: {field} must be YYYY-MM-DD") from exc


def validate_date_range(start_date: str, end_date: str) -> "tuple[datetime.date, datetime.date]":
    """Enforce the 92-day cap with a model-readable error before any work."""
    start = _parse_date(start_date, "startDate")
    end = _parse_date(end_date, "endDate")

    if end < start:
        raise ReportingError("invalid_date_range: endDate is before startDate")

    span_days = (end - start).days + 1
    if span_days > MAX_RANGE_DAYS:
        raise ReportingError(
            f"date_range_too_long: {span_days} days requested, maximum is {MAX_RANGE_DAYS} days"
        )
    return start, end


def _public_property(prop: dict) -> dict:
    return {k: prop[k] for k in ("propertyId", "name", "city", "state", "region")}


def get_range_metrics(property_id: str, start_date: str, end_date: str, caller_token: "str | None" = None) -> dict:
    start, end = validate_date_range(start_date, end_date)
    allowed = _allowed_properties(caller_token)

    if property_id in (None, "", "_all"):
        scope = allowed
        property_id = "_all"
    else:
        allowed_ids = {p["propertyId"] for p in allowed}
        if property_id not in allowed_ids:
            if property_id in sample_data.PROPERTIES_BY_ID:
                raise ReportingError(ACCESS_DENIED)
            raise ReportingError(f"unknown_property: {property_id}")
        scope = [sample_data.PROPERTIES_BY_ID[property_id]]

    # Data exists up to today. Future dates in the request are trimmed and
    # reported via dataThrough, instead of returning zeros that look real.
    data_end = min(end, _today())

    daily = []
    totals = {
        "revenue": 0.0,
        "roomNightsSold": 0,
        "reservationsCreated": 0,
        "reservationsCancelled": 0,
        "checkIns": 0,
        "checkOuts": 0,
    }
    total_rooms = sum(p["totalRooms"] for p in scope)
    available_room_nights = 0

    for day in sample_data.date_range(start, data_end):
        day_totals = {k: 0 for k in totals}
        for prop in scope:
            stats = sample_data.daily_stats(prop, day)
            for key in day_totals:
                day_totals[key] += stats[key]
        day_totals["revenue"] = round(day_totals["revenue"], 2)
        day_totals["date"] = day.isoformat()
        day_totals["occupancyPercent"] = (
            round(day_totals["roomNightsSold"] / total_rooms * 100, 1) if total_rooms else 0.0
        )
        daily.append(day_totals)
        available_room_nights += total_rooms
        for key in totals:
            totals[key] += day_totals[key]

    totals["revenue"] = round(totals["revenue"], 2)
    totals["averageOccupancyPercent"] = (
        round(totals["roomNightsSold"] / available_room_nights * 100, 1) if available_room_nights else 0.0
    )
    totals["totalRooms"] = total_rooms
    totals["propertyCount"] = len(scope)

    return {
        "propertyId": property_id,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dataThrough": data_end.isoformat() if data_end >= start else None,
        "totals": totals,
        "dailyBreakdown": daily,
    }


def get_occupancy(start_date: str, end_date: str, caller_token: "str | None" = None) -> dict:
    """Point-in-time snapshot for today. The dates are echoed back for
    context but do not change the figures, matching a live occupancy board.
    """
    allowed = _allowed_properties(caller_token)
    today = _today()

    rows = []
    for prop in allowed:
        stats = sample_data.daily_stats(prop, today)
        rows.append(
            {
                "propertyId": prop["propertyId"],
                "name": prop["name"],
                "city": prop["city"],
                "region": prop["region"],
                "totalRooms": stats["totalRooms"],
                "occupiedRooms": stats["occupiedRooms"],
                "occupancyPercent": stats["occupancyPercent"],
                "revenueToday": stats["revenue"],
                "checkInsToday": stats["checkIns"],
                "checkOutsToday": stats["checkOuts"],
            }
        )

    total_rooms = sum(r["totalRooms"] for r in rows)
    total_occupied = sum(r["occupiedRooms"] for r in rows)
    return {
        "startDate": start_date,
        "endDate": end_date,
        "snapshotDate": today.isoformat(),
        "properties": rows,
        "summary": {
            "totalProperties": len(rows),
            "totalRooms": total_rooms,
            "totalOccupied": total_occupied,
            "overallOccupancy": round(total_occupied / total_rooms * 100, 1) if total_rooms else 0.0,
        },
    }


def list_properties(caller_token: "str | None" = None) -> dict:
    allowed = _allowed_properties(caller_token)
    return {"properties": [_public_property(p) for p in allowed]}
