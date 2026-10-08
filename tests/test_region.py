# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


from lib.region import derive_region, DEFAULT_REGION



def test_derive_region_northeast():
    assert derive_region("NY") == "Northeast"
    assert derive_region("MA") == "Northeast"


def test_derive_region_southeast():
    assert derive_region("FL") == "Southeast"
    assert derive_region("DC") == "Southeast"


def test_derive_region_midwest():
    assert derive_region("IL") == "Midwest"


def test_derive_region_west():
    assert derive_region("CA") == "West"
    assert derive_region("HI") == "West"
    assert derive_region("AK") == "West"


def test_derive_region_south():
    assert derive_region("TX") == "South"


def test_derive_region_unknown_state_falls_back_to_other():
    assert derive_region("ZZ") == DEFAULT_REGION


def test_derive_region_missing_state_falls_back_to_other():
    assert derive_region(None) == DEFAULT_REGION
    assert derive_region("") == DEFAULT_REGION


def test_derive_region_lowercase_state_normalized():
    assert derive_region("tx") == "South"
