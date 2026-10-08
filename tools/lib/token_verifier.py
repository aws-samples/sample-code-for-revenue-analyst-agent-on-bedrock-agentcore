# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Verifies the analyst's Cognito ID token before any claim is trusted.

The AgentCore Runtime's JWT authorizer already validates the token before the
agent runs, but this Lambda does not rely on that. Every downstream component
enforces the caller's identity on its own, so the tools stay safe even if
another principal is later allowed to call the Gateway or this Lambda.

A token is accepted only if all of these hold:
  - the signature verifies against a key in the user pool's JWKS (RS256 only),
  - `iss` is this user pool,
  - `aud` is the SPA app client,
  - `token_use` is "id",
  - `exp` is in the future (and `iat` is present).

Anything else, including a missing token or a missing configuration, returns
no claims, which the reporting tools treat as access denied (fail closed).

Configuration comes from the Lambda environment, set by Terraform:
  COGNITO_REGION, COGNITO_USER_POOL_ID, COGNITO_APP_CLIENT_ID
"""
import logging
import os

import jwt

logger = logging.getLogger(__name__)

# Clock skew tolerated when checking exp/iat, in seconds.
LEEWAY_SECONDS = 30

_jwks_clients: dict = {}


def _settings() -> "tuple[str, str, str] | None":
    region = os.environ.get("COGNITO_REGION", "")
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    client_id = os.environ.get("COGNITO_APP_CLIENT_ID", "")
    if not (region and pool_id and client_id):
        return None
    return region, pool_id, client_id


def issuer_for(region: str, pool_id: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"


def _jwks_client(issuer: str) -> "jwt.PyJWKClient":
    # One client per issuer, kept for the life of the Lambda execution
    # environment. PyJWKClient caches the fetched keys, so the JWKS is
    # downloaded once per cold start, not once per request.
    client = _jwks_clients.get(issuer)
    if client is None:
        client = jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True, timeout=5)
        _jwks_clients[issuer] = client
    return client


def verified_claims(token: "str | None") -> dict:
    """Return the token's claims if it passes every check, otherwise {}.

    Never raises and never logs the token itself.
    """
    if not token:
        return {}

    settings = _settings()
    if settings is None:
        logger.error("Caller identity check is not configured; denying access")
        return {}
    region, pool_id, client_id = settings
    issuer = issuer_for(region, pool_id)

    raw = token[7:] if token.startswith("Bearer ") else token
    try:
        signing_key = _jwks_client(issuer).get_signing_key_from_jwt(raw)
        claims = jwt.decode(
            raw,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "token_use"]},
        )
    except jwt.ExpiredSignatureError:
        logger.warning("Caller identity rejected: expired")
        return {}
    except jwt.PyJWTError as exc:
        logger.warning("Caller identity rejected: %s", type(exc).__name__)
        return {}
    except Exception:  # noqa: BLE001 - JWKS fetch or unexpected failure: fail closed
        logger.warning("Caller identity rejected: verification failed")
        return {}

    if claims.get("token_use") != "id":
        logger.warning("Caller identity rejected: token_use is not id")
        return {}
    return claims
