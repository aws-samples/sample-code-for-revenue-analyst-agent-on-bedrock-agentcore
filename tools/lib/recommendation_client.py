# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The only write tools in this sample. They write recommendation records to
the sample's own artifacts bucket (recommendations/ prefix) and nothing else.

Human-in-the-loop confirm, made real, not just prompt-trusted:
-----------------------------------------------------------------
Strands' native interrupt mechanism (agent.interrupt / tool_context.interrupt)
requires the CALLER of the agent to keep a paused run alive across a
follow-up call supplying the human's response -- that fits a stateful chat
client holding a live connection, not this build's AgentCore Runtime
invocation model (one-shot InvokeAgentRuntime calls, with no client holding
a paused run open). So "confirm" here is enforced as a real
SERVER-SIDE STATE MACHINE across two separate tool calls / conversational
turns, not a UI checkbox the model could just claim was checked:

  1. propose_recommendation -- writes a PENDING record to S3. Returns its
     recommendationId. This is the "here's what I'd suggest" step -- it
     commits nothing as final.
  2. record_recommendation -- given a recommendationId AND the analyst's
     explicit textual confirmation (passed by the model as confirmed=True,
     which the SYSTEM PROMPT instructs the model to set ONLY after the
     human has explicitly said yes in the conversation), transitions that
     SAME record from PENDING -> CONFIRMED. It is a HARD ERROR to call this
     with a recommendationId that doesn't exist or isn't PENDING -- there
     is no code path to go straight from "nothing" to CONFIRMED.

This means a CONFIRMED record can only ever exist if a PENDING record for
it was written first, in an earlier, separate tool call -- the model
cannot fabricate a confirmation for a recommendation it never proposed, and
cannot skip the propose step. The actual human-language confirmation
happens in the conversation (system prompt requirement); this module's
contract is what makes that gate STRUCTURALLY enforced server-side, not
just prompt-trusted.

Idempotency: confirming an already-CONFIRMED record returns the existing
record unchanged (not an error, not a duplicate write) -- safe to retry.

Immutability: records are written to a VERSIONED bucket (see terraform/modules/foundation/artifacts.tf) and
never overwritten in place for CONFIRMED records -- confirming writes a
NEW object version, so the PENDING -> CONFIRMED transition itself is part
of the immutable history, not a silent in-place edit.
"""
import datetime
import json
import logging
import os
import uuid

import boto3

logger = logging.getLogger(__name__)

MAX_REASONING_CHARS = 4000  # bounds a single record's size; this is a summary, not a transcript

_s3_client = None


ACCESS_DENIED = "recommendation_access_denied"


class RecommendationError(Exception):
    """Raised for validation failures and S3 errors. Message is safe to
    surface to the model -- never includes raw AWS internals.
    """


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _bucket() -> str:
    return os.environ["ARTIFACTS_BUCKET_NAME"]


def _key(recommendation_id: str) -> str:
    return f"recommendations/{recommendation_id}.json"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _get_record(recommendation_id: str) -> "dict | None":
    try:
        resp = _get_s3_client().get_object(Bucket=_bucket(), Key=_key(recommendation_id))
    except _get_s3_client().exceptions.NoSuchKey:
        return None
    except Exception as exc:
        logger.warning("Failed to read recommendation %s: %s", recommendation_id, type(exc).__name__)
        raise RecommendationError("recommendation_read_failed") from exc
    return json.loads(resp["Body"].read())


def _put_record(recommendation_id: str, record: dict) -> None:
    try:
        _get_s3_client().put_object(
            Bucket=_bucket(),
            Key=_key(recommendation_id),
            Body=json.dumps(record, indent=2).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
    except Exception as exc:
        logger.warning("Failed to write recommendation %s: %s", recommendation_id, type(exc).__name__)
        raise RecommendationError("recommendation_write_failed") from exc


def propose_recommendation(
    summary: str,
    reasoning: str,
    supporting_data: "dict | None" = None,
    proposer_sub: "str | None" = None,
) -> dict:
    """Writes a new PENDING recommendation record. This is NOT the final
    write -- record_recommendation must be called afterward, with the
    returned recommendationId, to confirm it.

    The record stores the proposing analyst's `sub` claim, and only that
    same analyst can later confirm it (see record_recommendation).
    """
    if not proposer_sub:
        raise RecommendationError(ACCESS_DENIED)
    if not summary or not summary.strip():
        raise RecommendationError("missing_summary")
    if not reasoning or not reasoning.strip():
        raise RecommendationError("missing_reasoning")
    if len(reasoning) > MAX_REASONING_CHARS:
        raise RecommendationError(
            f"reasoning_too_long: {len(reasoning)} chars, maximum is {MAX_REASONING_CHARS}"
        )

    recommendation_id = uuid.uuid4().hex
    record = {
        "recommendationId": recommendation_id,
        "status": "PENDING",
        "summary": summary.strip(),
        "reasoning": reasoning.strip(),
        "supportingData": supporting_data or {},
        "proposedAt": _now_iso(),
        "confirmedAt": None,
        "proposedBy": proposer_sub,
    }
    _put_record(recommendation_id, record)
    logger.info("Recommendation proposed: %s", recommendation_id)
    return {"recommendationId": recommendation_id, "status": "PENDING"}


def record_recommendation(
    recommendation_id: str,
    confirmed: bool,
    caller_sub: "str | None" = None,
) -> dict:
    """Transitions a PENDING recommendation to CONFIRMED. `confirmed` must
    be True -- the system prompt instructs the model to only ever call
    this with confirmed=True after the analyst has explicitly said yes in
    the conversation; confirmed=False (or omitted) is a hard rejection, not
    a soft default, so a model that calls this prematurely gets an error
    rather than a silently-written record.

    Raises RecommendationError if:
    - recommendation_id doesn't exist at all
    - it exists but confirmed is not True
    - it exists, is PENDING, but is somehow malformed

    Only the analyst who proposed the record (matching `sub` claim) can
    confirm it. A record proposed by someone else is reported as not found,
    so callers cannot probe for other analysts' recommendation IDs.

    Idempotent: if the record is ALREADY CONFIRMED, returns it unchanged
    (safe retry), rather than erroring or double-writing.
    """
    if confirmed is not True:
        raise RecommendationError("confirmation_required: confirmed must be true")
    if not caller_sub:
        raise RecommendationError(ACCESS_DENIED)

    record = _get_record(recommendation_id)
    if record is None or record.get("proposedBy") != caller_sub:
        raise RecommendationError(f"recommendation_not_found: {recommendation_id}")

    if record.get("status") == "CONFIRMED":
        logger.info("Recommendation %s already CONFIRMED, idempotent return", recommendation_id)
        return {"recommendationId": recommendation_id, "status": "CONFIRMED", "alreadyConfirmed": True}

    if record.get("status") != "PENDING":
        raise RecommendationError(
            f"invalid_state: recommendation {recommendation_id} has status "
            f"{record.get('status')!r}, expected PENDING"
        )

    record["status"] = "CONFIRMED"
    record["confirmedAt"] = _now_iso()
    _put_record(recommendation_id, record)
    logger.info("Recommendation confirmed: %s", recommendation_id)
    return {"recommendationId": recommendation_id, "status": "CONFIRMED", "alreadyConfirmed": False}
