# Revenue Analyst Agent on Amazon Bedrock AgentCore

A sample AI agent that answers revenue and occupancy questions for a fictional
hotel chain (AnyCompany Hotels). Analysts sign in to a web app, ask questions in
plain language, and the agent pulls figures through governed tools, runs
analysis in a sandboxed code interpreter, proposes pricing recommendations that
need human confirmation, and emails a link to generated PDF reports.

The sample shows how to build and secure an agent on
[Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/) with
[Strands Agents](https://strandsagents.com/), and how to deploy all of it with
Terraform.

> [!IMPORTANT]
> **This is sample code for learning and experimentation. It is not intended
> for production use.** It uses synthetic data only. Before you use any part of
> it with real data or real users, review it against your own security,
> compliance, and operational requirements, and apply the hardening steps in
> [Before you use this beyond a demo](#before-you-use-this-beyond-a-demo).
> You are responsible for the AWS resources you deploy and their cost.

## What it demonstrates

- **AgentCore Runtime** hosting a Strands agent from a code package (no
  container), with a JWT authorizer that only accepts tokens from the sample's
  Cognito user pool.
- **AgentCore Gateway** exposing a Lambda function as MCP tools, behind an
  `AWS_IAM` authorizer so only the Runtime's role can call it.
- **AgentCore Memory** for per-user conversation history, and **AgentCore Code
  Interpreter** for sandboxed analysis and chart generation.
- **Role- and region-based data scoping.** `RevenueManager` users see the whole
  chain. `RegionalManager` users only see properties in their own region. The
  tools enforce this from the caller's token, not from anything the model
  writes.
- **Defense in depth for identity.** The tools Lambda verifies the analyst's
  Cognito ID token itself (signature against the user pool's JWKS, issuer,
  audience, `token_use`, and expiry) before reading any claim, instead of
  trusting the upstream authorizer.
- **Human-in-the-loop writes.** The agent can only *propose* a pricing
  recommendation. The same analyst must confirm it before it is recorded.
- **Multi-model routing.** Claude Haiku classifies each question, Claude Sonnet
  answers most of them, and Claude Opus handles complex multi-step analysis.
- **Amazon Bedrock Guardrails** applied to every model call.
- A **static single-page app** served from a private S3 bucket through
  CloudFront with Origin Access Control, AWS WAF, and a strict Content Security
  Policy.
- **Least-privilege IAM** roles for every component, scoped to the sample's own
  buckets and prefixes.

## Architecture

```
Browser (SPA)
   |  HTTPS, Cognito sign-in (SRP)
   v
CloudFront + AWS WAF --> S3 (static SPA, private, OAC)
   |
   |  Cognito ID token (JWT)
   v
AgentCore Runtime (Strands agent)
   |-- Bedrock models (Haiku / Sonnet / Opus) + Guardrail
   |-- AgentCore Memory (conversation history)
   |-- AgentCore Code Interpreter (analysis, charts, PDF reports -> S3)
   |
   |  SigV4 (Runtime role only)
   v
AgentCore Gateway (MCP) --> Lambda tools
                               |-- synthetic reporting data
                               |-- Athena + Glue (event lake in S3)
                               |-- S3 (recommendations)

S3 report upload --> Lambda --> Amazon SES (email with a short-lived pre-signed link)
```

### Tools the agent can call

| Tool | What it does |
|---|---|
| `list_properties` | Lists hotels the caller is allowed to see. |
| `get_range_metrics` | Booking and revenue totals plus a daily breakdown (up to 92 days). |
| `get_occupancy` | Per-property occupancy, revenue, and check-in/out counts, optionally by region. |
| `query_analytics` | Runs one of a fixed set of read-only Athena query templates. No free-form SQL. |
| `propose_recommendation` | Saves a pricing recommendation as `PENDING`. |
| `record_recommendation` | Confirms a `PENDING` recommendation. Only the analyst who proposed it can confirm it. |
| `code_interpreter` | Runs Python in an isolated AgentCore Code Interpreter session. |
| `generate_report` | Saves a PDF report and triggers the report-ready email. |

## Data

All data is synthetic and generated in code (`tools/lib/sample_data.py`). The
same inputs always produce the same numbers, so answers are reproducible. There
is no customer data, personal data, or payment data, and no external datasets.
To connect a real system, replace the functions in
`tools/lib/reporting_client.py`.

## Prerequisites

- An AWS account and credentials with permission to create the resources in
  this sample. Use a non-production account.
- A Region that offers Amazon Bedrock AgentCore. The defaults use `us-east-1`
  and US cross-Region inference profiles.
- [Model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)
  in Amazon Bedrock for Claude Haiku 4.5, Claude Sonnet 4.6, and Claude Opus 4.8,
  or override the model IDs (`AGENT_HAIKU_MODEL_ID`, `AGENT_SONNET_MODEL_ID`,
  `AGENT_OPUS_MODEL_ID`).
- Terraform 1.10 or later
- AWS CLI v2
- Python 3.13 (`python3.13` on your `PATH`, used to build the agent package and the tools Lambda layer)
- Node.js 22 and npm (used to build the web app)

Check your setup with:

```bash
scripts/check_prereqs.sh
```

The script is read-only. It checks local tools, AWS credentials, Bedrock
inference profiles, and whether Amazon SES is still in the sandbox.

## Deploy

### 1. Create the Terraform state bucket (once per account)

```bash
cd terraform/bootstrap
terraform init
terraform apply
terraform output -raw tfstate_bucket_name
cd ../..
```

### 2. Configure the environment

```bash
cd terraform/envs/dev
cp backend.hcl.example backend.hcl            # set bucket to the name from step 1
cp terraform.tfvars.example terraform.tfvars  # set notification_email
```

`notification_email` receives budget alerts and is the sender address for
report emails. Both files are gitignored.

Optional: to serve the app on your own domain with TLS 1.2 or later, set
`custom_domain` and `acm_certificate_arn` (an ACM certificate in `us-east-1`
that covers the domain) in `terraform.tfvars`. After deploying, create a DNS
CNAME or alias record from the domain to `terraform output -raw cloudfront_domain_name`.

### 3. Deploy

```bash
terraform init -backend-config=backend.hcl
terraform apply
```

Terraform builds the agent package, the tools Lambda layer, and the web app locally, loads the synthetic
event data, and deploys everything. No Docker is needed.

After the first apply, Amazon SES sends a verification email to
`notification_email`. Click the link, or report emails won't be sent.

**If your account uses AWS Lake Formation** (that is, Lake Formation is set up
and new Glue databases don't default to IAM-only access), the `query_analytics`
tool fails with "Insufficient Lake Formation permission(s)". Grant the tools
Lambda's role access to the sample's database and table once, as a Lake
Formation administrator:

```bash
ROLE_ARN=$(aws lambda get-function-configuration \
  --function-name "$(terraform output -raw tools_lambda_function_name)" \
  --query Role --output text)
DB=$(terraform output -raw glue_database_name)

aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier="$ROLE_ARN" \
  --permissions DESCRIBE \
  --resource "{\"Database\":{\"Name\":\"$DB\"}}"

aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier="$ROLE_ARN" \
  --permissions SELECT DESCRIBE \
  --resource "{\"Table\":{\"DatabaseName\":\"$DB\",\"Name\":\"events\"}}"
```

Accounts that still use the default IAM-only access control need no extra step.

### 4. Create an analyst user

Users can't sign themselves up. Create one with:

```bash
POOL_ID=$(terraform output -raw user_pool_id)

# Chain-wide access
../../../scripts/create_demo_user.sh "$POOL_ID" analyst@example.com RevenueManager

# Or region-scoped access (Northeast, Southeast, Midwest, West, South, Other)
../../../scripts/create_demo_user.sh "$POOL_ID" analyst@example.com RegionalManager West
```

The script prompts for a password (12+ characters with upper case, lower case,
a digit, and a symbol). It never echoes or stores it.

While your account is in the SES sandbox, SES only delivers to verified
addresses. To receive report emails, also verify the analyst's address:

```bash
aws ses verify-email-identity --email-address analyst@example.com
```

### 5. Open the app

```bash
terraform output -raw spa_url
```

Sign in and try questions like:

- "What was occupancy across the chain last month?"
- "Which West properties had the biggest drop in revenue over the last 30 days?"
- "Compare cancellations by region and chart them."
- "Propose a weekend rate change for the property with the lowest occupancy."
- "Generate a PDF report of last month's performance."

## Cost

You pay for the AWS resources you deploy and use. The main cost drivers are
Amazon Bedrock model calls, AgentCore, Athena queries, CloudFront, and AWS WAF.
The sample creates an AWS Budgets alert (default USD 50 per month, set with
`budget_limit_usd`) filtered on the `Project=RevenueAnalystAgent` tag. The
filter only works after you
[activate that tag as a cost allocation tag](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/activating-tags.html).
Athena queries are capped at 1 GB scanned per query.

Destroy the stack when you're done (see [Clean up](#clean-up)).

## Before you use this beyond a demo

The sample makes trade-offs to stay simple and cheap to run. Address these
before using it with real users or data:

- **Require MFA.** Cognito MFA defaults to `OPTIONAL` because the sample app has
  no MFA enrollment or challenge screens. For any non-demo deployment, set
  `cognito_mfa_configuration = "ON"` and add TOTP enrollment and challenge
  handling to the app (or use Cognito managed login, below).
- **Use TLS 1.2 or later.** By default the distribution uses the
  `*.cloudfront.net` certificate, and CloudFront only allows a minimum of
  `TLSv1` with that certificate. Set `custom_domain` and `acm_certificate_arn`
  (an [AWS Certificate Manager](https://docs.aws.amazon.com/acm/latest/userguide/acm-overview.html)
  certificate in `us-east-1`) and the distribution switches to SNI with a
  `TLSv1.2_2021` minimum.
- **Don't keep tokens in browser storage.** The app uses
  `amazon-cognito-identity-js`, which stores tokens in `localStorage`. For
  production, use
  [Cognito managed login](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-managed-login.html)
  with the authorization code flow and PKCE.
- **Protect the AgentCore Runtime endpoint.** The app calls the AgentCore
  Runtime data-plane endpoint directly, so those requests don't pass through
  CloudFront or AWS WAF. The Runtime's JWT authorizer rejects any request
  without a valid token from this user pool, and the tools Lambda has a
  concurrency cap, but there is no rate limiting per user. Before wider use,
  add per-user throttling or put the agent behind an API you can protect with
  WAF.
- **Move SES out of the sandbox** and set up SPF, DKIM, and DMARC for your
  sender domain.
- **Review logging, retention, and encryption** against your requirements.
  Log groups default to 365-day retention and use AWS-managed encryption.
  Buckets use SSE-S3. You may need customer-managed KMS keys, longer retention,
  S3 access logging on every bucket, or cross-Region replication.
- **Review the Guardrail** configuration in `terraform/modules/agent/guardrail.tf`
  for your content policies.

[SECURITY-NOTES.md](SECURITY-NOTES.md) lists every scanner finding and how it
was fixed or why it was accepted.

## Run the tests

```bash
python3 -m pip install pytest boto3 "pyjwt[crypto]"
python3 -m pytest tests
```

## Clean up

```bash
cd terraform/envs/dev
terraform destroy
```

The artifacts bucket keeps generated reports and recommendations and is
versioned, so `terraform destroy` fails if it isn't empty. Empty it first (in
the S3 console, choose the bucket and then **Empty**), then run
`terraform destroy` again. The bucket name is in
`terraform output -raw artifacts_bucket_name`.

AgentCore Runtime creates its own CloudWatch log group, named
`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`, which Terraform doesn't
manage. Delete it in the CloudWatch console or with
`aws logs delete-log-group --log-group-name <name>`. If you granted Lake
Formation permissions (see Deploy), they go away when Terraform deletes the Glue database.

To remove the state bucket too, empty it the same way and run
`terraform destroy` in `terraform/bootstrap`.

## Repository layout

| Path | Contents |
|---|---|
| `agent/` | The Strands agent that runs in AgentCore Runtime |
| `tools/` | Lambda tools behind AgentCore Gateway, and the report-delivery handler |
| `frontend/` | Analyst web app (vanilla JavaScript, built with esbuild) |
| `terraform/bootstrap/` | One-time Terraform state bucket |
| `terraform/envs/dev/` | Root module that deploys the sample |
| `terraform/modules/` | Modules: `foundation`, `analytics`, `tools`, `agent`, `identity`, `frontend` |
| `scripts/` | Prerequisite check, build scripts, sample data generator, user creation |
| `tests/` | Unit tests for the Lambda tools |

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more
information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE)
file.
