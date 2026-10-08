# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Puts tools/ directly on sys.path so tests import `handler` and `lib.*` the
exact same way the deployed Lambda does.

Why this matters: terraform/modules/tools/lambda.tf zips the CONTENTS of
tools/ to the zip root (source_dir = tools/), so at runtime the package is
`lib`, not `tools.lib`, and the entry point is `handler.lambda_handler`, not
`tools.handler.lambda_handler`. If tests imported via `tools.lib.*` instead,
they'd pass while the real deployment failed with ModuleNotFoundError. Adding tools/ to sys.path makes the test import path
match production exactly.
"""
import os
import sys

TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "tools")
sys.path.insert(0, os.path.abspath(TOOLS_DIR))

import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from lib import token_verifier

# Test user pool settings. The tools Lambda reads these from its environment.
TEST_REGION = "us-east-1"
TEST_POOL_ID = "us-east-1_TESTPOOL"
TEST_CLIENT_ID = "test-spa-client"
TEST_ISSUER = token_verifier.issuer_for(TEST_REGION, TEST_POOL_ID)
TEST_KID = "test-key-1"

os.environ.setdefault("COGNITO_REGION", TEST_REGION)
os.environ.setdefault("COGNITO_USER_POOL_ID", TEST_POOL_ID)
os.environ.setdefault("COGNITO_APP_CLIENT_ID", TEST_CLIENT_ID)

# One RSA key pair stands in for the user pool's signing key. A second key
# signs "forged" tokens, which must be rejected.
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FORGING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJwksClient:
    """Stands in for jwt.PyJWKClient so tests never fetch a real JWKS. Like
    the real client, it only knows the user pool's key id."""

    def get_signing_key_from_jwt(self, token):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != TEST_KID:
            raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid}")
        return _FakeSigningKey(SIGNING_KEY.public_key())


token_verifier._jwks_client = lambda issuer: _FakeJwksClient()


# Claim names test tokens may carry (Cognito ID token shape).
TEST_CLAIM_NAMES = (
    "sub", "email", "iss", "aud", "token_use", "iat", "exp",
    "cognito:groups", "custom:region",
)


def make_claims(groups=None, region=None, email="analyst@example.com", sub="test-sub", **overrides):
    now = int(time.time())
    claims = {
        "sub": sub,
        "email": email,
        "iss": TEST_ISSUER,
        "aud": TEST_CLIENT_ID,
        "token_use": "id",  # nosec B105 - JWT token_use claim value, not a password
        "iat": now,
        "exp": now + 3600,
    }
    if groups is not None:
        claims["cognito:groups"] = groups
    if region is not None:
        claims["custom:region"] = region
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def sign_token(claims, key=None, kid=TEST_KID, algorithm="RS256"):
    # Signs synthetic test claims (fake sub, example.com email) with a key
    # generated at test time. Test-only: the sample never issues JWTs;
    # Cognito does. Only the known test claim names are copied into the
    # payload.
    payload = {name: claims[name] for name in TEST_CLAIM_NAMES if name in claims}
    return jwt.encode(payload, key or SIGNING_KEY, algorithm=algorithm, headers={"kid": kid})


def make_token(groups=None, region=None, email="analyst@example.com", sub="test-sub"):
    """A Cognito-style ID token signed with the test user pool key."""
    return sign_token(make_claims(groups=groups, region=region, email=email, sub=sub))
