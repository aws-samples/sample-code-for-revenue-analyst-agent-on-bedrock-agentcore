#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Generate the sample event lake for the query_analytics tool.

Writes gzipped JSON Lines files in the partition layout the Glue table
expects (terraform/modules/analytics):

    <out>/source_partition=<p>/year=<y>/month=<m>/day=<d>/events.json.gz

Event counts come from tools/lib/sample_data.py, the same source the
reporting tools use, so for any property and day the lake's check-in,
check-out, booking, and cancellation counts match the reporting figures.

Uses only the Python standard library. Output is deterministic for a given
end date and --days value.

Usage:
    python3 scripts/generate_sample_events.py --out build/events [--days 45] [--end-date YYYY-MM-DD]
"""
import argparse
import datetime
import gzip
import json
import os
import shutil
import sys
import uuid

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

from lib import sample_data  # noqa: E402

_EVENT_NAMESPACE = uuid.UUID("0b8c7d6e-5f4a-4b3c-9d2e-1f0a9b8c7d6e")
_BOOKING_CHANNELS = ["direct_web", "mobile_app", "ota", "call_center", "gds"]


def _event(source_partition, detail_type, day, seq, prop, detail):
    hour = seq % 24
    minute = (seq * 7) % 60
    event_time = f"{day.isoformat()}T{hour:02d}:{minute:02d}:00Z"
    event_id = str(uuid.uuid5(_EVENT_NAMESPACE, f"{prop['propertyId']}:{day}:{detail_type}:{seq}"))
    body = {"propertyid": prop["propertyId"], "region": prop["region"]}
    body.update(detail)
    return {
        "event_id": event_id,
        "source": f"anycompany.{source_partition}",
        "detail_type": detail_type,
        "event_time": event_time,
        "region": prop["region"],
        "account": "sample",
        # Stored as a JSON string so json_extract_scalar(detail, ...) works
        # regardless of SerDe handling of nested objects.
        "detail": json.dumps(body, separators=(",", ":")),
    }


def _events_for(prop, day):
    """Yield (source_partition, event_dict) for one property-day."""
    stats = sample_data.daily_stats(prop, day)
    unit = sample_data._unit  # deterministic [0,1) helper
    pid = prop["propertyId"]

    for i in range(stats["checkIns"]):
        yield "pms", _event("pms", "checkinout.checked_in", day, i, prop, {"nights": 1 + int(unit(pid, day, "n", i) * 4)})
    for i in range(stats["checkOuts"]):
        yield "pms", _event("pms", "checkinout.checked_out", day, i, prop, {})
        yield "housekeeping", _event("housekeeping", "housekeeping.room_ready", day, i, prop, {})
        if unit(pid, day, "loyal", i) < 0.4:
            yield "loyalty", _event("loyalty", "loyalty.points_earned", day, i, prop, {"points": 100 + int(unit(pid, day, "pts", i) * 900)})

    for i in range(stats["reservationsCreated"]):
        channel = _BOOKING_CHANNELS[int(unit(pid, day, "ch", i) * len(_BOOKING_CHANNELS))]
        lead = int(unit(pid, day, "lead", i) * 60)
        yield "crs", _event("crs", "reservation.created", day, i, prop, {"bookingchannel": channel, "leadtimedays": lead})
    for i in range(stats["reservationsCancelled"]):
        # OTA bookings cancel more often, which gives driver analysis
        # something real to find.
        channel = "ota" if unit(pid, day, "cch", i) < 0.45 else _BOOKING_CHANNELS[int(unit(pid, day, "cch2", i) * len(_BOOKING_CHANNELS))]
        yield "crs", _event("crs", "reservation.cancelled", day, i, prop, {"bookingchannel": channel})
        if unit(pid, day, "refund", i) < 0.3:
            yield "payment", _event("payment", "payment.refunded", day, i, prop, {})

    if unit(pid, day, "rate") < 0.1:
        yield "billing", _event("billing", "billing.rate_adjusted", day, 0, prop, {})
        yield "audit", _event("audit", "audit.rate_plan_changed", day, 0, prop, {})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="Output directory (replaced if it exists)")
    parser.add_argument("--days", type=int, default=45, help="Number of days of history, ending at --end-date")
    parser.add_argument("--end-date", help="Last day to generate (default: today, UTC)")
    args = parser.parse_args()

    if not 1 <= args.days <= 366:
        parser.error("--days must be between 1 and 366")
    end = (
        datetime.date.fromisoformat(args.end_date)
        if args.end_date
        else datetime.datetime.now(datetime.timezone.utc).date()
    )
    start = end - datetime.timedelta(days=args.days - 1)

    out = os.path.abspath(args.out)
    if os.path.isdir(out):
        shutil.rmtree(out)

    total = 0
    for day in sample_data.date_range(start, end):
        by_partition = {}
        for prop in sample_data.PROPERTIES:
            for partition, event in _events_for(prop, day):
                by_partition.setdefault(partition, []).append(event)
        for partition, events in by_partition.items():
            folder = os.path.join(
                out, f"source_partition={partition}", f"year={day.year}", f"month={day.month}", f"day={day.day}"
            )
            os.makedirs(folder, exist_ok=True)
            # mtime=0 keeps the gzip bytes identical across runs.
            with open(os.path.join(folder, "events.json.gz"), "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                    for event in events:
                        gz.write((json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"))
            total += len(events)

    print(f"Wrote {total} events for {start} to {end} into {out}")


if __name__ == "__main__":
    main()
