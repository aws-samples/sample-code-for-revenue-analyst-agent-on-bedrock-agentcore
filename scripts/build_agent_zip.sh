#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# build_agent_zip.sh — assembles the direct-code-deployment package for
# AgentCore Runtime (terraform/modules/agent).
#
# AgentCore Runtime's direct code deployment mode runs the package on a
# managed Amazon Linux / arm64 Python 3.13 environment. Any dependency with
# compiled (non-pure-Python) code MUST be installed as an arm64 Linux wheel,
# not whatever wheel pip would pick on the build machine. pip can
# fetch platform-specific wheels without needing Docker, via
# --platform / --only-binary, as long as manylinux wheels exist for the
# package (true for all of this project's dependencies).
#
# Output: terraform/modules/agent/build/agent-package/
#   agent.py                  (entrypoint)
#   <vendored dependencies>/  (arm64 Linux wheels, unpacked)
#
# No __pycache__ is included (AWS explicitly recommends against shipping
# bytecode compiled on a different architecture/OS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR="$REPO_ROOT/agent"
BUILD_DIR="$REPO_ROOT/terraform/modules/agent/build/agent-package"

echo "==> Cleaning previous build output"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "==> Copying agent source"
cp "$AGENT_DIR/agent.py" "$BUILD_DIR/"

# pip enforces a package's Requires-Python against the ACTUAL running
# interpreter, not the --python-version target flag (that flag only affects
# wheel tag selection). bedrock-agentcore/strands-agents require >=3.10, so
# run pip with python3.13, which matches AgentCore Runtime's target runtime.
# It is a local build-time dependency only.
PIP_PYTHON="python3.13"
if ! command -v "$PIP_PYTHON" >/dev/null 2>&1; then
  echo "ERROR: $PIP_PYTHON not found. Install Python 3.13 (for example: brew install python@3.13, or from python.org)." >&2
  exit 1
fi

echo "==> Installing dependencies for linux/arm64 (Python 3.13), no Docker required"
"$PIP_PYTHON" -m pip install \
  --platform manylinux2014_aarch64 \
  --target "$BUILD_DIR" \
  --python-version 3.13 \
  --only-binary=:all: \
  --no-deps \
  -r "$AGENT_DIR/requirements.txt" \
  --quiet

echo "==> Re-resolving transitive dependencies for linux/arm64"
# --no-deps above avoids pip pulling in build-machine-platform transitive
# deps; do a second pass WITH deps but still platform-pinned, so transitive
# requirements (e.g. pydantic, httpx used by strands/bedrock-agentcore) are
# also arm64 Linux wheels, not macOS ones.
"$PIP_PYTHON" -m pip install \
  --platform manylinux2014_aarch64 \
  --target "$BUILD_DIR" \
  --python-version 3.13 \
  --only-binary=:all: \
  --upgrade \
  -r "$AGENT_DIR/requirements.txt" \
  --quiet

echo "==> Removing __pycache__ directories (bytecode is build-machine-specific)"
find "$BUILD_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +

echo "==> Build complete: $BUILD_DIR"
du -sh "$BUILD_DIR"
