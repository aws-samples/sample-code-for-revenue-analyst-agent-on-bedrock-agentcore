#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# check_prereqs.sh -- read-only checks that the local tools and the target
# AWS account are ready for `terraform apply`. Makes no changes.
set -uo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
FAIL=0
ok()   { echo "  OK    $1"; }
bad()  { echo "  FAIL  $1"; FAIL=1; }
warn() { echo "  WARN  $1"; }

echo "Local tools"
for cmd in terraform aws python3 python3.13 node npm; do
  if command -v "$cmd" >/dev/null 2>&1; then ok "$cmd"; else bad "$cmd not found"; fi
done
if command -v terraform >/dev/null 2>&1; then
  TF_VERSION=$(terraform version -json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null || echo 0)
  python3 - "$TF_VERSION" <<'PY' && ok "terraform $TF_VERSION >= 1.10" || bad "terraform $TF_VERSION is older than 1.10"
import sys
v = tuple(int(x) for x in sys.argv[1].split(".")[:2])
sys.exit(0 if v >= (1, 10) else 1)
PY
fi

echo "AWS account ($REGION)"
if IDENTITY=$(aws sts get-caller-identity --output text --query Arn 2>/dev/null); then
  ok "credentials: $IDENTITY"
else
  bad "no working AWS credentials (set AWS_PROFILE or run aws configure / aws sso login)"
fi

MODELS=$(aws bedrock list-inference-profiles --region "$REGION" --output text --query 'inferenceProfileSummaries[].inferenceProfileId' 2>/dev/null || true)
for model in us.anthropic.claude-haiku-4-5-20251001-v1:0 us.anthropic.claude-sonnet-4-6 us.anthropic.claude-opus-4-8; do
  if echo "$MODELS" | tr '\t' '\n' | grep -qx "$model"; then
    ok "inference profile $model"
  else
    warn "inference profile $model not listed; check model access in the Bedrock console or override the model IDs"
  fi
done

SES=$(aws sesv2 get-account --region "$REGION" --output text --query ProductionAccessEnabled 2>/dev/null || echo unknown)
if [ "$SES" = "True" ]; then ok "SES production access"; else warn "SES is in the sandbox: report emails only reach verified addresses"; fi

[ "$FAIL" -eq 0 ] && echo "Ready." || { echo "Fix the FAIL items above first."; exit 1; }
