# Security Notes

This document records how each finding from automated security scanning
(checkov, semgrep, Bandit, and dependency audits) and from the threat model
was addressed. It is intended for security reviewers and for anyone deploying
this sample.

This is **sample code** that demonstrates an Amazon Bedrock AgentCore agent
with Lambda tools, Athena analytics, and a static SPA front end. It is a
minimal, legible demonstration, not a production deployment. Some
production-grade hardening controls are intentionally out of scope and are
called out below with the rationale.

## Fixed

| Finding | File | Resolution |
|---|---|---|
| `code-after-unconditional-return` | `agent/agent.py` | Removed a duplicated `return` block (dead code). |
| `logging-error-without-handling` | `tools/lib/analytics_client.py`, `tools/lib/recommendation_client.py` | Downgraded `logger.error` to `logger.warning` where the exception is re-raised, so the caller handles it. |
| `python-logger-credential-disclosure` (false positive) | `agent/agent.py` | Reworded the log message so it no longer matches the secret-pattern heuristic. The message never contained a secret (it logged a model id label). |
| `arbitrary-sleep` | `tools/lib/analytics_client.py` | Annotated with `nosemgrep` and a reason: it is an intentional fixed poll interval between Athena `GetQueryExecution` status checks, bounded by `MAX_POLL_ATTEMPTS`. |
| `insecure-innerhtml` / `insecure-document-method` | `frontend/src/app.js` | The chat shell is a static template with no interpolation; user data is set with `textContent`. Agent markdown is sanitized with `DOMPurify.sanitize(marked.parse(...), { RETURN_DOM_FRAGMENT: true, FORBID_ATTR: ["style"] })` and appended as DOM nodes, never assigned to `innerHTML`. |
| `aws-lambda-permission-unrestricted-source-arn` | `terraform/modules/agent/gateway.tf` | The Gateway's invoke permission on the tools Lambda sets `source_arn` to the Gateway ARN as well as `source_account`. |
| CloudWatch log retention under 1 year (`CKV_AWS_338`) | `terraform/modules/agent`, `terraform/modules/tools` | `log_retention_days` defaults to 365. |
| CloudFront access logging (`CKV_AWS_86`) | `terraform/modules/frontend/access_logs.tf` | Standard access logs go to a dedicated bucket with Block Public Access, SSE-S3, versioning, a TLS-only bucket policy, and 365-day expiry. |
| S3 bucket versioning (events bucket) | `terraform/modules/analytics/main.tf` | Added `aws_s3_bucket_versioning` (status `Enabled`). |
| Vulnerable dependencies | `agent/requirements.txt`, `frontend/package.json` | Pinned `anyio` 4.14.2 and `idna` 3.15 (CVE-2026-63374, CVE-2026-64847, CVE-2026-45409) and upgraded `dompurify` to 3.4.16 (GHSA-p98j-92pf-mc4p). |
| Missing license headers | All source files | Added `SPDX-License-Identifier: MIT-0` headers. |
| JWT claims used without signature verification (threat model) | `tools/lib/token_verifier.py`, `tools/lib/reporting_client.py` | The tools Lambda verifies the caller's Cognito ID token before reading any claim: RS256 signature against the user pool's JWKS, issuer, audience (SPA client), `token_use = id`, and expiry. A failed check yields no claims, so access is denied (fail closed). PyJWT and cryptography ship in a Lambda layer built by `scripts/build_tools_layer.sh`. Tests in `tests/test_token_verifier.py` cover forged, unsigned, expired, wrong-issuer, wrong-audience, access-token, and HS256 tokens. |
| CSP allowed `style-src 'unsafe-inline'` (threat model) | `terraform/modules/frontend/cloudfront.tf`, `frontend/src/` | Removed `'unsafe-inline'`. The SPA shows and hides elements with the `hidden` attribute instead of inline styles, and DOMPurify strips `style` attributes from agent output. |
| CloudFront minimum TLS version (`CKV_AWS_174`, `aws-insecure-cloudfront-distribution-tls-version`, `CKV2_AWS_42`) | `terraform/modules/frontend/cloudfront.tf` | Optional `custom_domain` and `acm_certificate_arn` variables switch the distribution to an ACM certificate with SNI and a `TLSv1.2_2021` minimum. See the accepted row below for the default. |
| Analytics queries not scoped by role (threat model) | `tools/handler.py`, `agent/agent.py` | `query_analytics` receives the caller's token. Region-scoped users can only run the per-property template, and results are filtered to their own properties. Chain-wide templates are denied for them. |
| Recommendation confirmation not bound to the proposer (threat model) | `tools/lib/recommendation_client.py` | Each record stores the proposer's `sub`. Only the same analyst can confirm it. |
| Generated reports kept indefinitely (threat model) | `terraform/modules/foundation/artifacts.tf` | Added a lifecycle rule that expires `reports/` after `report_retention_days` (default 30). |

## Verified already-safe (no change needed)

| Finding | File | Why it is safe |
|---|---|---|
| S3 bucket versioning (`aws-s3-bucket-versioning-not-enabled`) on the other buckets | `agent/s3.tf`, `frontend/s3.tf`, `frontend/access_logs.tf`, `foundation/artifacts.tf`, `bootstrap/main.tf` | Each bucket declares `aws_s3_bucket_versioning` with status `Enabled`. The scanner keys on the `aws_s3_bucket` resource and does not always correlate the separate versioning resource. |
| `no-iam-data-exfiltration` (X-Ray `Resource = "*"`) | `tools/lambda.tf`, `agent/runtime.tf` | X-Ray `PutTraceSegments`, `PutTelemetryRecords`, `GetSamplingRules`, and `GetSamplingTargets` do not support resource-level permissions, and X-Ray has no service-specific condition keys; `*` is required. |
| `no-iam-data-exfiltration` (SES `Resource = "*"`) | `tools/lambda.tf` | The `ses:SendEmail` grant is constrained by a `Condition` on `ses:FromAddress`, so the role can only send from the one verified sender identity. |
| `no-iam-data-exfiltration` (S3 read grants) | `tools/lambda.tf`, `agent/runtime.tf` | Each grant is limited to the sample's own bucket and prefix (agent code package, synthetic events, Athena results, reports, recommendations). |
| WAF without the Log4j managed rule (`CKV2_AWS_47`) | `terraform/modules/frontend/waf.tf` | False positive: the WebACL includes `AWSManagedRulesKnownBadInputsRuleSet`, which contains the Log4j rules. |
| S3 ACLs enabled (`CKV2_AWS_65`) | `terraform/modules/frontend/access_logs.tf` | Only on the CloudFront access-log bucket, because CloudFront standard log delivery requires ACLs. The bucket blocks public access and denies non-TLS requests. |
| Bandit `assert_used` (B101) | `tests/` | pytest assertions in test code, which is not deployed. |

## Accepted for the sample (documented for deployers)

These are hardening controls appropriate for production. They add cost or
infrastructure that would obscure the sample's purpose. The README's "Before
you use this beyond a demo" section lists what to change before real use.

| Finding | Rationale |
|---|---|
| CloudFront TLSv1 minimum when no custom domain is set (`CKV_AWS_174`) | With the default `*.cloudfront.net` certificate, CloudFront only accepts `minimum_protocol_version = "TLSv1"`; any other value is overridden server-side. Deployers get TLS 1.2+ by setting `custom_domain` and `acm_certificate_arn`. Mitigations: `redirect-to-https`, HSTS, and WAF. |
| Cognito MFA defaults to `OPTIONAL` | The SPA has no TOTP enrollment or challenge screens. The README tells deployers to set `cognito_mfa_configuration = "ON"` for any non-demo deployment. |
| AgentCore Runtime endpoint is not behind AWS WAF | The SPA calls the Runtime data plane directly. Requests without a valid token from this user pool are rejected by the Runtime's JWT authorizer, and the tools Lambda verifies the token again and has a concurrency cap. There is no per-user rate limiting; the README describes how to add it. |
| CloudWatch Log Group KMS CMK encryption (`CKV_AWS_158`, `missing-cloudwatch-log-group-kms-key`, `aws-cloudwatch-log-group-unencrypted`) | Log data is already encrypted with AWS-managed keys. A customer-managed KMS key adds key-management overhead not needed for a demo. Listed in the README hardening steps. |
| Lambda environment variable KMS CMK encryption (`CKV_AWS_173`, `aws-lambda-environment-unencrypted`) | Environment variables are encrypted at rest with an AWS-managed key. The variables are non-secret identifiers (bucket names, user pool and client IDs, model IDs). |
| S3 default KMS encryption (`CKV_AWS_145`) and CMK rotation (`CKV2_AWS_67`) | Buckets use SSE-S3 (AES256). The data is synthetic or static build output. No customer-managed keys are used, so rotation does not apply. |
| S3 server access logging (`CKV_AWS_18`) | CloudFront access logs go to a dedicated bucket. Server access logging on the other buckets is left to deployers. |
| S3 cross-Region replication (`CKV_AWS_144`) | The buckets hold regenerable sample data; replication adds cost with no benefit here. |
| S3 lifecycle rules (`CKV2_AWS_61`, `CKV_AWS_300`) | The artifacts and access-log buckets expire objects and abort incomplete multipart uploads. The other buckets hold static or regenerable data. |
| S3 event notifications (`CKV2_AWS_62`) | Used where the design needs them: new objects under `reports/` trigger report delivery. |
| WAF logging (`CKV2_AWS_31`), CloudFront origin failover (`CKV_AWS_310`) | Observability and resilience beyond a demo's needs. |
| Lambda in a VPC (`CKV_AWS_117`) | The Lambda only calls AWS APIs (Athena, S3, SES, Glue, and the Cognito JWKS endpoint); no VPC resources are accessed. |
| Lambda dead-letter queue (`CKV_AWS_116`) | The Lambda is invoked synchronously by AgentCore Gateway; a DLQ applies to asynchronous invocation. The S3-triggered delivery path logs its failures. |
| Lambda code signing (`CKV_AWS_272`) | A production supply-chain control, out of scope for a sample. |
