# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
S3 ObjectCreated -> pre-signed URL -> SES per-analyst
email. Two core properties under test:
  1. The email must NEVER contain report content -- only a pointer
     (pre-signed URL) and a title derived from the filename.
  2. Delivery is per-analyst: the recipient is read from the report
     object's own `analyst-email` metadata (stamped by generate_report
     from the caller's validated token claims), falling back to a
     configured default only when that metadata is absent.
"""
import pytest

from lib import report_delivery
from lib.report_delivery import handle_report_created_event

SENDER = "reports@revagent.example"
DEFAULT = "fallback@revagent.example"


def _s3_event(bucket="revagent-dev-artifacts-123456789012", key="reports/west-summary-abc123def456.pdf"):
    return {
        "Records": [
            {"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}
        ]
    }


class FakeS3Client:
    def __init__(self, url="https://example-presigned.invalid/report.pdf", metadata=None):
        self.url = url
        # metadata: dict keyed by object key -> the object's user metadata
        self.metadata = metadata if metadata is not None else {}
        self.presign_calls = []
        self.head_calls = []

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presign_calls.append({"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn})
        return self.url

    def head_object(self, Bucket, Key):
        self.head_calls.append({"Bucket": Bucket, "Key": Key})
        return {"Metadata": self.metadata.get(Key, {})}


class FakeSesClient:
    def __init__(self):
        self.sent = []

    def send_email(self, **kwargs):
        self.sent.append(kwargs)


def _wire(monkeypatch, s3, ses):
    monkeypatch.setattr(report_delivery, "_get_s3_client", lambda: s3)
    monkeypatch.setattr(report_delivery, "_get_ses_client", lambda: ses)
    monkeypatch.setenv("REPORT_SENDER_EMAIL", SENDER)
    monkeypatch.setenv("REPORT_DEFAULT_RECIPIENT_EMAIL", DEFAULT)


def test_emails_pointer_only_no_content(monkeypatch):
    key = "reports/west-summary-abc123def456.pdf"
    fake_s3 = FakeS3Client(metadata={key: {"analyst-email": "analyst@example.com"}})
    fake_ses = FakeSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    result = handle_report_created_event(_s3_event(key=key))

    assert result == {"processed": 1, "skipped_no_recipient": 0, "failed": 0}
    assert len(fake_ses.sent) == 1
    body = fake_ses.sent[0]["Message"]["Body"]["Text"]["Data"]
    # The pointer (pre-signed URL) IS in the body...
    assert fake_s3.url in body
    # ...and nothing resembling report CONTENT is: this function never
    # reads the object body, only its key/metadata, so pointer-only is
    # structurally guaranteed, not just a string-absence check.
    assert fake_ses.sent[0]["Source"] == SENDER


def test_delivers_to_the_analyst_from_object_metadata(monkeypatch):
    """The per-analyst property: recipient comes from the object's
    analyst-email metadata, NOT a shared address."""
    key = "reports/west-summary-abc123def456.pdf"
    fake_s3 = FakeS3Client(metadata={key: {"analyst-email": "analyst-a@example.com"}})
    fake_ses = FakeSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    handle_report_created_event(_s3_event(key=key))

    assert fake_ses.sent[0]["Destination"]["ToAddresses"] == ["analyst-a@example.com"]


def test_two_reports_go_to_two_different_analysts(monkeypatch):
    """Distinct callers get distinct emails -- the whole point of the
    SES change over the old single-subscriber SNS topic."""
    k1 = "reports/a-abc123def456.pdf"
    k2 = "reports/b-abc123def456.pdf"
    fake_s3 = FakeS3Client(metadata={
        k1: {"analyst-email": "analyst-a@example.com"},
        k2: {"analyst-email": "analyst-b@example.com"},
    })
    fake_ses = FakeSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    event = {"Records": [
        {"s3": {"bucket": {"name": "b"}, "object": {"key": k1}}},
        {"s3": {"bucket": {"name": "b"}, "object": {"key": k2}}},
    ]}
    result = handle_report_created_event(event)

    assert result == {"processed": 2, "skipped_no_recipient": 0, "failed": 0}
    recipients = [s["Destination"]["ToAddresses"][0] for s in fake_ses.sent]
    assert recipients == ["analyst-a@example.com", "analyst-b@example.com"]


def test_falls_back_to_default_recipient_when_no_metadata(monkeypatch):
    """A report with no analyst-email metadata (e.g. a token without an
    email claim) must still be delivered -- to the configured default,
    not dropped."""
    key = "reports/west-summary-abc123def456.pdf"
    fake_s3 = FakeS3Client(metadata={key: {}})  # no analyst-email
    fake_ses = FakeSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    result = handle_report_created_event(_s3_event(key=key))

    assert result == {"processed": 1, "skipped_no_recipient": 0, "failed": 0}
    assert fake_ses.sent[0]["Destination"]["ToAddresses"] == [DEFAULT]


def test_scopes_presigned_url_to_the_exact_object(monkeypatch):
    key = "reports/east-summary-123456789012.pdf"
    fake_s3 = FakeS3Client(metadata={key: {"analyst-email": "analyst@example.com"}})
    fake_ses = FakeSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    handle_report_created_event(_s3_event(key=key))

    call = fake_s3.presign_calls[0]
    assert call["operation"] == "get_object"
    assert call["Params"]["Key"] == key
    assert call["ExpiresIn"] == report_delivery.PRESIGNED_URL_EXPIRY_SECONDS


def test_skips_non_report_prefix(monkeypatch):
    """The artifacts bucket also holds athena-results/ -- those
    object creations must NEVER trigger an analyst email."""
    fake_s3 = FakeS3Client()
    fake_ses = FakeSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    result = handle_report_created_event(_s3_event(key="athena-results/some-query-abc.csv"))

    assert result == {"processed": 0, "skipped_no_recipient": 0, "failed": 0}
    assert fake_ses.sent == []
    assert fake_s3.presign_calls == []
    assert fake_s3.head_calls == []


def test_handles_multiple_records(monkeypatch):
    k1 = "reports/a-abc123def456.pdf"
    k2 = "reports/b-abc123def456.pdf"
    fake_s3 = FakeS3Client(metadata={
        k1: {"analyst-email": "analyst@example.com"},
        k2: {"analyst-email": "analyst@example.com"},
    })
    fake_ses = FakeSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    event = {"Records": [
        {"s3": {"bucket": {"name": "b"}, "object": {"key": k1}}},
        {"s3": {"bucket": {"name": "b"}, "object": {"key": k2}}},
    ]}
    result = handle_report_created_event(event)

    assert result == {"processed": 2, "skipped_no_recipient": 0, "failed": 0}
    assert len(fake_ses.sent) == 2


def test_skips_malformed_record(monkeypatch):
    fake_s3 = FakeS3Client()
    fake_ses = FakeSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    result = handle_report_created_event({"Records": [{"s3": {}}]})

    assert result == {"processed": 0, "skipped_no_recipient": 0, "failed": 0}
    assert fake_ses.sent == []


def test_report_title_from_key_strips_uuid_suffix():
    title = report_delivery._report_title_from_key("reports/west-region-occupancy-abc123def456.pdf")
    assert title == "west region occupancy"


def test_report_title_from_key_falls_back_to_raw_filename_if_pattern_does_not_match():
    title = report_delivery._report_title_from_key("reports/some-other-shape.pdf")
    assert title == "some-other-shape.pdf"


def test_url_decodes_key(monkeypatch):
    """S3 event notifications URL-encode keys (e.g. spaces become '+')."""
    decoded = "reports/west region-abc123def456.pdf"
    fake_s3 = FakeS3Client(metadata={decoded: {"analyst-email": "analyst@example.com"}})
    fake_ses = FakeSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    handle_report_created_event(_s3_event(key="reports/west+region-abc123def456.pdf"))

    # both the head_object lookup and the presign use the DECODED key
    assert fake_s3.head_calls[0]["Key"] == decoded
    assert fake_s3.presign_calls[0]["Params"]["Key"] == decoded


class FailingSesClient:
    """Raises MessageRejected on the first send, succeeds on the rest --
    mirrors the SES-sandbox 'recipient not verified' case."""
    def __init__(self):
        self.sent = []
        self._first = True

    def send_email(self, **kwargs):
        if self._first:
            self._first = False
            from botocore.exceptions import ClientError
            raise ClientError(
                {"Error": {"Code": "MessageRejected", "Message": "Email address is not verified."}},
                "SendEmail",
            )
        self.sent.append(kwargs)


def test_send_failure_is_counted_and_does_not_abort_the_batch(monkeypatch):
    k1 = "reports/a-abc123def456.pdf"
    k2 = "reports/b-abc123def456.pdf"
    fake_s3 = FakeS3Client(metadata={
        k1: {"analyst-email": "unverified@example.com"},
        k2: {"analyst-email": "ok@example.com"},
    })
    fake_ses = FailingSesClient()
    _wire(monkeypatch, fake_s3, fake_ses)

    event = {"Records": [
        {"s3": {"bucket": {"name": "b"}, "object": {"key": k1}}},
        {"s3": {"bucket": {"name": "b"}, "object": {"key": k2}}},
    ]}
    result = handle_report_created_event(event)

    # first send failed, second succeeded -- the failure must NOT be
    # counted as processed, and must NOT stop the second report.
    assert result == {"processed": 1, "skipped_no_recipient": 0, "failed": 1}
    assert [s["Destination"]["ToAddresses"][0] for s in fake_ses.sent] == ["ok@example.com"]
