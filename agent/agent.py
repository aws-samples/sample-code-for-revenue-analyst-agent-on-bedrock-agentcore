# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Revenue / Occupancy Analyst Agent: a Strands agent hosted on AgentCore Runtime.

The agent reaches its data tools through AgentCore Gateway, which exposes the
tools Lambda as MCP tools. The Gateway's inbound authorizer is AWS_IAM: this
Runtime is its only caller and signs requests with its execution role via
mcp_proxy_for_aws.aws_iam_streamablehttp_client (aws_service =
"bedrock-agentcore").

The system prompt requires every figure the agent states to come from a tool
call. The agent must never invent numbers.
"""
import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent, tool
from strands.hooks import (
    AfterToolsEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from strands.types.exceptions import MaxTokensReachedException

app = BedrockAgentCoreApp()

logger = logging.getLogger()
logger.setLevel(logging.INFO)
# The root logger has no handler by default -- Python's logging module falls
# back to a WARNING-level "handler of last resort" on stderr, which silently
# swallows every INFO/DEBUG call made against this logger (e.g. the RBAC
# header-visibility log below) even though setLevel(INFO) looks correct.
# Attach an explicit stdout handler so INFO logs actually reach CloudWatch.
if not logger.handlers:
    _stdout_handler = logging.StreamHandler()
    _stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(_stdout_handler)

# Multi-model strategy: Haiku routes, Sonnet reasons, Opus handles heavy
# analysis, so a routing decision never pays Opus prices.
# Pinned, versioned inference profile IDs -- not un-dated aliases that could
# shift silently under us.
#   Haiku 4.5  -> cheap/fast complexity classification (routing decision only)
#   Sonnet 4.6 -> general-purpose reasoning / baseline for the main agent
#   Opus 4.8   -> heavy lifting: complex, multi-step driver analysis
HAIKU_MODEL_ID = os.environ.get(
    "AGENT_HAIKU_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
SONNET_MODEL_ID = os.environ.get(
    "AGENT_SONNET_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
OPUS_MODEL_ID = os.environ.get("AGENT_OPUS_MODEL_ID", "us.anthropic.claude-opus-4-8")

# Max output tokens for the main (answering) model. The SDK's default output
# cap was low enough that long turns -- especially report generation, which
# chains a reporting tool -> code_interpreter -> generate_report and emits a
# lot of text -- hit MaxTokensReachedException. That both failed the turn AND
# left a partial/corrupted assistant message in AgentCore Memory, which then
# derailed subsequent turns in the same session (surfacing as a spurious
# "This request can't be processed" reply). Set an explicit high ceiling to
# give report turns headroom. Overridable via env for tuning without a code
# change. Kept within Claude 4.x per-request output limits.
MAX_OUTPUT_TOKENS = int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "32000"))


def _friendly_model_label(model_id: str) -> str:
    """Map a raw Bedrock model/inference-profile id to a short human label
    for the UI (demo transparency: shows which Claude tier answered)."""
    mid = (model_id or "").lower()
    if "haiku" in mid:
        return "Claude Haiku"
    if "sonnet" in mid:
        return "Claude Sonnet"
    if "opus" in mid:
        return "Claude Opus"
    return "Claude"


# Bedrock Guardrail (R3) -- applied to every model invocation (routing call
# and main reasoning call): prompt-injection defense, PII masking, scope
# enforcement. See terraform/modules/agent/guardrail.tf for the policy.
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION")

# AgentCore Memory. Short-term: full conversation history within a
# session (active region, date window, disambiguation choices), handled
# automatically by AgentCoreMemorySessionManager -- no extra code needed
# beyond passing session_manager= to Agent(). Long-term: durable analyst
# findings extracted by the SEMANTIC strategy (see
# terraform/modules/agent/memory.tf), retrievable across sessions for the
# same actor.
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")

# AWS region for the AgentCore Memory read clients (chat-history feature).
# Matches the region the runtime/memory are deployed in.
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Fallback memory actor ID, used only when no caller identity is available
# (for example, a malformed token). The reporting tools deny such calls, so
# this only affects which memory namespace is used, not data access.
DEFAULT_ACTOR_ID = "anonymous"

# Matches memory.tf's aws_bedrockagentcore_memory_strategy.semantic
# namespace_templates -- must stay in sync with that Terraform resource.
SEMANTIC_NAMESPACE_TEMPLATE = "/semantic/{actorId}"

# Tools that receive the caller's token so the Lambda can scope results to
# the analyst's role and region (see tools/lib/reporting_client.py), and
# bind recommendation records to the analyst who proposed them (see
# tools/lib/recommendation_client.py). code_interpreter and generate_report
# run locally in this Runtime, so they never get it.
REPORTING_TOOL_NAMES = frozenset({
    "get_range_metrics",
    "get_occupancy",
    "list_properties",
    "query_analytics",
    "propose_recommendation",
    "record_recommendation",
})

# Demo transparency: maps each data-fetching tool to the data source it hits,
# so the UI can badge where an answer's data came from (mirrors the model
# badge). Determined by which tool the agent actually called -- observed via
# _ToolSourceTrackerHook, never self-reported by the model.
#   query_analytics -> Athena event lake (see tools/handler.py)
#   get_range_metrics / get_occupancy / list_properties -> reporting API
TOOL_DATA_SOURCE = {
    "query_analytics": "Athena",
    "get_range_metrics": "Reporting API",
    "get_occupancy": "Reporting API",
    "list_properties": "Reporting API",
}

_PDF_MAGIC = b"%PDF-"
MAX_PDF_BYTES = 10 * 1024 * 1024  # 10MB -- generous for a text+chart report, bounds cost/abuse


def _decode_jwt_claims(token: str) -> "dict | None":
    """Decode (NOT verify) a JWT's claims. Safe to skip signature
    verification here per AWS's own documented pattern (see
    docs.aws.amazon.com/.../runtime-oauth.html step 7.1): AgentCore
    Runtime's own inbound authorizer already validated the token's
    issuer/signature/expiry before this invocation was ever accepted --
    re-verifying here would be redundant, not an extra safety margin.
    Returns None (fails open to no-claims, not a crash) if the token is
    malformed.
    """
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        logger.exception("Failed to decode caller JWT claims")
        return None


def _extract_caller_context(context) -> "tuple[str | None, dict | None]":
    """Return the analyst's ID token and its decoded claims from the
    Authorization header.

    The Runtime's CUSTOM_JWT authorizer (see runtime.tf) already verified
    this token's issuer, signature, expiry, and audience before this
    invocation was accepted, and runtime.tf allowlists the header so it
    reaches request_headers.

    Returns (None, None) if the header is absent. In that case the
    reporting tools deny access and memory falls back to DEFAULT_ACTOR_ID.
    """
    headers = getattr(context, "request_headers", None) or {}
    # AgentCore Runtime delivers request_headers with lowercased keys, so
    # normalize before lookup.
    lower_headers = {k.lower(): v for k, v in headers.items()}
    raw = lower_headers.get("authorization")
    if not raw:
        return None, None

    token = raw[7:] if raw.startswith("Bearer ") else raw
    claims = _decode_jwt_claims(token)
    return token, claims


def _create_caller_token_hook(caller_token: "str | None") -> "HookProvider | None":
    """A Strands hook that injects the analyst's token into every
    reporting tool call as a hidden `_callerToken` argument.

    The hook overwrites `_callerToken` on every matching call using only
    the token extracted from the validated inbound request. Nothing the
    model writes in its tool arguments can change which identity is used,
    even under prompt injection.

    Returns None if there is no caller token; the reporting tools then
    deny access.
    """
    if not caller_token:
        return None

    class _CallerTokenHook(HookProvider):
        def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
            registry.add_callback(BeforeToolCallEvent, self._inject)

        def _inject(self, event: BeforeToolCallEvent) -> None:
            tool_name = event.tool_use.get("name", "")
            # Gateway/MCP tool names carry the "{target}___{tool}" prefix
            # (see tools/handler.py's TOOL_NAME_DELIMITER) -- match on the
            # suffix so this works regardless of prefixing.
            if not any(tool_name.endswith(name) for name in REPORTING_TOOL_NAMES):
                return
            tool_input = event.tool_use.setdefault("input", {})
            tool_input["_callerToken"] = caller_token

    return _CallerTokenHook()


class _ToolSourceTrackerHook(HookProvider):
    """Records which data sources were hit during a turn, by observing the
    tool calls the agent actually makes (BeforeToolCallEvent). Reliable and
    deterministic -- the source is derived from the tool name, not from
    asking the model (which could hallucinate, cf. the {EMAIL} bug). Read
    `sources` after the turn to badge the answer's provenance in the UI.
    """

    def __init__(self):
        self.sources = []  # preserves first-seen order

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self._track)

    def _track(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "")
        for name, source in TOOL_DATA_SOURCE.items():
            # Gateway/MCP tool names carry a "{target}___{tool}" prefix, so
            # match on the suffix (same approach as _CallerTokenHook).
            if tool_name.endswith(name) and source not in self.sources:
                self.sources.append(source)


# The exact JSON error the tools Lambda returns when reporting_client
# denies access ({"error": ACCESS_DENIED}). Matched verbatim because it is a
# fixed, code-controlled string.
_ACCESS_DENIED_ERROR = '{"error":"reporting_access_denied"}'

# When a denial comes back as normal tool-result text, the model explains it
# in its own words, and that explanation tends to false-positive against the
# Guardrail's out-of-scope DENY topic (see guardrail.tf). The Guardrail then
# replaces the answer with a generic refusal. To avoid that, this hook ends
# the turn with a fixed, code-authored message as soon as any reporting tool
# returns a denial, which skips the model call (and the Guardrail) for that
# response.
ACCESS_DENIED_MESSAGE = (
    "Your account role does not have permission to view this report. "
    "If your role is scoped to a region, ask about properties in that "
    "region, or contact your administrator for broader reporting access."
)


class _AccessDeniedHook(HookProvider):
    """Ends the turn with ACCESS_DENIED_MESSAGE if any tool result in
    this batch was an access denial. See the comment above for why.
    """

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(AfterToolsEvent, self._check)

    def _check(self, event: AfterToolsEvent) -> None:
        for content_block in event.message.get("content", []):
            tool_result = content_block.get("toolResult")
            if not tool_result:
                continue
            for item in tool_result.get("content", []):
                if item.get("text") == _ACCESS_DENIED_ERROR:
                    event.end_turn = ACCESS_DENIED_MESSAGE
                    return


def _sanitize_filename_component(value: str) -> str:
    """Reduces a model-supplied title/name to a safe S3 key component --
    alphanumerics, hyphens, underscores only. Prevents path traversal
    (../) or accidental prefix escapes from ever reaching the S3 key.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip())
    cleaned = cleaned.strip("-")
    return cleaned[:80] if cleaned else "report"


def _create_code_interpreter_tools(session_id: str, analyst_email: "str | None" = None):
    """Build the code_interpreter and generate_report tools, both bound to
    ONE bedrock_agentcore CodeInterpreter client created per invoke() call.

    Sharing one client by closure guarantees both tools use the same
    sandbox session, so generate_report always finds the file that
    code_interpreter just wrote. The code_interpreter tool deliberately
    takes no session name argument: if the model could pick a session, a
    mismatched name would silently start a new, empty sandbox.

    The session starts lazily on the first call to either tool, so a turn
    that never needs the sandbox never pays for one. session_id only names
    the AWS-side session for observability.
    """
    # AgentCore Runtime, like Lambda, sets AWS_REGION automatically in the
    # execution environment -- no need for a separate Terraform-managed env
    # var (and AWS_REGION is a reserved name on Lambda-like runtimes, so
    # explicitly setting it via Terraform can be rejected at deploy time).
    interpreter = CodeInterpreter(region=os.environ.get("AWS_REGION", "us-east-1"))
    started = False

    def _ensure_started() -> None:
        nonlocal started
        if not started:
            interpreter.start(name=f"session-{session_id}"[:63])
            started = True

    @tool
    def code_interpreter(code: str, language: str = "python", clear_context: bool = False) -> dict:
        """Execute Python (or JavaScript/TypeScript) code in a secure,
        managed sandbox. Use this for ANY calculation beyond restating a
        single number a reporting tool already returned: deltas, sums,
        averages, contribution shares, correlations, outlier detection.
        Also use it to render report artifacts (matplotlib charts,
        reportlab/fpdf PDFs) as FILES for generate_report to pick up
        afterward -- files written here persist for the rest of this
        conversation turn and are visible to generate_report.

        Args:
            code: The code to execute.
            language: "python" (default), "javascript", or "typescript".
            clear_context: If True, clears previously defined variables
                before running (Python only). Default False (state
                persists across calls within this turn).

        Returns:
            Dict with "stdout", "stderr", and "exitCode" from execution.
        """
        _ensure_started()
        try:
            result = interpreter.execute_code(code, language=language, clear_context=clear_context)
        except Exception as exc:
            logger.exception("code_interpreter execution failed")
            return {"stdout": "", "stderr": f"execution_failed: {type(exc).__name__}: {exc}", "exitCode": 1}

        return _extract_execution_result(result)

    @tool
    def generate_report(file_path: str, title: str = "report") -> dict:
        """Persist a PDF report you already rendered inside the
        code_interpreter sandbox to durable storage, and notify the
        analyst by email with a short-lived download link.

        The PDF must already exist as a FILE inside the code_interpreter
        sandbox (e.g. written via reportlab/fpdf's own save/output call,
        with any chart rendered via matplotlib.savefig into the SAME
        working directory first). Pass the RELATIVE path to that file --
        do not read the file yourself or pass its bytes; this tool reads
        it directly from the sandbox.

        Args:
            file_path: Relative path to the PDF file inside the sandbox,
                e.g. "west_region_summary.pdf".
            title: Short, filesystem-safe title for the report, used to
                name the saved file, e.g. "west-region-occupancy-summary".

        Returns:
            On success: {"status": "success", "bucket": ..., "key": ...}
            On failure: {"status": "error", "error": "<reason>"}
        """
        _ensure_started()
        try:
            pdf_bytes = interpreter.download_file(file_path)
        except FileNotFoundError:
            return {"status": "error", "error": f"file_not_found_in_sandbox: {file_path}"}
        except Exception as exc:
            logger.exception("Failed to read %s from sandbox", file_path)
            return {"status": "error", "error": f"sandbox_read_failed: {type(exc).__name__}"}

        if isinstance(pdf_bytes, str):
            # download_file() decodes to str when it can (text files); a
            # real PDF is binary and should have come back as bytes. If it
            # didn't, it's not a valid PDF -- treat as a validation
            # failure, not an encoding workaround.
            return {"status": "error", "error": "content_is_not_a_valid_pdf"}

        if len(pdf_bytes) > MAX_PDF_BYTES:
            return {
                "status": "error",
                "error": f"report_too_large: {len(pdf_bytes)} bytes, maximum is {MAX_PDF_BYTES}",
            }

        if not pdf_bytes.startswith(_PDF_MAGIC):
            return {"status": "error", "error": "content_is_not_a_valid_pdf"}

        bucket = os.environ.get("ARTIFACTS_BUCKET_NAME")
        if not bucket:
            return {"status": "error", "error": "artifacts_bucket_not_configured"}

        key = f"reports/{_sanitize_filename_component(title)}-{uuid.uuid4().hex[:12]}.pdf"

        # Stamp the authenticated analyst's own email onto the S3 object as
        # metadata. This is the ONLY channel by which the report's recipient
        # reaches the delivery Lambda: that Lambda is triggered by an S3
        # ObjectCreated event, which carries only bucket/key -- the caller's
        # identity (known here, from their validated token claims) is
        # otherwise lost at the event boundary. The delivery Lambda reads
        # this metadata back via head_object and sends the pre-signed link
        # to exactly this person via SES (see tools/lib/report_delivery.py).
        # If we have no verified email (e.g. a token without an email claim),
        # we omit the metadata and the delivery Lambda falls back to the
        # configured default recipient rather than dropping the report.
        metadata = {}
        if analyst_email:
            metadata["analyst-email"] = analyst_email

        try:
            boto3.client("s3").put_object(
                Bucket=bucket,
                Key=key,
                Body=pdf_bytes,
                ContentType="application/pdf",
                ServerSideEncryption="AES256",
                Metadata=metadata,
            )
        except Exception as exc:
            logger.exception("Failed to write report to S3")
            return {"status": "error", "error": f"report_write_failed: {type(exc).__name__}"}

        logger.info("Report written: s3://%s/%s (%d bytes)", bucket, key, len(pdf_bytes))
        # Return the resolved recipient so the model can state delivery
        # accurately instead of inventing a placeholder. "the email on file"
        # keeps us from asserting an address we don't actually have when the
        # token carried no email claim (delivery then uses the default).
        recipient = analyst_email if analyst_email else "the email address on file"
        return {
            "status": "success",
            "bucket": bucket,
            "key": key,
            "sizeBytes": len(pdf_bytes),
            "delivered_to": recipient,
        }

    return code_interpreter, generate_report


def _extract_execution_result(raw_result: dict) -> dict:
    """The raw bedrock_agentcore CodeInterpreter.execute_code() response is
    an event stream wrapper (see bedrock_agentcore.tools.code_interpreter_client
    -- CodeInterpreter.invoke() returns the boto3 invoke_code_interpreter
    response as-is). Flatten it to the simple {stdout, stderr, exitCode}
    shape the model actually needs, rather than exposing the raw stream
    structure as a tool result.
    """
    if "stream" in raw_result:
        for event in raw_result["stream"]:
            if "result" in event:
                structured = event["result"].get("structuredContent") or {}
                if structured:
                    return {
                        "stdout": structured.get("stdout", ""),
                        "stderr": structured.get("stderr", ""),
                        "exitCode": structured.get("exitCode", 0),
                    }
                # Fall back to the plain text content block if there's no
                # structuredContent (e.g. some tool invocations only return
                # content items).
                texts = [
                    item.get("text", "")
                    for item in event["result"].get("content", [])
                    if item.get("type") == "text"
                ]
                return {"stdout": "\n".join(texts), "stderr": "", "exitCode": 0}
    return {"stdout": "", "stderr": "unexpected_response_shape", "exitCode": 1}


def _resolve_actor_id(claims: "dict | None") -> str:
    """The analyst's own `sub` claim, so each user gets their own
    long-term memory namespace and chat history. Falls back to
    DEFAULT_ACTOR_ID only when there are no claims at all.
    """
    if claims and claims.get("sub"):
        return claims["sub"]
    return DEFAULT_ACTOR_ID


def _create_session_manager(session_id: str, actor_id: str) -> "AgentCoreMemorySessionManager | None":
    """Returns None (no memory) if AGENTCORE_MEMORY_ID isn't configured --
    fails open, same policy as _guardrail_kwargs(), rather than crashing the
    agent when running without the Terraform-provisioned environment.
    """
    if not AGENTCORE_MEMORY_ID:
        return None

    namespace = SEMANTIC_NAMESPACE_TEMPLATE.replace("{actorId}", actor_id)
    config = AgentCoreMemoryConfig(
        memory_id=AGENTCORE_MEMORY_ID,
        session_id=session_id,
        actor_id=actor_id,
        retrieval_config={
            namespace: RetrievalConfig(top_k=5, relevance_score=0.5)
        },
    )
    return AgentCoreMemorySessionManager(config)


def _list_user_sessions(actor_id: str) -> list:
    """Return the signed-in user's past conversations, newest first.

    Actor-scoped by design: it can ONLY ever list sessions for the
    actor_id we derived from the caller's own verified token (see
    invoke()). There is no code path that accepts an actor_id from the
    client, so one analyst can never enumerate another's conversations.
    Each entry carries a short preview (the first user message) so the UI
    can label the conversation without a second round trip.
    """
    if not AGENTCORE_MEMORY_ID:
        return []
    from bedrock_agentcore.memory import MemorySessionManager

    session_mgr = MemorySessionManager(
        memory_id=AGENTCORE_MEMORY_ID, region_name=REGION
    )
    event_client = MemoryClient(region_name=REGION)
    summaries = session_mgr.list_actor_sessions(actor_id)
    sessions = []
    for s in summaries:
        sid = s.get("sessionId")
        if not sid:
            continue
        preview, started_at = _session_preview(event_client, actor_id, sid)
        # Skip sessions with no real user message -- empty/system-only or
        # guardrail-redacted sessions would otherwise show as a wall of
        # "(no messages)" rows in the sidebar. Only list conversations the
        # analyst actually started.
        if not preview or preview in ("(no messages)", ""):
            continue
        sessions.append(
            {"sessionId": sid, "preview": preview, "startedAt": started_at}
        )
    # newest first
    sessions.sort(key=lambda x: x.get("startedAt") or "", reverse=True)
    return sessions


def _session_preview(client, actor_id: str, session_id: str):
    """First real user message + timestamp of a session, for the sidebar
    label. Uses list_events (same payload shape _extract_role_text parses)
    and returns the FIRST plain-text user turn. Returns ("", None) when the
    session has no real user message (session-metadata-only, tool-only, or
    internal-state events) so the caller filters it out of the sidebar."""
    try:
        events = client.list_events(
            memory_id=AGENTCORE_MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            max_results=30,
            include_payload=True,
        )
    except Exception:
        return "", None
    # events are chronological; find the first user prose turn
    for ev in events:
        for item in ev.get("payload", []) or []:
            role, text = _extract_role_text(item)
            if role == "user" and text:
                ts = ev.get("eventTimestamp")
                # eventTimestamp comes back as a datetime, which is NOT
                # JSON-serializable -- returning it raw makes the runtime
                # fall back to Python repr for the whole response, breaking
                # JSON.parse on the client. Always stringify to ISO.
                ts = ts.isoformat() if hasattr(ts, "isoformat") else (str(ts) if ts else None)
                return (text[:80], ts)
    return "", None


def _get_session_history(actor_id: str, session_id: str) -> list:
    """Return the full message list for ONE of the caller's conversations,
    oldest first, as [{role, text}]. actor_id is always the caller's own
    (server-derived) -- so this can only read the caller's own history."""
    if not AGENTCORE_MEMORY_ID:
        return []
    client = MemoryClient(region_name=REGION)
    events = client.list_events(
        memory_id=AGENTCORE_MEMORY_ID,
        actor_id=actor_id,
        session_id=session_id,
        max_results=100,
        include_payload=True,
    )
    # list_events returns NEWEST-first; sort chronologically (oldest first)
    # so the conversation reads top-to-bottom in the order it happened
    # (question then answer), not reversed.
    def _ts(ev):
        t = ev.get("eventTimestamp")
        return t.isoformat() if hasattr(t, "isoformat") else str(t or "")
    events = sorted(events, key=_ts)
    messages = []
    for ev in events:
        for msg in ev.get("payload", []) or []:
            role, text = _extract_role_text(msg)
            if role and text:
                messages.append({"role": role, "text": text})
    return messages


def _delete_session(actor_id: str, session_id: str) -> int:
    """Delete a conversation by removing all of its events. There is no
    single 'delete session' API, so we page through the session's events
    and delete each one. Scoped to the caller's own actor_id, so a caller
    can only ever delete their own conversation. Returns the count deleted.
    """
    if not AGENTCORE_MEMORY_ID:
        return 0
    from bedrock_agentcore.memory import MemorySessionManager

    session_mgr = MemorySessionManager(
        memory_id=AGENTCORE_MEMORY_ID, region_name=REGION
    )
    client = MemoryClient(region_name=REGION)
    deleted = 0
    # Loop until the session has no events left (delete in batches).
    while True:
        events = client.list_events(
            memory_id=AGENTCORE_MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            max_results=100,
            include_payload=False,
        )
        if not events:
            break
        for ev in events:
            event_id = ev.get("eventId")
            if not event_id:
                continue
            try:
                session_mgr.delete_event(
                    actor_id=actor_id, session_id=session_id, event_id=event_id
                )
                deleted += 1
            except Exception:
                logger.exception("delete_event failed for one event")
        # Safety: if a page returned events but none had deletable ids, stop
        # rather than loop forever.
        if all(not e.get("eventId") for e in events):
            break
    return deleted


def _extract_role_text(msg: dict):
    """Normalize an AgentCore Memory conversational event into (role, text).

    The live payload shape (verified against the deployed memory) is:
        {"conversational": {"content": {"text": "<INNER_JSON>"}}}
    where INNER_JSON is itself a JSON string:
        {"message": {"role": "user"|"assistant",
                     "content": [{"text": "..."}]  # or toolUse/toolResult
        }}
    Roles map to the SPA's vocabulary: user -> 'user', assistant -> 'agent'.
    Returns (None, None) for anything that isn't a plain-text conversational
    turn -- tool-use/tool-result content, Strands internal-state blobs, or
    session-metadata events -- so those are skipped for previews/history.
    """
    if not isinstance(msg, dict):
        return None, None

    conv = msg.get("conversational")
    if not isinstance(conv, dict):
        return None, None
    content = conv.get("content")
    inner_text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(inner_text, str) or not inner_text.strip():
        return None, None

    # content.text is a JSON string wrapping the real message.
    try:
        parsed = json.loads(inner_text)
    except (ValueError, TypeError):
        # Not JSON -- treat the raw string as the message text, role unknown.
        return None, None

    message = parsed.get("message") if isinstance(parsed, dict) else None
    if not isinstance(message, dict):
        return None, None

    raw_role = (message.get("role") or "").lower()
    if raw_role in ("user", "human"):
        role = "user"
    elif raw_role in ("assistant", "agent"):
        role = "agent"
    else:
        return None, None

    # message.content is a list of blocks; keep only PLAIN TEXT blocks,
    # skipping toolUse / toolResult (those aren't things the analyst typed
    # or the agent said in prose).
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return None, None
    texts = []
    for b in blocks:
        if isinstance(b, dict) and "text" in b and "toolUse" not in b and "toolResult" not in b:
            t = b.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t.strip())
    if not texts:
        return None, None
    return role, "\n".join(texts).strip()


def _msg_timestamp(msg: dict):
    for key in ("timestamp", "eventTimestamp", "createdAt"):
        if isinstance(msg, dict) and msg.get(key):
            return str(msg[key])
    return None


def _guardrail_kwargs() -> dict:
    """Guardrail config to merge into every BedrockModel(...) call. Empty
    dict if the guardrail isn't configured (e.g. local testing without the
    Terraform-provisioned environment variables) -- fails open to "no
    guardrail" rather than crashing the agent.
    """
    if not GUARDRAIL_ID or not GUARDRAIL_VERSION:
        return {}
    return {
        "guardrail_id": GUARDRAIL_ID,
        "guardrail_version": GUARDRAIL_VERSION,
        "guardrail_trace": "enabled",
    }

_ROUTER_SYSTEM_PROMPT = """You classify revenue/occupancy analyst questions \
by reasoning complexity. Respond with EXACTLY one word: either "simple" or \
"complex".

"simple" = a single, direct lookup (one metric, one region/property, one \
date range) that a single reporting tool call can answer.

"complex" = requires comparing multiple time periods, ranking/contribution \
analysis across properties, correlating drivers (e.g. cancellations vs \
booking source), or multi-step investigation of "why" something happened.

Respond with only the one word, nothing else."""

SYSTEM_PROMPT = """You are the Revenue / Occupancy Analyst Agent for a hotel \
chain's revenue management team.

You answer questions about booking, occupancy, and revenue performance using \
ONLY the tools available to you. You have no knowledge of the hotel chain's \
actual data beyond what the tools return in the current conversation.

TODAY'S DATE:
Today is {current_date}. Resolve every relative date expression ("last \
month", "this month", "last quarter", "year to date", "recent", "latest") \
against THIS date -- never against any date you assume from prior knowledge. \
When you call a reporting tool, translate the analyst's phrasing into explicit \
startDate/endDate values derived from today's date above. If a query returns \
zero rows for a period, treat it as a possibly-empty window first: state that \
the period returned no data and offer to check an adjacent period -- do NOT \
conclude a data-pipeline or ingestion failure unless multiple distinct \
periods all return zero.

CRITICAL RULES:
1. Every number you state (occupancy %, revenue, room counts, dates, region \
names) MUST come directly from a tool call's result. Never estimate, round \
imprecisely, or invent a figure you have not just retrieved. Every question \
about a specific metric, period, region, or property REQUIRES a fresh tool \
call in THIS turn to fetch the current figure -- even if you believe you \
answered a similar question before or "remember" the value from earlier in \
this or a prior conversation. NEVER answer a data question from memory or \
recalled context, and NEVER say things like "based on prior data already \
retrieved", "as retrieved earlier", or "from our previous conversation". \
Memory is for continuity of discussion, NOT a substitute for looking the \
number up again -- always call the tool and report the freshly retrieved \
result.
2. If a tool call fails or returns an error, say so plainly. Do not guess \
what the answer "probably" would have been.
3. If a question requires data you don't have a tool for, say what you can \
and cannot answer, rather than fabricating supporting detail.
4. Region names come from the chain's own taxonomy: Northeast, Southeast, \
Midwest, West, South, Other. If an analyst asks about a US state or city, \
map it to the correct region using the tool results, and say which region \
you mapped it to.
CHOOSING THE OCCUPANCY/REVENUE TOOL (important -- pick correctly): \
get_range_metrics returns TRUE per-period aggregates (occupancy, revenue, \
room nights) for a date range and is the CORRECT tool for any question \
about a specific month, quarter, or historical period (e.g. "occupancy in \
July 2026", "June revenue", "how did August do"). get_occupancy returns a \
point-in-time SNAPSHOT of currently-occupied rooms and does NOT vary by the \
period you pass -- use it ONLY when the analyst explicitly wants the current \
live occupancy "right now" or needs the per-property / per-region breakdown \
for the current snapshot. For monthly or historical occupancy figures ALWAYS \
use get_range_metrics so the numbers actually reflect that period; never use \
get_occupancy for a past month (it will return the same snapshot for every \
month, which is wrong). If get_range_metrics does not return an occupancy \
value directly, compute it from its room-nights / available-room fields with \
the code_interpreter -- do not fall back to the snapshot endpoint.
5. Cite which tool(s) you used to support your answer when it is not obvious \
from the conversation.
6. For "why" / driver questions (e.g. what's behind a trend), use \
query_analytics against the event lake rather than speculating. It only \
supports a fixed set of named templates over specific partitions -- if the \
data needed isn't covered by an existing template or partition, say so \
rather than guessing at an answer. The event lake holds only recent history \
(typically the last 45 days), so older months can return zero rows; say so \
rather than treating it as a data failure. Bookings and cancellations, with \
their booking channel, are in the crs partition.
7. NEVER perform arithmetic in your head -- not a percentage, a delta, a sum, \
an average, nothing. Any calculation beyond restating a single number a \
tool already returned (week-over-week deltas, each property's share of a \
regional change, correlating cancellations with booking source, lead-time \
distributions, outlier detection) MUST be done by writing and executing \
Python in the code_interpreter tool. State the code's actual computed \
result, not your own mental estimate of what it would be.
8. To produce a downloadable report, render a PDF FILE INSIDE the \
code_interpreter sandbox (matplotlib for any chart, reportlab or fpdf for \
the document layout, saved to a relative path there) then call \
generate_report with that file PATH -- do not read the file's bytes \
yourself or pass file contents as an argument to any tool; generate_report \
reads the sandbox file directly. LAYOUT: build the document as a normal \
top-to-bottom FLOW, not with hand-placed absolute coordinates -- prefer \
reportlab's Platypus (SimpleDocTemplate + a list of flowables: Paragraph, \
Spacer, Table, Image) so elements never overlap. The title and the \
date/subtitle MUST be on SEPARATE lines with vertical space between them \
(e.g. Paragraph(title) then Spacer then Paragraph(subtitle)); never draw \
the subtitle at a y-position that can collide with the title, and never \
overlay two text elements. Keep a comfortable top margin and let long \
titles wrap. Never claim a report was delivered unless \
generate_report actually returned status "success" -- the analyst is \
notified automatically by email once it does, so you do not need to \
describe a manual delivery step. When it succeeds, tell the analyst the \
report will be emailed to them shortly -- keep it brief and natural (e.g. \
"The report has been generated and will be emailed to you shortly."). Do \
NOT write out a specific email address, and NEVER emit a placeholder token \
like "{{EMAIL}}" or invent/guess an address; if you mention the destination \
at all, say "the email address on file" or use the exact "delivered_to" \
value the tool returned, nothing else.
9. If you want to recommend a concrete action (a rate change, a promotion, \
etc.), this is a TWO-STEP process and you must never skip or reorder it: \
first call propose_recommendation and present its summary/reasoning to the \
analyst as a proposal, clearly asking for their explicit confirmation. Do \
NOT call record_recommendation in the same turn you propose something. \
Only after the analyst has explicitly said yes/confirmed/approved IN THE \
CONVERSATION -- a later message, not something you infer or assume -- call \
record_recommendation with that exact recommendationId and confirmed=true. \
If the analyst says no, changes their mind, or doesn't clearly confirm, do \
NOT call record_recommendation at all. Never claim a recommendation was \
recorded/finalized unless record_recommendation actually returned status \
"CONFIRMED".
10. NEVER expose internal schema identifiers or raw system values in your \
answer. This includes event-type codes (e.g. "checkinout.checked_in", \
"checkinout.checked_out"), partition or source names (e.g. "pms", "billing", \
"source_partition", "detail_type"), template names, tool argument names, \
column names, or any dotted/underscored machine identifier. These are \
internal plumbing. Translate them into plain business language for the \
revenue manager -- for example, say "check-ins" and "check-outs" instead of \
"checkinout.checked_in / checkinout.checked_out", and "the property \
management system" instead of "the pms partition". Present only the \
business-meaningful result (properties, counts, occupancy, revenue, regions, \
dates); do not narrate which internal event types or partitions the data \
came from.

Be concise and analytical. You're speaking to a revenue manager who wants \
the numbers and the "why", not a sales pitch."""


def _get_gateway_url() -> str:
    return os.environ["AGENTCORE_GATEWAY_URL"]


def _create_mcp_client() -> MCPClient:
    gateway_url = _get_gateway_url()
    return MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=gateway_url,
            aws_service="bedrock-agentcore",
        )
    )


def _classify_complexity(prompt: str) -> str:
    """Policy-driven routing decision (Haiku), NOT a random choice: a cheap,
    fast classification call decides whether the main reasoning step needs
    Sonnet (baseline) or Opus (heavy multi-step analysis).

    Defaults to "simple" (Sonnet) on any classification failure -- routing
    up unnecessarily only costs more, never produces a wrong answer, so the
    safe failure mode is the cheaper path, not silently escalating to Opus.
    """
    try:
        router_model = BedrockModel(model_id=HAIKU_MODEL_ID, **_guardrail_kwargs())
        router = Agent(model=router_model, system_prompt=_ROUTER_SYSTEM_PROMPT)
        result = router(prompt)
        classification = result.message["content"][0]["text"].strip().lower()
        if classification not in ("simple", "complex"):
            logger.warning("Router returned unexpected classification: %r", classification)
            return "simple"
        return classification
    except Exception:
        logger.exception("Complexity routing (Haiku) failed; defaulting to simple/Sonnet")
        return "simple"


@app.entrypoint
async def invoke(payload=None, context=None):
    """Main entrypoint.

    Chat turn:      payload = {"prompt": "<question>"}
    List history:   payload = {"action": "list_sessions"}
    Load a chat:    payload = {"action": "get_history", "session_id": "<id>"}

    For a chat turn the client supplies the runtime session id via the
    X-Amzn-Bedrock-AgentCore-Runtime-Session-Id header (context.session_id),
    which scopes AgentCore Memory's conversation history. The history
    actions are ALWAYS scoped to the caller's own actor_id (derived from
    their verified token below) -- the payload never supplies an actor_id,
    so one analyst can never read another's conversations.
    """
    payload = payload or {}
    action = payload.get("action")

    # Caller identity first -- both the chat path and the history actions
    # need the actor_id, and it MUST come from the verified token, never
    # the client payload.
    caller_token, caller_claims = _extract_caller_context(context)
    actor_id = _resolve_actor_id(caller_claims)

    # --- Chat-history read actions (no model call, no tools) ---
    if action == "list_sessions":
        try:
            return {"status": "success", "sessions": _list_user_sessions(actor_id)}
        except Exception:
            logger.exception("list_sessions failed for actor")
            return {"status": "error", "error": "could not list conversations"}

    if action == "get_history":
        history_session_id = payload.get("session_id")
        if not isinstance(history_session_id, str) or not history_session_id.strip():
            return {"status": "error", "error": "get_history requires a 'session_id'"}
        try:
            # actor_id is the caller's own -- list_events is scoped to it,
            # so a caller cannot read a session that isn't theirs.
            messages = _get_session_history(actor_id, history_session_id)
            return {"status": "success", "messages": messages}
        except Exception:
            logger.exception("get_history failed for actor")
            return {"status": "error", "error": "could not load conversation"}

    if action == "delete_session":
        del_session_id = payload.get("session_id")
        if not isinstance(del_session_id, str) or not del_session_id.strip():
            return {"status": "error", "error": "delete_session requires a 'session_id'"}
        try:
            # actor_id is the caller's own -- deletion is scoped to it, so a
            # caller can only ever delete their own conversation.
            deleted = _delete_session(actor_id, del_session_id)
            return {"status": "success", "deleted": deleted}
        except Exception:
            logger.exception("delete_session failed for actor")
            return {"status": "error", "error": "could not delete conversation"}

    # --- Normal chat turn ---
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"status": "error", "error": "payload must include a non-empty 'prompt' string"}

    complexity = _classify_complexity(prompt)
    main_model_id = OPUS_MODEL_ID if complexity == "complex" else SONNET_MODEL_ID
    logger.info("Routing decision: complexity=%s model=%s", complexity, main_model_id)

    session_id = getattr(context, "session_id", None) or "default-session"

    # The verified email claim from the analyst's own token -- passed into
    # generate_report so a report is delivered to the person who asked for
    # it (see the generate_report metadata note + report_delivery.py). None
    # when the token carries no email; delivery then falls back to the
    # configured default recipient rather than dropping the report.
    analyst_email = caller_claims.get("email") if caller_claims else None
    caller_token_hook = _create_caller_token_hook(caller_token)

    session_manager = _create_session_manager(session_id, actor_id)
    code_interpreter_tool, generate_report_tool = _create_code_interpreter_tools(session_id, analyst_email)

    mcp_client = _create_mcp_client()
    with mcp_client:
        tools = mcp_client.list_tools_sync() + [code_interpreter_tool, generate_report_tool]
        model = BedrockModel(
            model_id=main_model_id,
            max_tokens=MAX_OUTPUT_TOKENS,
            **_guardrail_kwargs(),
        )
        hooks = [caller_token_hook] if caller_token_hook else []
        hooks.append(_AccessDeniedHook())
        source_tracker = _ToolSourceTrackerHook()
        hooks.append(source_tracker)
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=SYSTEM_PROMPT.format(
                current_date=datetime.now(timezone.utc).strftime("%A, %B %-d, %Y (%Y-%m-%d)")
            ),
            session_manager=session_manager,
            hooks=hooks,
        )
        try:
            result = agent(prompt)
        except MaxTokensReachedException:
            # The model hit its output-token ceiling mid-turn. Strands has
            # already appended the partial assistant message to history;
            # returning a clean, explicit error here (rather than letting the
            # exception propagate as a 500) keeps the failure legible to the
            # caller and lets them retry. max_tokens is set high above to make
            # this rare, but a pathological turn can still reach it.
            logger.exception("Main model reached its maximum output length (model id=%s)", main_model_id)
            return {
                "status": "error",
                "error": "response_too_long",
                "message": (
                    "That answer grew too long to finish in one turn. Try "
                    "narrowing the request (a single region, metric, or date "
                    "range) and ask again."
                ),
                "model_used": main_model_id,
                "routing_complexity": complexity,
            }

    return {
        "status": "success",
        "response": result.message["content"][0]["text"],
        "model_used": main_model_id,
        "model_label": _friendly_model_label(main_model_id),
        "router_model_label": _friendly_model_label(HAIKU_MODEL_ID),
        "routing_complexity": complexity,
        "data_sources": source_tracker.sources,
    }


if __name__ == "__main__":
    app.run()
