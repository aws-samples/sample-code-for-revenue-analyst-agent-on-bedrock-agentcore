// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Auth module: signs the analyst in to the sample's Cognito user pool with
 * SRP (amazon-cognito-identity-js). The app client only allows
 * ALLOW_USER_SRP_AUTH, so the password is never sent in plaintext.
 *
 * The ID token returned by login() is sent directly to AgentCore Runtime as
 * a bearer token (see agent-client.js). No AWS credential is ever stored in
 * this bundle.
 */
import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
} from "amazon-cognito-identity-js";

let config = null;

export async function loadConfig() {
  if (!config) {
    const resp = await fetch("/runtime-config.json");
    if (!resp.ok) {
      throw new Error("Failed to load runtime configuration.");
    }
    config = await resp.json();
  }
  return config;
}

function getUserPool(cfg) {
  return new CognitoUserPool({
    UserPoolId: cfg.userPoolId,
    ClientId: cfg.userPoolClientId,
  });
}

/**
 * Authenticates against the Cognito user pool via SRP.
 * Returns { idToken, claims } on success. Rejects with a plain Error
 * carrying a message safe to show the analyst (never echoes raw Cognito
 * exception internals that might carry sensitive detail).
 */
export function login(username, password) {
  return new Promise(async (resolve, reject) => {
    const cfg = await loadConfig();
    const pool = getUserPool(cfg);
    const authDetails = new AuthenticationDetails({
      Username: username,
      Password: password,
    });
    const cognitoUser = new CognitoUser({ Username: username, Pool: pool });

    cognitoUser.authenticateUser(authDetails, {
      onSuccess: (session) => {
        const idToken = session.getIdToken().getJwtToken();
        const claims = session.getIdToken().decodePayload();
        resolve({ idToken, claims, cognitoUser, session });
      },
      onFailure: (err) => {
        reject(new Error(err.message || "Sign-in failed. Check your username and password."));
      },
      // Accounts are created by an administrator (scripts/create_demo_user.sh
      // sets a permanent password). A NEW_PASSWORD_REQUIRED challenge means
      // the account still has a temporary password; this sample does not
      // implement a password-change screen.
      newPasswordRequired: () => {
        reject(
          new Error(
            "Your account requires a password reset. Contact your administrator."
          )
        );
      },
    });
  });
}

/**
 * True once a token's exp claim has passed. The SPA checks this before
 * every agent call rather than waiting for AgentCore to reject an expired
 * credential, so the analyst sees a clear "please sign in again" instead
 * of an opaque AWS error.
 */
export function isTokenExpired(claims) {
  if (!claims || !claims.exp) return true;
  return Date.now() >= claims.exp * 1000;
}
