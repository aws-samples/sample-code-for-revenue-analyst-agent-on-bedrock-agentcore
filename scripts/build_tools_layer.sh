#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# build_tools_layer.sh -- builds the Lambda layer for the tools Lambda
# (terraform/modules/tools): PyJWT and cryptography, used to verify the
# analyst's Cognito ID token.
#
# cryptography has compiled code, so the wheels must match the Lambda
# runtime (Python 3.13, x86_64, Amazon Linux 2023), not the build machine.
# pip fetches those wheels with --platform / --only-binary; no Docker needed.
#
# Output: terraform/modules/tools/build/tools-layer/python/<packages>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REQUIREMENTS="$REPO_ROOT/tools/requirements.txt"
LAYER_DIR="$REPO_ROOT/terraform/modules/tools/build/tools-layer"

PIP_PYTHON="python3.13"
if ! command -v "$PIP_PYTHON" >/dev/null 2>&1; then
  echo "ERROR: $PIP_PYTHON not found. Install Python 3.13 (for example: brew install python@3.13, or from python.org)." >&2
  exit 1
fi

echo "==> Cleaning previous layer build"
rm -rf "$LAYER_DIR"
mkdir -p "$LAYER_DIR/python"

echo "==> Installing layer dependencies for linux/x86_64 (Python 3.13)"
"$PIP_PYTHON" -m pip install \
  --platform manylinux_2_28_x86_64 \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --only-binary=:all: \
  --target "$LAYER_DIR/python" \
  -r "$REQUIREMENTS" \
  --quiet

find "$LAYER_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +

echo "==> Layer build complete: $LAYER_DIR"
