# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The write tools. Core security property under test: a
CONFIRMED record can NEVER be created except by first transitioning an
EXISTING PENDING record -- there is no code path from "nothing" straight
to CONFIRMED. This is what makes "human-in-the-loop confirm" a real
server-side gate rather than a prompt instruction the model could ignore.
"""
import json

import pytest

from lib import recommendation_client
from lib.recommendation_client import (
    ACCESS_DENIED,
    RecommendationError,
    propose_recommendation,
    record_recommendation,
)


SUB = "analyst-a"
OTHER_SUB = "analyst-b"


class FakeNoSuchKeyError(Exception):
    pass


class FakeS3Client:
    """In-memory fake standing in for boto3's S3 client -- enough surface
    (get_object/put_object/exceptions.NoSuchKey) to exercise the real
    propose/confirm state machine without hitting AWS.
    """

    def __init__(self):
        self.objects: dict = {}

        class _Exceptions:
            NoSuchKey = FakeNoSuchKeyError

        self.exceptions = _Exceptions()

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey()

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": _Body(self.objects[Key])}


@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(recommendation_client, "_get_s3_client", lambda: fake)
    monkeypatch.setenv("ARTIFACTS_BUCKET_NAME", "revagent-dev-artifacts-123456789012")
    return fake


# ---------------------------------------------------------------------------
# propose_recommendation -- writes PENDING only, never CONFIRMED
# ---------------------------------------------------------------------------

def test_propose_recommendation_writes_pending_status(_fake_s3):
    result = propose_recommendation(summary="Raise weekday rate 5%", reasoning="Occupancy trending up", proposer_sub=SUB)
    assert result["status"] == "PENDING"
    assert "recommendationId" in result

    stored = json.loads(_fake_s3.objects[f"recommendations/{result['recommendationId']}.json"])
    assert stored["status"] == "PENDING"
    assert stored["confirmedAt"] is None


def test_propose_recommendation_rejects_missing_summary():
    with pytest.raises(RecommendationError, match="missing_summary"):
        propose_recommendation(summary="", reasoning="some reasoning", proposer_sub=SUB)


def test_propose_recommendation_rejects_missing_reasoning():
    with pytest.raises(RecommendationError, match="missing_reasoning"):
        propose_recommendation(summary="Some summary", reasoning="", proposer_sub=SUB)


def test_propose_recommendation_rejects_oversized_reasoning():
    huge_reasoning = "x" * (recommendation_client.MAX_REASONING_CHARS + 1)
    with pytest.raises(RecommendationError, match="reasoning_too_long"):
        propose_recommendation(summary="Summary", reasoning=huge_reasoning, proposer_sub=SUB)


def test_propose_recommendation_generates_unique_ids(_fake_s3):
    r1 = propose_recommendation(summary="A", reasoning="A reasoning", proposer_sub=SUB)
    r2 = propose_recommendation(summary="B", reasoning="B reasoning", proposer_sub=SUB)
    assert r1["recommendationId"] != r2["recommendationId"]


# ---------------------------------------------------------------------------
# record_recommendation -- the actual gate. Cannot go straight to CONFIRMED.
# ---------------------------------------------------------------------------

def test_record_recommendation_rejects_nonexistent_id(_fake_s3):
    """This is the core security property: you cannot confirm a
    recommendation that was never proposed. There is no code path from
    "nothing" to CONFIRMED.
    """
    with pytest.raises(RecommendationError, match="recommendation_not_found"):
        record_recommendation(recommendation_id="never-existed-id", confirmed=True, caller_sub=SUB)


def test_record_recommendation_rejects_confirmed_false(_fake_s3):
    proposal = propose_recommendation(summary="S", reasoning="R", proposer_sub=SUB)
    with pytest.raises(RecommendationError, match="confirmation_required"):
        record_recommendation(recommendation_id=proposal["recommendationId"], confirmed=False, caller_sub=SUB)


def test_record_recommendation_rejects_confirmed_omitted(_fake_s3):
    """confirmed=None (the default if the model omits the arg entirely)
    must be treated the same as an explicit rejection -- there is no
    'assume yes' default.
    """
    proposal = propose_recommendation(summary="S", reasoning="R", proposer_sub=SUB)
    with pytest.raises(RecommendationError, match="confirmation_required"):
        record_recommendation(recommendation_id=proposal["recommendationId"], confirmed=None, caller_sub=SUB)


def test_record_recommendation_happy_path_transitions_pending_to_confirmed(_fake_s3):
    proposal = propose_recommendation(summary="Raise rate", reasoning="Demand signal", proposer_sub=SUB)
    result = record_recommendation(recommendation_id=proposal["recommendationId"], confirmed=True, caller_sub=SUB)

    assert result["status"] == "CONFIRMED"
    assert result["alreadyConfirmed"] is False

    stored = json.loads(_fake_s3.objects[f"recommendations/{proposal['recommendationId']}.json"])
    assert stored["status"] == "CONFIRMED"
    assert stored["confirmedAt"] is not None
    # Original proposal content is preserved through the transition, not
    # overwritten/lost.
    assert stored["summary"] == "Raise rate"
    assert stored["reasoning"] == "Demand signal"


def test_record_recommendation_is_idempotent_on_already_confirmed(_fake_s3):
    proposal = propose_recommendation(summary="S", reasoning="R", proposer_sub=SUB)
    first = record_recommendation(recommendation_id=proposal["recommendationId"], confirmed=True, caller_sub=SUB)
    second = record_recommendation(recommendation_id=proposal["recommendationId"], confirmed=True, caller_sub=SUB)

    assert first["alreadyConfirmed"] is False
    assert second["alreadyConfirmed"] is True
    assert second["status"] == "CONFIRMED"


def test_record_recommendation_rejects_malformed_status(_fake_s3):
    """Defensive: if a record somehow has neither PENDING nor CONFIRMED
    status (e.g. manual tampering, a future REJECTED state), refuse to
    silently promote it rather than assuming PENDING-like behavior.
    """
    proposal = propose_recommendation(summary="S", reasoning="R", proposer_sub=SUB)
    key = f"recommendations/{proposal['recommendationId']}.json"
    record = json.loads(_fake_s3.objects[key])
    record["status"] = "REJECTED"
    _fake_s3.objects[key] = json.dumps(record).encode("utf-8")

    with pytest.raises(RecommendationError, match="invalid_state"):
        record_recommendation(recommendation_id=proposal["recommendationId"], confirmed=True, caller_sub=SUB)


def test_propose_recommendation_requires_proposer_identity(_fake_s3):
    with pytest.raises(RecommendationError, match=ACCESS_DENIED):
        propose_recommendation(summary="S", reasoning="R", proposer_sub=None)


def test_propose_recommendation_records_proposer(_fake_s3):
    proposal = propose_recommendation(summary="S", reasoning="R", proposer_sub=SUB)
    key = recommendation_client._key(proposal["recommendationId"])
    assert json.loads(_fake_s3.objects[key])["proposedBy"] == SUB


def test_record_recommendation_rejects_other_analyst(_fake_s3):
    proposal = propose_recommendation(summary="S", reasoning="R", proposer_sub=SUB)
    with pytest.raises(RecommendationError, match="recommendation_not_found"):
        record_recommendation(
            recommendation_id=proposal["recommendationId"], confirmed=True, caller_sub=OTHER_SUB
        )


def test_record_recommendation_requires_caller_identity(_fake_s3):
    proposal = propose_recommendation(summary="S", reasoning="R", proposer_sub=SUB)
    with pytest.raises(RecommendationError, match=ACCESS_DENIED):
        record_recommendation(
            recommendation_id=proposal["recommendationId"], confirmed=True, caller_sub=None
        )
