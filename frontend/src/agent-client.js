// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Calls AgentCore Runtime's InvokeAgentRuntime with a plain HTTPS request,
 * not the AWS SDK. AWS's API reference states that when the agent uses
 * OAuth, InvokeAgentRuntime must be called over HTTPS rather than the SDK.
 *
 * Auth: the Runtime uses a CUSTOM_JWT authorizer (see
 * terraform/modules/agent/runtime.tf). The analyst's Cognito ID token is
 * sent as a standard OAuth2 bearer token in the Authorization header, and
 * the Runtime validates its issuer, signature, expiry, and audience before
 * the request reaches agent.py. The SPA holds no AWS credentials.
 */
import { loadConfig } from "./auth.js";

let sessionId = null;

function newSessionId() {
  // AgentCore Runtime requires runtimeSessionId to be at least 33
  // characters. Two UUID fragments give 44.
  return crypto.randomUUID() + crypto.randomUUID().slice(0, 8);
}

/**
 * Starts a fresh runtime session. Call once at login (and again after
 * sign-out/sign-in) so a new analyst's conversation doesn't inherit a
 * prior session's history.
 */
export function initClient() {
  sessionId = newSessionId();
}

export function resetSession() {
  sessionId = newSessionId();
}

/** The current conversation's session id (used to label/resume chats). */
export function getSessionId() {
  return sessionId;
}

/**
 * Resume an existing conversation: point the client at a known session id
 * so subsequent turns append to (and the agent's memory reloads) that
 * conversation. Used when the analyst clicks a past chat in the sidebar.
 */
export function setSessionId(id) {
  sessionId = id;
}

/**
 * Low-level POST to InvokeAgentRuntime with an arbitrary JSON body. Shared
 * by chat turns and the history read-actions -- same authenticated,
 * CUSTOM_JWT path, so history is fetched with the analyst's own bearer
 * token and the server scopes it to their actor id.
 */
async function postToRuntime(body, idToken, runtimeSessionId) {
  const cfg = await loadConfig();
  const encodedArn = encodeURIComponent(cfg.agentRuntimeArn);
  const url = `https://bedrock-agentcore.${cfg.region}.amazonaws.com/runtimes/${encodedArn}/invocations?qualifier=${cfg.agentRuntimeQualifier || "DEFAULT"}`;

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      Authorization: `Bearer ${idToken}`,
      "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": runtimeSessionId,
    },
    body: JSON.stringify(body),
  });

  const bodyText = await resp.text();
  if (!resp.ok) {
    throw new Error(`Agent call failed (HTTP ${resp.status}): ${bodyText.slice(0, 300)}`);
  }
  try {
    return JSON.parse(bodyText);
  } catch {
    return { status: "error", response: bodyText };
  }
}

/**
 * Sends one chat turn to the agent. Returns the parsed JSON response body
 * (see agent/agent.py's invoke() success shape).
 */
export async function invokeAgent(prompt, idToken) {
  if (!sessionId) {
    throw new Error("Agent client not initialized -- sign in first.");
  }
  return postToRuntime({ prompt }, idToken, sessionId);
}

/**
 * List the signed-in analyst's past conversations. The server derives the
 * actor id from the bearer token, so this only ever returns THIS user's
 * conversations. A session id header is still required by the runtime, so
 * we send the current one (the action ignores it server-side).
 */
export async function listSessions(idToken) {
  const result = await postToRuntime(
    { action: "list_sessions" },
    idToken,
    sessionId || newSessionId()
  );
  return Array.isArray(result.sessions) ? result.sessions : [];
}

/**
 * Load the messages of one past conversation. Server enforces that the
 * session belongs to the caller's own actor id.
 */
export async function getHistory(historySessionId, idToken) {
  const result = await postToRuntime(
    { action: "get_history", session_id: historySessionId },
    idToken,
    historySessionId
  );
  return Array.isArray(result.messages) ? result.messages : [];
}

/**
 * Delete one of the caller's conversations. Server enforces the session
 * belongs to the caller's own actor id (deletes all its events).
 */
export async function deleteSession(historySessionId, idToken) {
  const result = await postToRuntime(
    { action: "delete_session", session_id: historySessionId },
    idToken,
    sessionId || newSessionId()
  );
  return result && result.status === "success";
}
