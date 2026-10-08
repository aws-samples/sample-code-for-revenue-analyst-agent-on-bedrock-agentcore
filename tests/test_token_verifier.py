# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Token verification: only a correctly signed, unexpired Cognito ID token for
this user pool and app client is trusted. Every other token yields no claims,
so the reporting tools deny access."""
import base64
import json
import time

import pytest

from conftest import FORGING_KEY, make_claims, make_token, sign_token
from lib import reporting_client
from lib.reporting_client import ACCESS_DENIED, ReportingError
from lib.token_verifier import verified_claims


def _unsigned(claims):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none', 'kid': 'test-key-1'})}.{b64(claims)}."


def test_valid_token_returns_claims():
    claims = verified_claims(make_token(groups=["RevenueManager"]))
    assert claims["sub"] == "test-sub"
    assert claims["cognito:groups"] == ["RevenueManager"]


def test_bearer_prefix_is_accepted():
    assert verified_claims("Bearer " + make_token(groups=["RevenueManager"]))["sub"] == "test-sub"


@pytest.mark.parametrize("token", [None, "", "not-a-jwt", "a.b.c"])
def test_missing_or_malformed_token_is_rejected(token):
    assert verified_claims(token) == {}


def test_unsigned_token_is_rejected():
    assert verified_claims(_unsigned(make_claims(groups=["RevenueManager"]))) == {}


def test_token_signed_with_another_key_is_rejected():
    forged = sign_token(make_claims(groups=["RevenueManager"]), key=FORGING_KEY)
    assert verified_claims(forged) == {}


def test_token_with_unknown_key_id_is_rejected():
    assert verified_claims(sign_token(make_claims(), kid="other-kid")) == {}


def test_hs256_token_is_rejected():
    token = sign_token(make_claims(), key="shared-secret-that-is-at-least-32-bytes!", algorithm="HS256")
    assert verified_claims(token) == {}


def test_expired_token_is_rejected():
    past = int(time.time()) - 7200
    assert verified_claims(sign_token(make_claims(iat=past, exp=past + 3600))) == {}


def test_wrong_issuer_is_rejected():
    other = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER"
    assert verified_claims(sign_token(make_claims(iss=other))) == {}


def test_wrong_audience_is_rejected():
    assert verified_claims(sign_token(make_claims(aud="some-other-client"))) == {}


def test_access_token_is_rejected():
    access = make_claims(token_use="access")  # nosec B106 - JWT token_use claim value, not a password
    assert verified_claims(sign_token(access)) == {}


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "token_use", "aud", "iss"])
def test_token_missing_required_claim_is_rejected(claim):
    assert verified_claims(sign_token(make_claims(**{claim: None}))) == {}


@pytest.mark.parametrize("var", ["COGNITO_REGION", "COGNITO_USER_POOL_ID", "COGNITO_APP_CLIENT_ID"])
def test_missing_configuration_fails_closed(monkeypatch, var):
    monkeypatch.delenv(var)
    assert verified_claims(make_token(groups=["RevenueManager"])) == {}


def test_forged_chain_level_token_cannot_read_reporting_data():
    forged = sign_token(make_claims(groups=["RevenueManager"]), key=FORGING_KEY)
    with pytest.raises(ReportingError, match=ACCESS_DENIED):
        reporting_client.list_properties(caller_token=forged)


def test_unsigned_token_cannot_claim_another_analysts_sub():
    assert reporting_client.caller_sub(_unsigned(make_claims(sub="someone-else"))) is None
