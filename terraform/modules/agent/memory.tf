# AgentCore Memory. Short-term: conversation history within a session
# (active region, date window, disambiguation choices) -- handled
# automatically by AgentCoreMemorySessionManager in agent.py, no extra
# Terraform config needed for that part. Long-term: durable analyst
# findings (e.g. "West region soft on weekday lead-time in Q2"), extracted
# via the built-in SEMANTIC strategy and retrievable across sessions.

resource "aws_bedrockagentcore_memory" "main" {
  name        = replace("${var.name_prefix}_memory", "-", "_")
  description = "Short-term session context + long-term analyst findings for the Revenue Analyst Agent"

  # Raw conversation events expire after 30 days -- long enough to matter
  # for a demo, short enough to bound storage cost and stay
  # data-minimal (see foundation module's similar 30-day artifact
  # lifecycle rationale).
  event_expiry_duration = 30

  tags = {
    Name = "${var.name_prefix}-memory"
  }
}

resource "aws_bedrockagentcore_memory_strategy" "semantic" {
  name        = replace("${var.name_prefix}_semantic", "-", "_")
  memory_id   = aws_bedrockagentcore_memory.main.id
  type        = "SEMANTIC"
  description = "Extracts durable analyst findings (drivers, recurring patterns) from conversations, retrievable across sessions"

  # One namespace per actor (the analyst's Cognito `sub`, see agent.py
  # _resolve_actor_id), so each analyst's findings stay their own.
  namespace_templates = ["/semantic/{actorId}"]
}
