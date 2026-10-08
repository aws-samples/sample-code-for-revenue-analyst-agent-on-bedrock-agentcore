# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
S3 ObjectCreated -> pre-signed URL -> SES per-analyst
email delivery.

Why this is Lambda-mediated rather than a direct S3->notification event:
S3 event notifications can only carry the raw bucket/key JSON -- there is
no way for S3 itself to compute a pre-signed URL, nor to know WHO should
receive the report. So this Lambda (triggered by S3 ObjectCreated on the
artifacts bucket's reports/ prefix, see
terraform/modules/tools/report_delivery.tf) does both: it generates the
short-lived pre-signed URL, and it reads the report's intended recipient
back off the S3 object's own metadata (the `analyst-email` key that
agent.py's generate_report stamped on at upload time -- see that tool's
docstring). It then emails ONLY that pre-signed URL (plus the report
title) to that one analyst via SES -- never the report's actual content,
and never any PII that might be inside the PDF.

Why SES and not SNS: SNS can only email confirmed topic subscribers, so it
cannot send a report to whoever asked for it. SES send_email addresses one
recipient directly. The recipient comes from the object metadata above,
which came from the analyst's validated token claims, so delivery is bound
to the authenticated caller.

Security / privacy notes:
- The pre-signed URL is scoped to GetObject on this one object key only,
  with a short expiry (PRESIGNED_URL_EXPIRY_SECONDS) -- not a durable
  public link.
- The email body carries ONLY the pointer (pre-signed URL) + title, never
  the report bytes or any figures from inside the PDF.
- SES SendEmail is granted to this Lambda's execution role only (see
  lambda.tf). While SES is in the account's sandbox, only verified
  recipient addresses receive mail; production use requires a one-time
  SES production-access request.
"""
import logging
import os
import re
import urllib.parse

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

PRESIGNED_URL_EXPIRY_SECONDS = 3600  # 1 hour -- long enough for an analyst to click through promptly, short enough to bound exposure if the email is somehow intercepted

_s3_client = None
_ses_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_ses_client():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("ses")
    return _ses_client


def _report_title_from_key(key: str) -> str:
    """Derives a human-readable title from the S3 key for the email
    subject/body, e.g. "reports/west-region-occupancy-abc123def456.pdf"
    -> "west-region-occupancy". Falls back to the raw key if the pattern
    doesn't match (never raises -- this is cosmetic, not load-bearing).
    """
    filename = key.rsplit("/", 1)[-1]
    match = re.match(r"^(.*)-[0-9a-f]{12}\.pdf$", filename)
    return match.group(1).replace("-", " ") if match else filename


def _resolve_recipient(bucket: str, key: str, default_recipient: "str | None") -> "str | None":
    """Reads the intended recipient off the report object's own metadata.

    generate_report (agent.py) stamps the authenticated analyst's verified
    email onto the object as user metadata `analyst-email` at upload. S3
    lowercases and strips the `x-amz-meta-` prefix, so head_object returns
    it under Metadata["analyst-email"]. If it's absent (e.g. a report
    produced by a caller whose token had no email claim), fall back to the
    configured default recipient rather than dropping the report silently.
    """
    try:
        head = _get_s3_client().head_object(Bucket=bucket, Key=key)
    except ClientError:
        logger.exception("head_object failed for %s/%s; using default recipient", bucket, key)
        return default_recipient

    email = (head.get("Metadata") or {}).get("analyst-email")
    if email:
        return email
    logger.info("No analyst-email metadata on %s; using default recipient", key)
    return default_recipient


def handle_report_created_event(event: dict) -> dict:
    """Processes one or more S3 ObjectCreated records. Only records under
    the reports/ prefix are handled -- the artifacts bucket also holds
    athena-results/, which must NOT trigger an analyst
    notification.
    """
    sender = os.environ["REPORT_SENDER_EMAIL"]
    default_recipient = os.environ.get("REPORT_DEFAULT_RECIPIENT_EMAIL") or sender
    processed = 0
    skipped_no_recipient = 0
    failed = 0

    for record in event.get("Records", []):
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        key = s3_info.get("object", {}).get("key")

        if not bucket or not key:
            logger.warning("S3 event record missing bucket/key, skipping: %s", record)
            continue

        # S3 event keys are URL-encoded; decode before using as an actual
        # S3 API key (a key containing spaces or unicode would otherwise
        # mismatch what generate_presigned_url / head_object expect).
        key = urllib.parse.unquote_plus(key)

        if not key.startswith("reports/"):
            logger.info("Skipping non-report object: %s", key)
            continue

        recipient = _resolve_recipient(bucket, key, default_recipient)
        if not recipient:
            # No per-analyst email AND no default configured -- do not send
            # blind. Log and skip rather than guess an address.
            logger.warning("No recipient resolvable for %s; skipping delivery", key)
            skipped_no_recipient += 1
            continue

        url = _get_s3_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
        )

        title = _report_title_from_key(key)
        body = (
            f"A new analyst report is ready: {title}\n\n"
            f"Download link (expires in {PRESIGNED_URL_EXPIRY_SECONDS // 60} minutes):\n"
            f"{url}\n\n"
            "This link is scoped to this one report and will stop working "
            "after it expires. If it expires before you can download it, "
            "ask the agent to regenerate the report."
        )

        try:
            _get_ses_client().send_email(
                Source=sender,
                Destination={"ToAddresses": [recipient]},
                Message={
                    "Subject": {"Data": f"Revenue Analyst Agent: report ready — {title}"[:100]},
                    "Body": {"Text": {"Data": body}},
                },
            )
        except ClientError:
            # A failed send must NOT be counted or logged as success. One
            # bad recipient (e.g. an unverified address while SES is in the
            # sandbox) should not abort delivery of the OTHER reports in
            # this batch, so log and continue rather than raise. Do NOT log
            # the recipient address itself (PII).
            logger.exception("SES send_email failed for %s; skipping this report", key)
            failed += 1
            continue

        # Do NOT log the recipient address or the pre-signed URL at info
        # level -- both are sensitive (PII / a live download credential).
        logger.info("Sent report-ready email for %s (pointer-only, recipient/URL not logged)", key)
        processed += 1

    return {"processed": processed, "skipped_no_recipient": skipped_no_recipient, "failed": failed}
