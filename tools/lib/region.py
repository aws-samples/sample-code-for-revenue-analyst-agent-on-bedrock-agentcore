# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
US state -> sales region mapping for the sample portfolio.

Regions: Northeast, Southeast, Midwest, West, South, and Other (anything
unmapped). RegionalManager users are scoped by the `custom:region` claim on
their Cognito token, which must be one of these values.
"""

_STATE_TO_REGION = {
    # Northeast
    "NY": "Northeast", "MA": "Northeast", "CT": "Northeast", "NJ": "Northeast",
    "PA": "Northeast", "ME": "Northeast", "NH": "Northeast", "RI": "Northeast",
    "VT": "Northeast",
    # Southeast
    "FL": "Southeast", "GA": "Southeast", "NC": "Southeast", "SC": "Southeast",
    "VA": "Southeast", "TN": "Southeast", "AL": "Southeast", "MS": "Southeast",
    "KY": "Southeast", "WV": "Southeast", "DC": "Southeast", "MD": "Southeast",
    "DE": "Southeast",
    # Midwest
    "IL": "Midwest", "IN": "Midwest", "IA": "Midwest", "KS": "Midwest",
    "MI": "Midwest", "MN": "Midwest", "MO": "Midwest", "NE": "Midwest",
    "ND": "Midwest", "OH": "Midwest", "SD": "Midwest", "WI": "Midwest",
    # West
    "CA": "West", "OR": "West", "WA": "West", "NV": "West", "AZ": "West",
    "UT": "West", "CO": "West", "ID": "West", "MT": "West", "WY": "West",
    "AK": "West", "HI": "West", "NM": "West",
    # South
    "TX": "South", "OK": "South", "AR": "South", "LA": "South",
}

DEFAULT_REGION = "Other"

VALID_REGIONS = frozenset(list(_STATE_TO_REGION.values()) + [DEFAULT_REGION])


def derive_region(state: "str | None") -> str:
    """Map a US state abbreviation to a region. Unknown or missing -> Other."""
    if not state:
        return DEFAULT_REGION
    return _STATE_TO_REGION.get(state.strip().upper(), DEFAULT_REGION)
