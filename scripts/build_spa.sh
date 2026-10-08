#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# build_spa.sh — installs SPA dependencies (if needed) and runs the
# esbuild bundle, producing terraform/modules/frontend/build/spa-dist/.
# Invoked by terraform/modules/frontend/deploy.tf's null_resource.build_spa
# local-exec provisioner, mirroring scripts/build_agent_zip.sh's pattern
# for the agent's Python package.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$REPO_ROOT/frontend"

if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: node not found. Install Node.js (this build used v22) to build the SPA." >&2
  exit 1
fi

cd "$FRONTEND_DIR"

if [ ! -d node_modules ]; then
  echo "==> Installing SPA dependencies (first run)"
  npm install --silent
fi

echo "==> Building SPA bundle"
npm run build --silent

echo "==> Build complete"
