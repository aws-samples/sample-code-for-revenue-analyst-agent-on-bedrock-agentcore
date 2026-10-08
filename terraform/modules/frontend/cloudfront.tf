# CloudFront distribution -- the only public entry point to the SPA.
# OAC-only origin access (no public S3 website endpoint), TLS 1.2+
# when a custom domain and ACM certificate are set (see viewer_certificate), a strict security-headers response policy (CSP scoped to
# exactly the AWS endpoints this SPA calls -- Cognito IdP, Cognito
# Identity, and the AgentCore data plane -- nothing else), and the WAF
# WebACL from waf.tf attached.

resource "aws_cloudfront_origin_access_control" "spa" {
  name                              = "${var.name_prefix}-spa-oac"
  description                       = "OAC for the Analyst SPA's S3 origin -- no public bucket access."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Strict CSP scoped to exactly what this SPA needs: same-origin for the
# bundle itself, plus HTTPS calls to Cognito IdP (login), Cognito Identity
# (credential exchange), and the AgentCore Runtime data plane (the actual
# agent calls). No third-party scripts, no wildcard connect-src, no inline
# script execution beyond what the bundler needs for module loading.
resource "aws_cloudfront_response_headers_policy" "spa" {
  name    = "${var.name_prefix}-spa-security-headers"
  comment = "Strict security headers for the Analyst SPA."

  security_headers_config {
    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }

    strict_transport_security {
      access_control_max_age_sec = 63072000 # 2 years
      include_subdomains         = true
      preload                    = true
      override                   = true
    }

    content_security_policy {
      content_security_policy = join("; ", [
        "default-src 'self'",
        "script-src 'self'",
        # No 'unsafe-inline': all styling is in styles.css, and the SPA
        # shows/hides elements with the `hidden` attribute, not inline styles.
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self' https://cognito-idp.${data.aws_region.current.region}.amazonaws.com https://bedrock-agentcore.${data.aws_region.current.region}.amazonaws.com",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'"
      ])
      override = true
    }
  }

  remove_headers_config {
    items {
      header = "server"
    }
  }
}

locals {
  use_custom_domain = var.custom_domain != ""
}

resource "aws_cloudfront_distribution" "spa" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  comment             = "Revenue Analyst Agent -- Analyst SPA"
  price_class         = "PriceClass_100" # US/Canada/Europe edge locations only; lowest cost tier
  web_acl_id          = aws_wafv2_web_acl.spa.arn
  aliases             = local.use_custom_domain ? [var.custom_domain] : []

  logging_config {
    bucket          = aws_s3_bucket.access_logs.bucket_domain_name
    prefix          = "cloudfront/"
    include_cookies = false
  }

  origin {
    domain_name              = aws_s3_bucket.spa.bucket_regional_domain_name
    origin_id                = "spa-s3-origin"
    origin_access_control_id = aws_cloudfront_origin_access_control.spa.id
  }

  default_cache_behavior {
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    target_origin_id           = "spa-s3-origin"
    viewer_protocol_policy     = "redirect-to-https"
    compress                   = true
    cache_policy_id            = "658327ea-f89d-4fab-a63d-7e88639e58f6" # AWS managed: CachingOptimized
    response_headers_policy_id = aws_cloudfront_response_headers_policy.spa.id
  }

  # SPA client-side routing: any path CloudFront can't find in the bucket
  # (a deep link like /chat) falls back to index.html so the SPA's own
  # router handles it, rather than surfacing a raw S3 404.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    # Two modes, chosen by var.custom_domain / var.acm_certificate_arn:
    #
    # Custom domain (recommended beyond a demo): the ACM certificate is
    # served with SNI and the minimum viewer protocol is TLSv1.2_2021.
    #
    # Default (no custom domain): CloudFront's shared *.cloudfront.net
    # certificate. With cloudfront_default_certificate = true, CloudFront
    # only accepts minimum_protocol_version = "TLSv1"; any other value is
    # overridden server-side and leaves a permanent plan diff
    # (github.com/hashicorp/terraform-provider-aws/issues/44756).
    cloudfront_default_certificate = local.use_custom_domain ? false : true
    acm_certificate_arn            = local.use_custom_domain ? var.acm_certificate_arn : null
    ssl_support_method             = local.use_custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.use_custom_domain ? "TLSv1.2_2021" : "TLSv1"
  }

  tags = {
    Name = "${var.name_prefix}-spa-distribution"
  }

  depends_on = [aws_s3_bucket_acl.access_logs]
}
