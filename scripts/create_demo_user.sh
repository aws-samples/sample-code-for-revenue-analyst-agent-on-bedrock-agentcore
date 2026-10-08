#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# create_demo_user.sh -- creates an analyst account in the sample's Cognito
# user pool and adds it to a group.
#
# Usage:
#   scripts/create_demo_user.sh <user-pool-id> <email> RevenueManager
#   scripts/create_demo_user.sh <user-pool-id> <email> RegionalManager <region>
#
#   <user-pool-id>  terraform -chdir=terraform/envs/dev output -raw user_pool_id
#   <region>        Northeast | Southeast | Midwest | West | South | Other
#
# The password is read from the terminal (not echoed, not stored anywhere)
# and set as permanent, so the user can sign in to the SPA straight away.
# It must be 12+ characters with upper, lower, digit, and symbol.
#
# Report emails are sent through SES. While the account is in the SES
# sandbox, SES only delivers to verified addresses, so verify the analyst's
# email too if you want the report email to arrive:
#   aws ses verify-email-identity --email-address <email>
set -euo pipefail

usage() {
  sed -n '4,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

[ $# -ge 3 ] || usage
POOL_ID="$1"
EMAIL="$2"
GROUP="$3"
REGION="${4:-}"

case "$GROUP" in
  RevenueManager)
    [ -z "$REGION" ] || { echo "RevenueManager is chain-level; do not pass a region." >&2; exit 1; }
    ;;
  RegionalManager)
    case "$REGION" in
      Northeast|Southeast|Midwest|West|South|Other) ;;
      *) echo "RegionalManager needs a region: Northeast, Southeast, Midwest, West, South, or Other." >&2; exit 1 ;;
    esac
    ;;
  *)
    echo "Group must be RevenueManager or RegionalManager." >&2
    exit 1
    ;;
esac

read -r -s -p "Password for $EMAIL: " PASSWORD
echo
read -r -s -p "Confirm password: " PASSWORD_CONFIRM
echo
[ "$PASSWORD" = "$PASSWORD_CONFIRM" ] || { echo "Passwords do not match." >&2; exit 1; }

ATTRS=("Name=email,Value=$EMAIL" "Name=email_verified,Value=true")
[ -z "$REGION" ] || ATTRS+=("Name=custom:region,Value=$REGION")

echo "==> Creating user $EMAIL"
aws cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" \
  --username "$EMAIL" \
  --user-attributes "${ATTRS[@]}" \
  --message-action SUPPRESS \
  --output text --query 'User.Username' >/dev/null

echo "==> Setting permanent password"
# Passed as JSON on stdin so the password never appears in the process list.
POOL_ID="$POOL_ID" EMAIL="$EMAIL" PASSWORD="$PASSWORD" python3 -c '
import json, os
print(json.dumps({"UserPoolId": os.environ["POOL_ID"], "Username": os.environ["EMAIL"],
                  "Password": os.environ["PASSWORD"], "Permanent": True}))
' | aws cognito-idp admin-set-user-password --cli-input-json file:///dev/stdin

echo "==> Adding to group $GROUP"
aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$POOL_ID" \
  --username "$EMAIL" \
  --group-name "$GROUP"

unset PASSWORD PASSWORD_CONFIRM
echo "Done. $EMAIL can now sign in to the SPA."
