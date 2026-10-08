# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Deterministic synthetic hotel data for the sample.

This sample ships without a real property-management system. Instead, every
figure the reporting tools return is computed on the fly from a fixed
portfolio of fictional AnyCompany hotels plus a seeded hash of
(property, date, metric). The same inputs always produce the same numbers,
so answers are reproducible across calls, deployments, and accounts, and no
database is needed.

scripts/generate_sample_events.py imports this module to build the Athena
event lake, so event counts in the lake (check-ins, check-outs, bookings,
cancellations) match the reporting figures for the same property and day.

To connect a real system instead, replace the functions in
tools/lib/reporting_client.py; this module can then be deleted.
"""
import datetime
import hashlib
import math
import uuid

from .region import derive_region

# Fixed namespace so property IDs are stable UUIDs across runs.
_PROPERTY_NAMESPACE = uuid.UUID("6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f")

# (name, city, state) for the fictional portfolio.
_PORTFOLIO = [
    ("AnyCompany Bay Austin Hotel & Spa", "Austin", "TX"),
    ("AnyCompany Bay Bend Residences", "Bend", "OR"),
    ("AnyCompany Bay Detroit Spa", "Detroit", "MI"),
    ("AnyCompany Bay Houston Retreat", "Houston", "TX"),
    ("AnyCompany Bay Juneau Spa", "Juneau", "AK"),
    ("AnyCompany Bay Miami Retreat", "Miami", "FL"),
    ("AnyCompany Bay Savannah Inn", "Savannah", "GA"),
    ("AnyCompany Boston Inn", "Boston", "MA"),
    ("AnyCompany Grand Atlanta Boutique Hotel", "Atlanta", "GA"),
    ("AnyCompany Grand Dallas Inn", "Dallas", "TX"),
    ("AnyCompany Grand Providence Resort", "Providence", "RI"),
    ("AnyCompany Jacksonville Inn", "Jacksonville", "FL"),
    ("AnyCompany Kauai Hotel", "Kauai", "HI"),
    ("AnyCompany Lanai Hotel & Spa", "Lanai", "HI"),
    ("AnyCompany Park Albuquerque Hotel", "Albuquerque", "NM"),
    ("AnyCompany Park Big Island Hotel & Spa", "Big Island", "HI"),
    ("AnyCompany Park Charleston Inn", "Charleston", "SC"),
    ("AnyCompany Park Denver Hotel", "Denver", "CO"),
    ("AnyCompany Park Minneapolis Club", "Minneapolis", "MN"),
    ("AnyCompany Park Napa Hotel & Spa", "Napa", "CA"),
    ("AnyCompany Park Park City Hotel & Spa", "Park City", "UT"),
    ("AnyCompany Park San Diego Hotel", "San Diego", "CA"),
    ("AnyCompany Park Tampa Club", "Tampa", "FL"),
    ("AnyCompany Park Washington Club", "Washington", "DC"),
    ("AnyCompany Plaza Anchorage Retreat", "Anchorage", "AK"),
    ("AnyCompany Plaza Maui Spa", "Maui", "HI"),
    ("AnyCompany Plaza Orlando Residences", "Orlando", "FL"),
    ("AnyCompany Plaza Portland Residences", "Portland", "OR"),
    ("AnyCompany Plaza Scottsdale Resort", "Scottsdale", "AZ"),
    ("AnyCompany Resort & Aspen Spa", "Aspen", "CO"),
    ("AnyCompany Resort & Indianapolis Residences", "Indianapolis", "IN"),
    ("AnyCompany Suites Boise Club", "Boise", "ID"),
    ("AnyCompany Suites Honolulu Residences", "Honolulu", "HI"),
    ("AnyCompany Suites Jackson Hole Retreat", "Jackson Hole", "WY"),
    ("AnyCompany Suites Santa Barbara Club", "Santa Barbara", "CA"),
    ("AnyCompany Tower Chicago Hotel & Spa", "Chicago", "IL"),
    ("AnyCompany Tower Cleveland Lodge", "Cleveland", "OH"),
    ("AnyCompany Tower Fort Lauderdale Residences", "Fort Lauderdale", "FL"),
    ("AnyCompany Tower Fort Worth Inn", "Fort Worth", "TX"),
    ("AnyCompany Tower Key West Spa", "Key West", "FL"),
    ("AnyCompany Tower Los Angeles Resort", "Los Angeles", "CA"),
    ("AnyCompany Tower San Antonio Hotel", "San Antonio", "TX"),
    ("AnyCompany Tower San Francisco Hotel & Spa", "San Francisco", "CA"),
    ("AnyCompany Tower Seattle Resort", "Seattle", "WA"),
    ("AnyCompany Vista New York Lodge", "New York", "NY"),
    ("AnyCompany Vista Philadelphia Club", "Philadelphia", "PA"),
    ("AnyCompany Vista Spokane Inn", "Spokane", "WA"),
    ("The AnyCompany Nashville Inn", "Nashville", "TN"),
    ("The AnyCompany Olympia Inn", "Olympia", "WA"),
    ("The AnyCompany Salt Lake City Inn", "Salt Lake City", "UT"),
]

# Per-region baseline occupancy and average daily rate (USD). Chosen so the
# regions tell different stories when compared.
_REGION_PROFILE = {
    "Northeast": {"occupancy": 0.74, "adr": 245.0, "cancel_rate": 0.06},
    "Southeast": {"occupancy": 0.68, "adr": 210.0, "cancel_rate": 0.08},
    "Midwest": {"occupancy": 0.61, "adr": 165.0, "cancel_rate": 0.05},
    "West": {"occupancy": 0.66, "adr": 260.0, "cancel_rate": 0.07},
    "South": {"occupancy": 0.63, "adr": 180.0, "cancel_rate": 0.11},
    "Other": {"occupancy": 0.60, "adr": 175.0, "cancel_rate": 0.07},
}

AVERAGE_LENGTH_OF_STAY = 2.4


def _unit(*parts) -> float:
    """Deterministic pseudo-random float in [0, 1) from the given parts."""
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _build_properties() -> list:
    properties = []
    for name, city, state in _PORTFOLIO:
        property_id = str(uuid.uuid5(_PROPERTY_NAMESPACE, name))
        properties.append(
            {
                "propertyId": property_id,
                "name": name,
                "city": city,
                "state": state,
                "region": derive_region(state),
                "totalRooms": 60 + int(_unit(property_id, "rooms") * 240),
            }
        )
    return properties


PROPERTIES = _build_properties()
PROPERTIES_BY_ID = {p["propertyId"]: p for p in PROPERTIES}


def daily_stats(prop: dict, day: datetime.date) -> dict:
    """All metrics for one property on one day."""
    profile = _REGION_PROFILE.get(prop["region"], _REGION_PROFILE["Other"])
    pid = prop["propertyId"]
    iso = day.isoformat()

    weekend_bump = 0.08 if day.weekday() in (4, 5) else 0.0
    # Smooth seasonal curve peaking in late July.
    seasonal = 0.07 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 205) / 365)
    property_bias = (_unit(pid, "bias") - 0.5) * 0.16
    noise = (_unit(pid, iso, "occ") - 0.5) * 0.10
    occupancy = min(0.98, max(0.15, profile["occupancy"] + weekend_bump + seasonal + property_bias + noise))

    rooms = prop["totalRooms"]
    occupied = round(rooms * occupancy)
    adr = profile["adr"] * (0.9 + _unit(pid, "adr") * 0.25) * (1 + weekend_bump)
    revenue = round(occupied * adr, 2)

    check_ins = max(0, round(occupied / AVERAGE_LENGTH_OF_STAY * (0.9 + _unit(pid, iso, "in") * 0.2)))
    check_outs = max(0, round(occupied / AVERAGE_LENGTH_OF_STAY * (0.9 + _unit(pid, iso, "out") * 0.2)))
    created = max(0, round(check_ins * (1.15 + _unit(pid, iso, "created") * 0.3)))
    cancel_rate = profile["cancel_rate"] * (0.6 + _unit(pid, iso, "cancel") * 0.8)
    cancelled = round(created * cancel_rate)

    return {
        "date": iso,
        "totalRooms": rooms,
        "occupiedRooms": occupied,
        "occupancyPercent": round(occupied / rooms * 100, 1),
        "revenue": revenue,
        "roomNightsSold": occupied,
        "checkIns": check_ins,
        "checkOuts": check_outs,
        "reservationsCreated": created,
        "reservationsCancelled": cancelled,
    }


def date_range(start: datetime.date, end: datetime.date):
    day = start
    while day <= end:
        yield day
        day += datetime.timedelta(days=1)
