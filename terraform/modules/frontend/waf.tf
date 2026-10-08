# WAF WebACL for the SPA's CloudFront distribution, the one internet-facing
# entry point in this sample. CLOUDFRONT-scope WebACLs must live in
# us-east-1, so this resource uses the aws.us_east_1 provider alias passed
# in from the root module.

resource "aws_wafv2_web_acl" "spa" {
  provider = aws.us_east_1

  name        = "${var.name_prefix}-spa-web-acl"
  description = "WAF for the Revenue Analyst Agent Analyst SPA, CloudFront-scoped."
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  # AWS managed Core Rule Set -- baseline protection against common web
  # exploits (XSS, common injection patterns, etc.).
  rule {
    name     = "AWS-AWSManagedRulesCommonRuleSet"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name_prefix}-common-rule-set"
      sampled_requests_enabled   = true
    }
  }

  # AWS managed known-bad-inputs rule set -- request patterns already known
  # to correlate with exploitation attempts.
  rule {
    name     = "AWS-AWSManagedRulesKnownBadInputsRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name_prefix}-known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  # Per-IP rate limit -- bounds abuse/cost from any single source without
  # needing a custom Lambda@Edge or CloudFront Function.
  rule {
    name     = "RateLimitPerIP"
    priority = 3

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 500
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name_prefix}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name_prefix}-spa-web-acl"
    sampled_requests_enabled   = true
  }

  tags = {
    Name = "${var.name_prefix}-spa-web-acl"
  }
}
